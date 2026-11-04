"""`gtnh-client` command line: prepare / run / play / export / cameras / states / coverage / screens / serve."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from gtnh_client import bridge, cameras, coverage, lock, packs, states, workers
from gtnh_client.proxy import DelayProxy
from gtnh_client.download import DownloadError
from gtnh_client.instance import Layout, PrepareError, ensure_downloads, prepare
from gtnh_client.launch import (
    JAVA_TOOL,
    LWJGL3IFY_WINDOW,
    MUTE_OPTION,
    ClientProcess,
    LaunchError,
    build_command,
    configure_lwjgl3ify,
    install_mods,
    install_resource_packs,
    keep_awake,
    mute_sound,
    restore_resource_packs,
    run_process,
    run_processes,
    tree_rss_kib,
    virtual_display,
)
from gtnh_client.pathmap import PathMapError
from gtnh_client.profile import ProfileError

ZULU8_TOOL = "java@zulu-8.98.0.107"
EXPORTER_MOD_GLOB = "bluemap-gtnh-exporter-*.jar"
EXPORT_REPORT = "export-report.json"


class CliError(Exception):
    pass


# src/gtnh_client/cli.py -> src -> launcher -> gtnh-exporter checkout; None when installed from a wheel
_CHECKOUT = Path(__file__).resolve().parents[3]
EXPORTER_REPO: Path | None = _CHECKOUT if (_CHECKOUT / "launcher" / "pyproject.toml").is_file() else None


def workspace() -> Path:
    """The workspace holding the gtnh-exporter checkout and its siblings (`BlueMap/`, `clients/`, `downloads/`, ...):
    BMGTNH_WORKSPACE, else the checkout's parent, else (wheel install) the current directory."""
    env = os.environ.get("BMGTNH_WORKSPACE")
    if env:
        return Path(env)
    return EXPORTER_REPO.parent if EXPORTER_REPO else Path.cwd()


def exporter_repo() -> Path:
    if EXPORTER_REPO is None:
        raise CliError("not running from a gtnh-exporter checkout; pass --mod-jar or use `uv run` in gtnh-exporter/launcher")
    return EXPORTER_REPO


def default_zip(pack: packs.Pack) -> Path:
    return workspace() / "downloads" / pack.zip_name


def default_client_dir(pack: packs.Pack) -> Path:
    return workspace() / "clients" / pack.client


def bluemap_repo() -> Path:
    """The BlueMap fork checkout (sibling `BlueMap/`)."""
    root = workspace() / "BlueMap"
    if not (root / "settings.gradle.kts").is_file():
        raise CliError(f"BlueMap checkout not found at {root}; clone it next to gtnh-exporter or set BMGTNH_WORKSPACE")
    return root


def make_layout(client_dir: Path | None, pack: packs.Pack) -> Layout:
    return Layout((client_dir or default_client_dir(pack)).resolve(), (workspace() / "downloads" / "mc-cache").resolve())


@contextlib.contextmanager
def client_session(
    layout: Layout,
    *,
    zip_path: Path,
    mods: list[Path],
    extra_mods: list[Path] = (),
    resource_packs: list[Path] = (),
    disable_exporter: bool = False,
):
    """Gets an instance ready for one run and yields its profile: prepares it if the client directory is missing,
    fetches missing downloads, installs `mods` and, for this run only, `extra_mods` and `resource_packs` (removed
    afterwards, options.txt `resourcePacks` restored, so later default runs are unaffected), and writes the window and
    mute settings. `disable_exporter` parks the exporter mod in `.minecraft/mods-disabled/` for the session (it would
    export and quit) and puts it back afterwards."""
    if not layout.client_dir.exists():
        prepare(layout, zip_path)
    profile = layout.load_profile()
    ensure_downloads(layout, profile)
    recover_parked_mods(layout.minecraft_dir)
    install_mods(layout.minecraft_dir, mods)
    mods_dir = layout.minecraft_dir / "mods"
    temporary_mods = [mods_dir / jar.name for jar in extra_mods if not (mods_dir / jar.name).exists()]
    install_mods(layout.minecraft_dir, list(extra_mods))
    parked: list[tuple[Path, Path]] = []
    if disable_exporter:
        disabled = layout.minecraft_dir / "mods-disabled"
        for jar in mods_dir.glob(EXPORTER_MOD_GLOB):
            disabled.mkdir(exist_ok=True)
            target = disabled / jar.name
            jar.replace(target)
            parked.append((target, jar))
            print(f"[run] parked {jar.name} in {disabled} for this session")
    installed_packs, previous_packs = install_resource_packs(layout.minecraft_dir, list(resource_packs))
    try:
        cfg = configure_lwjgl3ify(layout.minecraft_dir)
        print(f"[run] {cfg}: window " + ", ".join(f"{k}={str(v).lower()}" for k, v in LWJGL3IFY_WINDOW.items()))
        print(f"[run] {mute_sound(layout.minecraft_dir)}: {MUTE_OPTION[0]}:{MUTE_OPTION[1]} (muted)")
        yield profile
    finally:
        restore_resource_packs(layout.minecraft_dir, installed_packs, previous_packs)
        for jar in temporary_mods:
            jar.unlink(missing_ok=True)
            print(f"[run] removed extra mod {jar}")
        for parked_jar, original in parked:
            parked_jar.replace(original)
            print(f"[run] restored {original}")
        recover_parked_mods(layout.minecraft_dir)  # removes the then empty mods-disabled/


def default_log(layout: Layout, kind: str = "client") -> Path:
    return layout.instance_dir / "logs-launcher" / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}.log"


def run_client(
    layout: Layout,
    *,
    zip_path: Path,
    mods: list[Path],
    jvm_args: list[str],
    xmx: str,
    timeout: float,
    username: str,
    log: Path | None,
    extra_mods: list[Path] = (),
    resource_packs: list[Path] = (),
    game_args: list[str] = (),
    disable_exporter: bool = False,
) -> int:
    """One client run with [client_session]; returns the java exit code (see [run_process])."""
    with terminate_as_interrupt(), client_session(
        layout, zip_path=zip_path, mods=mods, extra_mods=extra_mods, resource_packs=resource_packs,
        disable_exporter=disable_exporter,
    ) as profile:
        cmd = build_command(layout, profile, xmx=xmx, jvm_args=jvm_args, username=username, game_args=list(game_args))
        log = log or default_log(layout)
        print(f"[run] log: {log}", flush=True)
        with keep_awake(timeout), virtual_display(log.with_suffix(".xvfb.log")) as env:
            return run_process(cmd, cwd=layout.minecraft_dir, log_file=log, timeout=timeout, env=env)


class Terminated(KeyboardInterrupt):
    """SIGTERM/SIGHUP (Windows: SIGBREAK) delivered as an interrupt, so cleanup runs; `signum` sets the exit code
    (128 + signum)."""

    def __init__(self, signum: int):
        super().__init__(f"signal {signum}")
        self.signum = signum


def _interrupt(signum: int, frame) -> None:
    raise Terminated(signum)


# SIGTERM, and the caller's terminal going away: SIGHUP, on Windows the console's Ctrl-Break (SIGBREAK)
TERMINATE_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, name))


@contextlib.contextmanager
def terminate_as_interrupt():
    """[TERMINATE_SIGNALS] (e.g. the caller's process group is terminated) raise [Terminated] in the main thread, so
    the client process trees are killed and the run's cleanup happens."""
    previous = {sig: signal.signal(sig, _interrupt) for sig in TERMINATE_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def recover_parked_mods(minecraft_dir: Path) -> None:
    """Moves exporter jars left in `mods-disabled/` by a launcher that was killed hard (SIGKILL) back to `mods/`."""
    disabled = minecraft_dir / "mods-disabled"
    if not disabled.is_dir():
        return
    for jar in disabled.glob(EXPORTER_MOD_GLOB):
        target = minecraft_dir / "mods" / jar.name
        if target.exists():
            jar.unlink()
        else:
            jar.replace(target)
            print(f"[run] recovered {target} from an earlier interrupted session")
    if not any(disabled.iterdir()):
        disabled.rmdir()


def java_home(tool: str) -> str:
    """JAVA_HOME of a mise Java install: `mise where`, or its Contents/Home on macOS (where the install is a bundle)."""
    home = Path(subprocess.run(["mise", "where", tool], check=True, capture_output=True, text=True).stdout.strip())
    return str(home / "Contents" / "Home" if (home / "Contents" / "Home").is_dir() else home)


def build_exporter_mod() -> Path:
    project = exporter_repo()
    if shutil.which("mise") is None:
        raise CliError("building the exporter mod needs mise and its Java toolchains (the container); pass --mod-jar "
                       "(gtnh-exporter/build/libs/bluemap-gtnh-exporter-<version>.jar)")
    homes = ",".join(java_home(t) for t in (ZULU8_TOOL, JAVA_TOOL))
    cmd = ["mise", "exec", JAVA_TOOL, "--", "./gradlew", f"-Porg.gradle.java.installations.paths={homes}", "build"]
    print(f"[export] building exporter mod: {subprocess.list2cmdline(cmd)} (cwd {project})", flush=True)
    if subprocess.run(cmd, cwd=project).returncode != 0:
        raise CliError("exporter mod build failed")
    jars = [
        p
        for p in (project / "build" / "libs").glob(EXPORTER_MOD_GLOB)
        if not p.stem.endswith(("-dev", "-sources"))
    ]
    if not jars:
        raise CliError(f"no {EXPORTER_MOD_GLOB} (excluding -dev/-sources) in {project / 'build' / 'libs'}")
    return max(jars, key=lambda p: p.stat().st_mtime)


def cmd_prepare(args: argparse.Namespace) -> int:
    prepare(make_layout(args.client_dir, args.gtnh_pack), args.zip or default_zip(args.gtnh_pack), clean=args.clean)
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    """Plays (without the exporter mod) and optionally auto-connects to a server via 1.7.10's `--server`/`--port`."""
    game_args: list[str] = []
    proxy = None
    if args.server:
        host, _, port = args.server.rpartition(":") if ":" in args.server else (args.server, "", "25565")
        if args.connect_delay > 0:
            # Forge 1.7.10 login race against fast servers, see gtnh_client.proxy
            proxy = DelayProxy(host, int(port), args.connect_delay).start()
            print(f"[play] connecting through 127.0.0.1:{proxy.port} (server replies held {args.connect_delay:g}s)")
            host, port = "127.0.0.1", str(proxy.port)
        game_args = ["--server", host, "--port", port]
    try:
        return run_client(
            make_layout(args.client_dir, args.gtnh_pack),
            zip_path=default_zip(args.gtnh_pack),
            mods=[],
            jvm_args=args.jvm_arg,
            xmx=args.xmx,
            timeout=args.timeout,
            username=args.username,
            log=args.log,
            game_args=game_args,
            disable_exporter=True,
        )
    finally:
        if proxy:
            proxy.close()


def cmd_run(args: argparse.Namespace) -> int:
    return run_client(
        make_layout(args.client_dir, args.gtnh_pack),
        zip_path=default_zip(args.gtnh_pack),
        mods=args.mod,
        jvm_args=args.jvm_arg,
        xmx=args.xmx,
        timeout=args.timeout,
        username=args.username,
        log=args.log,
    )


def cmd_export(args: argparse.Namespace) -> int:
    start_wall = time.time()
    t0 = time.monotonic()
    if args.mod_jar and not args.mod_jar.is_file():
        raise CliError(f"--mod-jar {args.mod_jar} does not exist")
    if args.extra_states and not args.extra_states.is_file():
        raise CliError(f"--extra-states {args.extra_states} does not exist")
    for dump in args.extra_states_dump:
        if not dump.is_file():
            raise CliError(f"--extra-states-dump {dump} does not exist")
    mod_jar = args.mod_jar.resolve() if args.mod_jar else build_exporter_mod()
    t_build = time.monotonic()
    layout = make_layout(args.client_dir, args.gtnh_pack)
    prepare(layout, default_zip(args.gtnh_pack), clean=args.clean)
    t_prepare = time.monotonic()
    out = (args.out or args.gtnh_pack.dir(workspace(), "out/exporter")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    jvm_args = [f"-Dbluemap.gtnh.exporter.out={out}"]
    if args.pack_suffix:
        jvm_args.append(f"-Dbluemap.gtnh.exporter.packSuffix={args.pack_suffix}")
    # the mod names the pack after the version the client reports itself (dreamcraft.cfg), as packs.client_version
    zip_name = packs.resources_zip(packs.client_version(layout.minecraft_dir) or args.gtnh_pack.version, args.pack_suffix)
    extra_files = [args.extra_states.resolve()] if args.extra_states else []
    if args.extra_states_dump:
        scanned = out / "extra-states.txt"
        count = coverage.write_extra_states(args.extra_states_dump, scanned)
        print(f"[export] {count} non-vanilla states from {len(args.extra_states_dump)} dump(s) -> {scanned}")
        extra_files.append(scanned)
    if extra_files:
        merged = out / "extra-states.merged.txt" if len(extra_files) > 1 else extra_files[0]
        if len(extra_files) > 1:
            merged.write_text("".join(f.read_text(encoding="utf-8") for f in extra_files), encoding="utf-8", newline="\n")
        jvm_args.append(f"-Dbluemap.gtnh.exporter.extraStates={merged}")
    code = run_client(
        layout, zip_path=default_zip(args.gtnh_pack), mods=[mod_jar], jvm_args=jvm_args, xmx="10G", timeout=1800,
        username="BlueMapExporter", log=None,
        extra_mods=[p.resolve() for p in args.extra_mod], resource_packs=[p.resolve() for p in args.resource_pack],
    )
    t_run = time.monotonic()
    split = (
        f"build {t_build - t0:.0f}s, prepare {t_prepare - t_build:.0f}s, run {t_run - t_prepare:.0f}s"
    )
    if code != 0:
        print(f"[export] FAILED: client exited with {code} after {t_run - t0:.0f}s ({split})", file=sys.stderr)
        return code
    problems = []
    for name in (EXPORT_REPORT, zip_name):
        path = out / name
        if not path.is_file():
            problems.append(f"{path} missing")
        elif path.stat().st_mtime < start_wall:
            problems.append(f"{path} is stale (older than this export)")
    if problems:
        print(f"[export] FAILED: {'; '.join(problems)} ({split})", file=sys.stderr)
        return 1
    print(f"[export] OK in {t_run - t0:.0f}s total ({split}); outputs in {out}")
    return 0


# Measured on Linux (32 cores, llvmpipe, 8 scenes / 81 cameras): N=1 300 s of client time, N=3 (--xmx 6G) 192 s; the
# largest scene alone (endgame, 118 s after a 70 s boot) bounds any larger N.
DEFAULT_PARALLEL = 3
LWJGL_EXTRACT_PATH = "org.lwjgl.system.SharedLibraryExtractPath"  # LWJGL 3 Configuration.SHARED_LIBRARY_EXTRACT_PATH


@dataclass
class WorkerRun:
    """One client of a parallel run: worker `index`'s instance and its JVM arguments; filled in by [run_workers]."""

    index: int
    layout: Layout
    jvm_args: list[str]
    jobs: list = field(default_factory=list)
    log: Path | None = None
    code: int | None = None
    rss_peak_kib: int = 0


def run_workers(
    runs: list[WorkerRun],
    *,
    zip_path: Path,
    mods: list[Path],
    extra_mods: list[Path],
    resource_packs: list[Path],
    xmx: str,
    timeout: float,
    username: str,
    kind: str,
    settings=None,
) -> list[int]:
    """Runs one client per worker at the same time ([client_session] for each, `settings(minecraft_dir)` around each
    run if given) and waits for all of them. Console lines are prefixed with `[w<i>]` when there is more than one
    worker; each worker's full log is `<worker>/logs-launcher/<kind>-<timestamp>.log`. Records each exit code and the
    peak resident memory of each client's process group. Each worker extracts LWJGL's natives into its own
    `<worker>/lwjgl-natives/`: with the shared default (`<tmp>/lwjgl_<user>/`) clients that start together race on the
    extraction and fail with `UnsatisfiedLinkError: Failed to locate library: libSDL3.so` (or `liblwjgl_opengl.so`)."""
    with terminate_as_interrupt(), contextlib.ExitStack() as stack:
        commands = []
        for run in runs:
            profile = stack.enter_context(client_session(
                run.layout, zip_path=zip_path, mods=mods, extra_mods=extra_mods, resource_packs=resource_packs))
            if settings is not None:
                stack.enter_context(settings(run.layout.minecraft_dir))
            jvm_args = [f"-D{LWJGL_EXTRACT_PATH}={run.layout.instance_dir / 'lwjgl-natives'}", *run.jvm_args]
            commands.append(build_command(run.layout, profile, xmx=xmx, jvm_args=jvm_args, username=username))
            run.log = default_log(run.layout, kind)
            print(f"[run] worker {run.index} log: {run.log}", flush=True)
        stack.enter_context(keep_awake(timeout))
        procs: list[ClientProcess] = []
        try:
            for run, cmd in zip(runs, commands):
                prefix = f"[w{run.index}] " if len(runs) > 1 else ""
                env = stack.enter_context(virtual_display(run.log.with_suffix(".xvfb.log")))
                procs.append(ClientProcess(cmd, cwd=run.layout.minecraft_dir, log_file=run.log, prefix=prefix, env=env))

            def sample() -> None:
                rss = tree_rss_kib([proc.pid for proc in procs])
                for run, proc in zip(runs, procs):
                    run.rss_peak_kib = max(run.rss_peak_kib, rss.get(proc.pid, 0))

            codes = run_processes(procs, timeout=timeout, sample=sample)
        except BaseException:
            for proc in procs:
                if proc.code is None:
                    proc.kill()
                    proc.finish(1)
            raise
    for run, code in zip(runs, codes):
        run.code = code
    return codes


def default_bmh_jar(pack: packs.Pack) -> Path:
    """The harness mod the scene server runs: the newest `bmh-*.jar` in `WS/servers/gtnh-<V>/mods/`."""
    mods = workspace() / "servers" / pack.server / "mods"
    jars = [p for p in mods.glob("bmh-*.jar") if not p.stem.endswith(("-dev", "-sources"))]
    if not jars:
        raise CliError(f"no bmh-*.jar in {mods}; pass --bmh-jar (the harness build, harness/build/libs/bmh-<v>.jar)")
    return max(jars, key=lambda p: p.stat().st_mtime)


def _read_fresh(path: Path, started_wall: float) -> dict | None:
    if not path.is_file() or path.stat().st_mtime < started_wall:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_cameras(args: argparse.Namespace) -> int:
    """Reference screenshots of scene worlds (`--jobs`): split the jobs across `--parallel` worker instances, copy
    each job's world into its worker's `saves/`, run the workers' clients in the exporter mod's `cameras` mode (one
    session each, the saves opened one after another), check every job's outputs."""
    started_wall = time.time()
    t0 = time.monotonic()
    jobs = cameras.read_jobs(args.jobs.resolve())
    for path in [*args.resource_pack, *args.extra_mod]:
        if not path.exists():
            raise CliError(f"{path} does not exist")
    if args.mod_jar and not args.mod_jar.is_file():
        raise CliError(f"--mod-jar {args.mod_jar} does not exist")
    if args.bmh_jar and not args.bmh_jar.is_file():
        raise CliError(f"--bmh-jar {args.bmh_jar} does not exist")
    if args.parallel < 1:
        raise CliError("--parallel must be at least 1")
    bmh_jar = (args.bmh_jar or default_bmh_jar(args.gtnh_pack)).resolve()
    mod_jar = args.mod_jar.resolve() if args.mod_jar else build_exporter_mod()
    t_build = time.monotonic()
    base = make_layout(args.client_dir, args.gtnh_pack)
    if not base.client_dir.exists():
        prepare(base, default_zip(args.gtnh_pack))
    runs: list[WorkerRun] = []
    codes: list[int] = []
    t_setup = t_run = time.monotonic()
    try:
        for i, group in enumerate(workers.split(jobs, args.parallel, cost=lambda job: job.cost), 1):
            layout, stats = workers.sync_worker(base, i)
            saves = cameras.prepare_saves(layout.minecraft_dir, group)
            for job in group:
                job.out.mkdir(parents=True, exist_ok=True)
                cameras.clean_outputs(job.out, job.cameras)
            session = layout.instance_dir / cameras.SESSION
            session.unlink(missing_ok=True)
            spec_file = layout.instance_dir / cameras.SPEC
            spec_file.write_text(json.dumps(cameras.build_spec(group, saves, session=session), indent=1) + "\n", encoding="utf-8")
            print(f"[cameras] worker {i}: {layout.instance_dir} ({stats.copied} entries copied, {stats.removed} removed "
                  f"to match {base.minecraft_dir}); {len(group)} job(s): {', '.join(job.scene for job in group)}", flush=True)
            runs.append(WorkerRun(i, layout, jvm_args=[
                "-Dbluemap.gtnh.exporter.mode=cameras",
                f"-Dbluemap.gtnh.exporter.cameras={spec_file}",
                "-Dfml.queryResult=confirm",  # answer FML's startup queries (e.g. missing blocks) without a GUI
            ], jobs=group))
        t_setup = time.monotonic()
        codes = run_workers(
            runs, zip_path=default_zip(args.gtnh_pack), mods=[mod_jar], extra_mods=[bmh_jar, *(p.resolve() for p in args.extra_mod)],
            resource_packs=[p.resolve() for p in args.resource_pack], xmx=args.xmx, timeout=args.timeout,
            username=cameras.USERNAME, kind="cameras", settings=cameras.capture_settings,
        )
        t_run = time.monotonic()
    finally:
        for run in runs:
            cameras.remove_saves(run.layout.minecraft_dir)
    failed = []
    for run in runs:
        session_file = run.layout.instance_dir / cameras.SESSION
        session = _read_fresh(session_file, started_wall)
        if session is not None:
            session["launcher"] = {"exit": run.code, "rssPeakMiB": round(run.rss_peak_kib / 1024), "log": str(run.log),
                                   "wallSeconds": round(t_run - t_setup, 1)}
            session_file.write_text(json.dumps(session, indent=1) + "\n", encoding="utf-8")
        boot = f"boot {session['bootSeconds']:.0f}s" if session else "no session report"
        session_error = session.get("error") if session else None
        error = f", session error: {session_error}" if session_error else ""
        print(f"[cameras] worker {run.index}: exit {run.code}, {boot}{error}, peak RSS {run.rss_peak_kib / 1024 / 1024:.1f} GiB, "
              f"log {run.log}")
        for job in run.jobs:
            report = _read_fresh(job.out / cameras.REPORT, started_wall) or {}
            timings = report.get("timings", {})
            print(f"[cameras]   {job.scene}: {report.get('seconds', 0):.0f}s (open {timings.get('open', 0):.0f}s, "
                  f"join {timings.get('join', 0):.0f}s, cameras {timings.get('cameras', 0):.0f}s)")
            for result in report.get("results", []):
                state = "captured" if result.get("captured") else f"FAILED: {result.get('error')}"
                flags = "" if not result.get("captured") else (
                    ("" if result.get("settled") else ", NOT settled") + ("" if result.get("stable") else ", NOT stable")
                )
                print(f"[cameras]     {result['name']}: {state}{flags} ({result.get('seconds', 0):.1f}s, "
                      f"{result.get('fps', 0):.0f} fps)")
            problems = cameras.check_outputs(job.out, job.cameras, started_wall, session_error)
            if problems:
                failed.append(job.scene)
                print(f"[cameras]   {job.scene} FAILED: {'; '.join(problems[:5])}", file=sys.stderr)
    split = f"build {t_build - t0:.0f}s, setup {t_setup - t_build:.0f}s, clients {t_run - t_setup:.0f}s"
    cameras_total = sum(len(job.cameras["cameras"]) for job in jobs)
    if failed or any(codes) or not codes:
        print(f"[cameras] FAILED: {len(failed)} of {len(jobs)} job(s) ({', '.join(failed) or 'see the client logs'}), "
              f"client exits {codes} ({split})", file=sys.stderr)
        return 1
    print(f"[cameras] OK: {len(jobs)} job(s), {cameras_total} cameras on {len(runs)} worker(s) in {time.monotonic() - t0:.0f}s "
          f"total ({split})")
    return 0


def cmd_states(args: argparse.Namespace) -> int:
    """Reference renders of every block state for the per-state oracle: write the state list (vanilla from the
    fork's flattening table, modded from the pack's `gtnh/blocks.ndjson`), run the client in the exporter mod's
    `states` mode (with `--parallel N`, N worker clients each draw a contiguous shard into the same output directory),
    check the outputs."""
    started_wall = time.time()
    t0 = time.monotonic()
    ws = workspace()
    variant_dir = args.gtnh_pack.dir(ws, "out/oracle") / args.variant / "states"
    out = (args.out or variant_dir / "client").resolve()
    pack = (args.states_from or args.gtnh_pack.dir(ws, "out/exporter") / packs.resources_zip(args.gtnh_pack.version)).resolve()
    for path in [pack, *args.resource_pack, *args.extra_mod]:
        if not path.exists():
            raise CliError(f"{path} does not exist")
    if args.mod_jar and not args.mod_jar.is_file():
        raise CliError(f"--mod-jar {args.mod_jar} does not exist")
    if args.parallel < 1:
        raise CliError("--parallel must be at least 1")
    flattening = bluemap_repo() / states.FLATTENING_TSV
    mod_jar = args.mod_jar.resolve() if args.mod_jar else build_exporter_mod()
    t_build = time.monotonic()
    base = make_layout(args.client_dir, args.gtnh_pack)
    if not base.client_dir.exists():
        prepare(base, default_zip(args.gtnh_pack))
    if out.exists() and not args.only:
        shutil.rmtree(out)  # a full run starts clean: images of states that are no longer listed must not survive
    out.mkdir(parents=True, exist_ok=True)
    (out / states.INDEX).unlink(missing_ok=True)
    states_file = out.parent / ("states.txt" if not args.only else "states-only.txt")
    count = states.write_states(pack, flattening, states_file, args.only)
    if count == 0:
        raise CliError(f"no state matches --only {args.only!r}")
    print(f"[states] {count} states from {flattening.name} and {pack.name} -> {states_file}", flush=True)
    common = ["-Dbluemap.gtnh.exporter.mode=states", f"-Dbluemap.gtnh.exporter.out={out}"]
    extra_mods = [p.resolve() for p in args.extra_mod]
    resource_packs = [p.resolve() for p in args.resource_pack]
    if args.parallel == 1:
        code = run_client(
            base, zip_path=default_zip(args.gtnh_pack), mods=[mod_jar],
            jvm_args=[*common, f"-Dbluemap.gtnh.exporter.states={states_file}"], xmx=args.xmx, timeout=args.timeout,
            username="BlueMapExporter", log=args.log, extra_mods=extra_mods, resource_packs=resource_packs,
        )
        codes = [code]
    else:
        runs = []
        indexes = []
        for i, shard in enumerate(states.shard_lines(states_file.read_text(encoding="utf-8"), args.parallel), 1):
            layout, _ = workers.sync_worker(base, i)
            shard_file = layout.instance_dir / "states-shard.txt"
            shard_file.write_text("".join(shard), encoding="utf-8", newline="\n")
            index = layout.instance_dir / "states-index.json"
            index.unlink(missing_ok=True)
            indexes.append(index)
            print(f"[states] worker {i}: {len(shard)} states -> {layout.instance_dir}", flush=True)
            runs.append(WorkerRun(i, layout, jvm_args=[
                *common, f"-Dbluemap.gtnh.exporter.states={shard_file}", f"-Dbluemap.gtnh.exporter.statesIndex={index}",
            ]))
        codes = run_workers(
            runs, zip_path=default_zip(args.gtnh_pack), mods=[mod_jar], extra_mods=extra_mods,
            resource_packs=resource_packs, xmx=args.xmx, timeout=args.timeout, username="BlueMapExporter", kind="states",
        )
        for run in runs:
            print(f"[states] worker {run.index}: exit {run.code}, peak RSS {run.rss_peak_kib / 1024 / 1024:.1f} GiB, log {run.log}")
        if not any(codes):
            states.merge_indexes(indexes, out / states.INDEX, started_wall)
    t_run = time.monotonic()
    split = f"build {t_build - t0:.0f}s, client boot+render {t_run - t_build:.0f}s"
    problems, summary = states.check_outputs(out, count, started_wall)
    if any(codes) or problems:
        print(f"[states] FAILED (client exit {codes}): {'; '.join(problems) or 'see the client log'} ({split})", file=sys.stderr)
        return next((c for c in codes if c), 1)
    print(f"[states] OK: {summary['states']} states ({summary['errors']} with errors, {summary['blank']} blank, "
          f"{summary['tesr']} with a TESR), render {summary['seconds']:.0f}s, {t_run - t0:.0f}s total ({split}); "
          f"outputs in {out}")
    return 0


def _join_dash_values(argv: list[str]) -> list[str]:
    """Let `--jvm-arg -Dfoo=bar` work: argparse would otherwise treat the value as an option."""
    joined: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--jvm-arg":
            value = next(it, None)
            joined.append(arg if value is None else f"--jvm-arg={value}")
        else:
            joined.append(arg)
    return joined


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtnh-client", description=f"Launch a GT New Horizons client without Prism (pack {packs.DEFAULT_VERSION} by default)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kwargs)
        p.add_argument("--pack-version", choices=list(packs.PACKS),
                       help="GTNH pack version of the default zip, client, server and output paths (default: the "
                            f"--client-dir's own version, else $BMGTNH_PACK_VERSION, else {packs.DEFAULT_VERSION})")
        return p

    def client_dir_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--client-dir", type=Path, help="client directory (default WS/clients/gtnh-<version>)")

    p = add_parser("prepare", help="unpack the instance zip and download libraries/assets")
    p.add_argument("--zip", type=Path, help="Prism instance zip (default WS/downloads/GT_New_Horizons_<version>_Java_17-26.zip)")
    client_dir_arg(p)
    p.add_argument("--clean", action="store_true", help="delete the client directory first")
    p.set_defaults(func=cmd_prepare)

    p = add_parser("run", help="launch the client (prepares first if the client directory is missing)")
    client_dir_arg(p)
    p.add_argument("--mod", type=Path, action="append", default=[], help="mod jar to copy into .minecraft/mods (repeatable)")
    p.add_argument("--jvm-arg", action="append", default=[], help="extra JVM argument (repeatable)")
    p.add_argument("--xmx", default="10G", help="heap size for -Xms/-Xmx (default %(default)s)")
    p.add_argument("--timeout", type=float, default=1800, help="kill the client after this many seconds, exit 124")
    p.add_argument("--username", default="BlueMapExporter", help="offline username (default %(default)s)")
    p.add_argument("--log", type=Path, help="log file (default <client-dir>/logs-launcher/client-<timestamp>.log)")
    p.set_defaults(func=cmd_run)

    p = add_parser("play", help="launch the client without the exporter mod, optionally auto-connecting to a server")
    client_dir_arg(p)
    p.add_argument("--server", help="host[:port] to connect to on start (1.7.10 --server/--port), e.g. 127.0.0.1:25567")
    p.add_argument("--connect-delay", type=float, default=2.0, help="connect through a local proxy that holds the server's first reply this many seconds, working around Forge's login race with fast local servers; 0 = direct (default %(default)s)")
    p.add_argument("--username", default="BlueMapPlayer", help="offline username (default %(default)s)")
    p.add_argument("--jvm-arg", action="append", default=[], help="extra JVM argument (repeatable)")
    p.add_argument("--xmx", default="10G", help="heap size for -Xms/-Xmx (default %(default)s)")
    p.add_argument("--timeout", type=float, default=1800, help="kill the client after this many seconds, exit 124")
    p.add_argument("--log", type=Path, help="log file (default <client-dir>/logs-launcher/client-<timestamp>.log)")
    p.set_defaults(func=cmd_play)

    p = add_parser("export", help="build the exporter mod, prepare, run it and verify the export outputs")
    client_dir_arg(p)
    p.add_argument("--clean", action="store_true", help="delete the client directory first")
    p.add_argument("--mod-jar", type=Path, help="use this exporter jar instead of building it with gradle")
    p.add_argument("--out", type=Path, help="export output directory (default WS/out/exporter-<version>)")
    p.add_argument("--extra-states", type=Path, help="extra block states file passed to the exporter ('<modid:name> <meta>' lines)")
    p.add_argument("--extra-states-dump", type=Path, action="append", default=[], help="bmh dump.ndjson (world scan) whose non-vanilla states are exported too; repeatable")
    p.add_argument("--resource-pack", type=Path, action="append", default=[], help="client resource pack (zip or dir) enabled for this export only, first = highest priority; repeatable")
    p.add_argument("--extra-mod", type=Path, action="append", default=[], help="extra client mod jar installed for this export only; repeatable")
    p.add_argument("--pack-suffix", help="output pack name suffix: bluemap-gtnh-resources-<version>-<suffix>.zip")
    p.set_defaults(func=cmd_export)

    p = add_parser("cameras", help="reference screenshots of scene worlds for the world-scene oracle (e2e oracle world)")
    client_dir_arg(p)
    p.add_argument("--jobs", type=Path, required=True, help='jobs file {"jobs": [{"scene", "world", "cameras", "out", "remove", "freeze_random_ticks"}]} (written by `e2e oracle world`)')
    p.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="worker clients (WS/clients/<client>-worker-<i>) the jobs are split across (default %(default)s)")
    p.add_argument("--resource-pack", type=Path, action="append", default=[], help="client resource pack enabled for this run only, first = highest priority; repeatable")
    p.add_argument("--extra-mod", type=Path, action="append", default=[], help="extra client mod jar installed for this run only; repeatable")
    p.add_argument("--mod-jar", type=Path, help="use this exporter jar instead of building it with gradle")
    p.add_argument("--bmh-jar", type=Path, help="harness mod jar loaded into the clients (default: the newest bmh-*.jar in WS/servers/gtnh-<version>/mods)")
    p.add_argument("--xmx", default="6G", help="heap size of each client (default %(default)s: about 9 GiB resident each, so 3 workers fit in 62 GB with room to spare)")
    p.add_argument("--timeout", type=float, default=3600, help="kill the clients after this many seconds, exit 124 (default %(default)s)")
    p.set_defaults(func=cmd_cameras)

    p = add_parser("states", help="reference renders of every block state for the per-state oracle (e2e oracle states)")
    client_dir_arg(p)
    p.add_argument("--variant", default="default", help="oracle variant, only for the default paths (default %(default)s)")
    p.add_argument("--out", type=Path, help="output dir (default WS/out/oracle[-<version>]/<variant>/states/client)")
    p.add_argument("--states-from", type=Path, help="exported pack whose gtnh/blocks.ndjson lists the modded states (default: the version's export, WS/out/exporter[-<version>]/bluemap-gtnh-resources-<version>.zip)")
    p.add_argument("--only", help="only states whose '<modid:name> <meta>' matches this regex (keeps the other outputs)")
    p.add_argument("--resource-pack", type=Path, action="append", default=[], help="client resource pack enabled for this run only, first = highest priority; repeatable")
    p.add_argument("--extra-mod", type=Path, action="append", default=[], help="extra client mod jar installed for this run only; repeatable")
    p.add_argument("--mod-jar", type=Path, help="use this exporter jar instead of building it with gradle")
    p.add_argument("--parallel", type=int, default=1, help="worker clients (WS/clients/<client>-worker-<i>) each drawing a contiguous shard (default %(default)s: the client itself)")
    p.add_argument("--xmx", default="10G", help="heap size of each client (default %(default)s; use 6G with --parallel)")
    p.add_argument("--timeout", type=float, default=3600, help="kill the client after this many seconds, exit 124 (default %(default)s)")
    p.add_argument("--log", type=Path, help="client log file with --parallel 1 (default <client-dir>/logs-launcher/client-<timestamp>.log)")
    p.set_defaults(func=cmd_states)

    def coverage_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scene", type=Path, help="scene output dir with dump.ndjson and scene.json (default WS/out/scenes[-<version>]/gtnh-zoo)")
        p.add_argument("--world", type=Path, help="world to render (default WS/worlds[-<version>]/gtnh-zoo)")
        p.add_argument("--pack", type=Path, help="exported resource pack (default WS/out/exporter[-<version>]/bluemap-gtnh-resources-<version>.zip)")
        p.add_argument("--out", type=Path, help="work/output dir (default WS/out/exporter[-<version>]/coverage)")
        p.add_argument("--no-build", action="store_true", help="use the existing CLI/addon jars instead of building them")
        p.add_argument("--port", type=int, default=8210, help="CLI webserver port (default %(default)s)")

    p = add_parser("coverage", help="render a scene world with the BlueMap CLI and compare rendered states with its dump")
    coverage_args(p)
    p.set_defaults(func=cmd_coverage)

    p = add_parser("screens", help="serve the coverage render and take headless Chromium screenshots")
    coverage_args(p)
    p.add_argument("--shots", type=Path, help="screenshot dir (default WS/out/exporter[-<version>]/screens)")
    p.add_argument("--view", action="append", default=[], metavar="NAME=HASH", help="view as name=<webapp url hash>; repeatable (default: a few zoo overviews)")
    p.set_defaults(func=cmd_screens)

    p = add_parser("serve", help="run client commands for callers that forward them (BMGTNH_CLIENT_HOST, the dev "
                                 "container) on this host; listens on 127.0.0.1 only")
    p.add_argument("--port", type=int, default=bridge.DEFAULT_PORT, help="port (default %(default)s)")
    p.add_argument("--log", type=Path, help="server log, appended to (default WS/out/gtnh-client-serve.log)")
    p.set_defaults(func=cmd_serve)
    return parser


def bluemap_jars(no_build: bool) -> tuple[Path, list[Path]]:
    """(CLI shadow jar, [legacy addon, gtnh-render addon]) from the BlueMap build, built from the current tree."""
    root = bluemap_repo()
    if not no_build:
        cmd = ["mise", "exec", "--", "./gradlew", ":cli:shadowJar", ":legacy:addonJar", ":gtnh-render:addonJar"]
        print(f"[coverage] building: {subprocess.list2cmdline(cmd)} (cwd {root})", flush=True)
        if subprocess.run(cmd, cwd=root).returncode != 0:
            raise CliError("BlueMap build failed")

    def newest(directory: Path, pattern: str) -> Path:
        jars = list(directory.glob(pattern))
        if not jars:
            raise CliError(f"no {pattern} in {directory}")
        return max(jars, key=lambda p: p.stat().st_mtime)

    cli = newest(root / "implementations" / "cli" / "build" / "libs", "cli-*-shadow.jar")
    addons = [newest(root / m / "build" / "libs", f"{m}-*-addon.jar") for m in ("legacy", "gtnh-render")]
    return cli, addons


def setup_coverage(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.pack.is_file():
        raise CliError(f"--pack {args.pack} does not exist (run `gtnh-client export` first)")
    cli, addons = bluemap_jars(args.no_build)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    scene = json.loads((args.scene / "scene.json").read_text()) if (args.scene / "scene.json").is_file() else {}
    coverage.write_config(out / "config", world=args.world.resolve(), packs=[*addons, args.pack.resolve()], box=scene.get("dump_box"), port=args.port)
    return cli, out


def cmd_coverage(args: argparse.Namespace) -> int:
    cli, out = setup_coverage(args)
    report = out / "bluemap-coverage-report.json"
    seconds = coverage.render(cli, out, report)
    result = coverage.compare(args.scene / "dump.ndjson", report, args.pack)
    result["renderSeconds"] = round(seconds, 1)
    (out / "coverage.json").write_text(json.dumps(result, indent=1))
    coverage.print_summary(result)
    print(f"[coverage] render {seconds:.0f}s; details in {out / 'coverage.json'}")
    return 0


DEFAULT_VIEWS = {
    # zoo grid spans x/z 0..256 at y 4..8; <map>:<x>:<y>:<z>:<distance>:<rotation>:<angle>:<tilt>:<ortho>:<mode>
    "overview": "overworld:128:5:128:260:0:0.8:0:0:perspective",
    "corner-0-0": "overworld:16:5:16:40:0.6:0.9:0:0:perspective",
    "gregtech": "overworld:{gx}:5:{gz}:40:0.6:0.9:0:0:perspective",
    "middle": "overworld:128:5:128:45:-0.6:0.9:0:0:perspective",
    "far-corner": "overworld:240:5:240:45:2.4:0.9:0:0:perspective",
}


def cmd_screens(args: argparse.Namespace) -> int:
    import socket
    import urllib.request

    cli, out = setup_coverage(args)
    if not (out / "web" / "maps").is_dir():
        raise CliError(f"no render in {out}; run `gtnh-client coverage` first")
    views = dict(DEFAULT_VIEWS)
    placed = (args.scene / "zoo.json")
    gx, gz = 128, 128
    if placed.is_file():
        for entry in json.loads(placed.read_text()).get("placed", []):
            if entry["name"].startswith("gregtech:"):
                gx, gz = entry["x"], entry["z"]
                break
    views = {k: v.format(gx=gx, gz=gz) for k, v in views.items()}
    for spec in args.view:
        name, _, hash_ = spec.partition("=")
        views[name] = hash_
    cmd = coverage.cli_command(cli, out / "config", extra_jvm=[], args=["-w"])
    print(f"[screens] {subprocess.list2cmdline(cmd)} (cwd {out})", flush=True)
    with (out / "webserver.log").open("w") as log:
        server = subprocess.Popen(cmd, cwd=out, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=1):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise CliError(f"webserver did not come up on 127.0.0.1:{args.port}, see {out / 'webserver.log'}")
                time.sleep(0.5)
        urllib.request.urlopen(f"http://127.0.0.1:{args.port}/", timeout=10).read()
        shots = [
            "uv", "run", "--with", "playwright", "python", "-m", "gtnh_client.screens",
            "--url", f"http://127.0.0.1:{args.port}/", "--out", str(args.shots.resolve()),
            "--views", json.dumps([{"name": k, "hash": v} for k, v in views.items()]),
        ]
        install = ["uv", "run", "--with", "playwright", "playwright", "install", "chromium"]
        project = exporter_repo() / "launcher"
        if subprocess.run(install, cwd=project).returncode != 0:
            raise CliError("playwright install chromium failed")
        return subprocess.run(shots, cwd=project).returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


def resolve_pack(args: argparse.Namespace) -> None:
    """Sets ``args.gtnh_pack`` and the defaults that depend on it. The pack version is ``--pack-version``, else the
    version the ``--client-dir`` reports itself (packs.client_version), else $BMGTNH_PACK_VERSION, else the default."""
    client_dir = getattr(args, "client_dir", None)
    own = packs.client_version(client_dir / ".minecraft") if client_dir else None
    if args.pack_version and own and own != args.pack_version:
        raise CliError(f"{client_dir} is a GTNH {own} client, not --pack-version {args.pack_version}")
    try:
        pack = packs.get(args.pack_version or (own if own in packs.PACKS else None))
    except ValueError as e:
        raise CliError(str(e)) from e
    args.gtnh_pack = pack
    ws = workspace()
    if args.command in ("coverage", "screens"):
        exporter = pack.dir(ws, "out/exporter")
        args.scene = args.scene or pack.dir(ws, "out/scenes") / "gtnh-zoo"
        args.world = args.world or pack.dir(ws, "worlds") / "gtnh-zoo"
        args.pack = args.pack or exporter / packs.resources_zip(pack.version)
        args.out = args.out or exporter / "coverage"
        if args.command == "screens":
            args.shots = args.shots or exporter / "screens"


# Commands that prepare or launch clients: each holds the workspace's client lock (gtnh_client.lock) for its whole run
# (`prepare` rewrites the natives a running client has loaded, which Windows does not allow).
CLIENT_COMMANDS = frozenset({"prepare", "run", "play", "export", "cameras", "states"})


def cmd_serve(args: argparse.Namespace) -> int:
    log_file = (args.log or workspace() / "out" / "gtnh-client-serve.log").resolve()
    return bridge.serve(args.port, workspace().resolve(), log_file)


def forward(args: argparse.Namespace, argv: list[str]) -> int:
    """Runs a client command on the host of $BMGTNH_CLIENT_HOST (gtnh_client.bridge). What needs this machine is done
    here first: the exporter mod build when there is no --mod-jar, and the default --bmh-jar of `cameras` (the scene
    server's, in a container volume: the bridge sends it along)."""
    argv = list(argv)
    if args.command in ("export", "cameras", "states") and args.mod_jar is None:
        args.mod_jar = build_exporter_mod()
        argv += ["--mod-jar", str(args.mod_jar)]
    if args.command == "cameras" and args.bmh_jar is None:
        args.bmh_jar = default_bmh_jar(args.gtnh_pack)
        argv += ["--bmh-jar", str(args.bmh_jar)]
    paths = [(f"--{name.replace('_', '-')}", item) for name, value in vars(args).items()
             for item in (value if isinstance(value, list) else [value]) if isinstance(item, Path)]
    return bridge.forward(os.environ[bridge.HOST_ENV], argv, root=PurePosixPath(workspace().resolve().as_posix()),
                          cwd=PurePosixPath(Path.cwd().as_posix()), paths=paths,
                          jobs_file=args.jobs if args.command == "cameras" else None)


def main(argv: list[str] | None = None) -> None:
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(_join_dash_values(raw))
    try:
        if os.environ.get(bridge.STOP_ENV):
            bridge.stop_on_eof()
        resolve_pack(args)
        if args.command in bridge.FORWARDED and os.environ.get(bridge.HOST_ENV):
            sys.exit(forward(args, raw))
        held = lock.client_lock(workspace()) if args.command in CLIENT_COMMANDS else contextlib.nullcontext()
        with held:
            code = args.func(args)
        sys.exit(code)
    except (CliError, cameras.CamerasError, states.StatesError, PrepareError, ProfileError, DownloadError, lock.LockBusy,
            LaunchError, PathMapError, bridge.BridgeError, FileNotFoundError, subprocess.CalledProcessError) as e:  # fmt: skip
        print(f"gtnh-client: error: {e}", file=sys.stderr)
        sys.exit(1)
