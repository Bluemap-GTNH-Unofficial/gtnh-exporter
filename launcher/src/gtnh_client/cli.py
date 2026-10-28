"""`gtnh-client` command line: prepare / run / export / coverage / screens."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from gtnh_client import coverage
from gtnh_client.proxy import DelayProxy
from gtnh_client.download import DownloadError
from gtnh_client.instance import Layout, PrepareError, ensure_downloads, prepare
from gtnh_client.launch import (
    JAVA_TOOL,
    LWJGL3IFY_WINDOW,
    MUTE_OPTION,
    build_command,
    configure_lwjgl3ify,
    install_mods,
    install_resource_packs,
    keep_awake,
    mute_sound,
    restore_resource_packs,
    run_process,
)
from gtnh_client.profile import ProfileError

PACK_VERSION = "2.9.0-RC-2"
ZULU8_TOOL = "java@zulu-8.98.0.107"
EXPORTER_MOD_GLOB = "bluemap-gtnh-exporter-*.jar"
RESOURCES_ZIP = f"bluemap-gtnh-resources-{PACK_VERSION}.zip"
EXPORT_REPORT = "export-report.json"


class CliError(Exception):
    pass


def workspace() -> Path:
    env = os.environ.get("BMGTNH_WORKSPACE")
    # src/gtnh_client/cli.py -> launcher -> gtnh-exporter -> tools -> BlueMap-GTNH -> workspace
    return Path(env) if env else Path(__file__).resolve().parents[6]


def default_zip() -> Path:
    return workspace() / "downloads" / f"GT_New_Horizons_{PACK_VERSION}_Java_17-26.zip"


def default_client_dir() -> Path:
    return workspace() / "clients" / f"gtnh-{PACK_VERSION}"


def exporter_project() -> Path:
    return workspace() / "BlueMap-GTNH" / "tools" / "gtnh-exporter"


def make_layout(client_dir: Path | None) -> Layout:
    return Layout((client_dir or default_client_dir()).resolve(), (workspace() / "downloads" / "mc-cache").resolve())


def run_client(
    layout: Layout,
    *,
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
    """`extra_mods` and `resource_packs` are installed for this run only and removed afterwards (options.txt
    `resourcePacks` restored), so later default runs are unaffected. `disable_exporter` parks the exporter mod in
    `.minecraft/mods-disabled/` for the session (it would export and quit) and puts it back afterwards."""
    if not layout.client_dir.exists():
        prepare(layout, default_zip())
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
    # SIGTERM/SIGHUP (e.g. the caller's process group is terminated) must also kill the client and run the cleanup
    # below; run_process kills the Java process tree on KeyboardInterrupt
    previous_handlers = {sig: signal.signal(sig, _interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        cfg = configure_lwjgl3ify(layout.minecraft_dir)
        print(f"[run] {cfg}: window " + ", ".join(f"{k}={str(v).lower()}" for k, v in LWJGL3IFY_WINDOW.items()))
        print(f"[run] {mute_sound(layout.minecraft_dir)}: {MUTE_OPTION[0]}:{MUTE_OPTION[1]} (muted)")
        cmd = build_command(layout, profile, xmx=xmx, jvm_args=jvm_args, username=username, game_args=list(game_args))
        log = log or layout.client_dir / "logs-launcher" / f"client-{datetime.now():%Y%m%d-%H%M%S}.log"
        print(f"[run] log: {log}", flush=True)
        with keep_awake(timeout):
            return run_process(cmd, cwd=layout.minecraft_dir, log_file=log, timeout=timeout)
    finally:
        restore_resource_packs(layout.minecraft_dir, installed_packs, previous_packs)
        for jar in temporary_mods:
            jar.unlink(missing_ok=True)
            print(f"[run] removed extra mod {jar}")
        for parked_jar, original in parked:
            parked_jar.replace(original)
            print(f"[run] restored {original}")
        recover_parked_mods(layout.minecraft_dir)  # removes the then empty mods-disabled/
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


class Terminated(KeyboardInterrupt):
    """SIGTERM/SIGHUP delivered as an interrupt, so cleanup runs; `signum` sets the exit code (128 + signum)."""

    def __init__(self, signum: int):
        super().__init__(f"signal {signum}")
        self.signum = signum


def _interrupt(signum: int, frame) -> None:
    raise Terminated(signum)


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


def mise_where(tool: str) -> str:
    return subprocess.run(["mise", "where", tool], check=True, capture_output=True, text=True).stdout.strip()


def build_exporter_mod() -> Path:
    project = exporter_project()
    homes = ",".join(f"{mise_where(t)}/Contents/Home" for t in (ZULU8_TOOL, JAVA_TOOL))
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
    prepare(make_layout(args.client_dir), args.zip, clean=args.clean)
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
            make_layout(args.client_dir),
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
        make_layout(args.client_dir),
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
    layout = make_layout(args.client_dir)
    prepare(layout, default_zip(), clean=args.clean)
    t_prepare = time.monotonic()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    jvm_args = [f"-Dbluemap.gtnh.exporter.out={out}"]
    if args.pack_suffix:
        jvm_args.append(f"-Dbluemap.gtnh.exporter.packSuffix={args.pack_suffix}")
    zip_name = RESOURCES_ZIP if not args.pack_suffix else RESOURCES_ZIP.removesuffix(".zip") + f"-{args.pack_suffix}.zip"
    extra_files = [args.extra_states.resolve()] if args.extra_states else []
    if args.extra_states_dump:
        scanned = out / "extra-states.txt"
        count = coverage.write_extra_states(args.extra_states_dump, scanned)
        print(f"[export] {count} non-vanilla states from {len(args.extra_states_dump)} dump(s) -> {scanned}")
        extra_files.append(scanned)
    if extra_files:
        merged = out / "extra-states.merged.txt" if len(extra_files) > 1 else extra_files[0]
        if len(extra_files) > 1:
            merged.write_text("".join(f.read_text() for f in extra_files))
        jvm_args.append(f"-Dbluemap.gtnh.exporter.extraStates={merged}")
    code = run_client(
        layout, mods=[mod_jar], jvm_args=jvm_args, xmx="10G", timeout=1800, username="BlueMapExporter", log=None,
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
    ws = workspace()
    parser = argparse.ArgumentParser(prog="gtnh-client", description=f"Launch the GTNH {PACK_VERSION} client without Prism.")
    sub = parser.add_subparsers(dest="command", required=True)

    def client_dir_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--client-dir", type=Path, help=f"client directory (default {default_client_dir()})")

    p = sub.add_parser("prepare", help="unpack the instance zip and download libraries/assets")
    p.add_argument("--zip", type=Path, default=default_zip(), help="Prism instance zip (default %(default)s)")
    client_dir_arg(p)
    p.add_argument("--clean", action="store_true", help="delete the client directory first")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("run", help="launch the client (prepares first if the client directory is missing)")
    client_dir_arg(p)
    p.add_argument("--mod", type=Path, action="append", default=[], help="mod jar to copy into .minecraft/mods (repeatable)")
    p.add_argument("--jvm-arg", action="append", default=[], help="extra JVM argument (repeatable)")
    p.add_argument("--xmx", default="10G", help="heap size for -Xms/-Xmx (default %(default)s)")
    p.add_argument("--timeout", type=float, default=1800, help="kill the client after this many seconds, exit 124")
    p.add_argument("--username", default="BlueMapExporter", help="offline username (default %(default)s)")
    p.add_argument("--log", type=Path, help="log file (default <client-dir>/logs-launcher/client-<timestamp>.log)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("play", help="launch the client without the exporter mod, optionally auto-connecting to a server")
    client_dir_arg(p)
    p.add_argument("--server", help="host[:port] to connect to on start (1.7.10 --server/--port), e.g. 127.0.0.1:25567")
    p.add_argument("--connect-delay", type=float, default=2.0, help="connect through a local proxy that holds the server's first reply this many seconds, working around Forge's login race with fast local servers; 0 = direct (default %(default)s)")
    p.add_argument("--username", default="BlueMapPlayer", help="offline username (default %(default)s)")
    p.add_argument("--jvm-arg", action="append", default=[], help="extra JVM argument (repeatable)")
    p.add_argument("--xmx", default="10G", help="heap size for -Xms/-Xmx (default %(default)s)")
    p.add_argument("--timeout", type=float, default=1800, help="kill the client after this many seconds, exit 124")
    p.add_argument("--log", type=Path, help="log file (default <client-dir>/logs-launcher/client-<timestamp>.log)")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("export", help="build the exporter mod, prepare, run it and verify the export outputs")
    client_dir_arg(p)
    p.add_argument("--clean", action="store_true", help="delete the client directory first")
    p.add_argument("--mod-jar", type=Path, help="use this exporter jar instead of building it with gradle")
    p.add_argument("--out", type=Path, default=ws / "out" / "exporter", help="export output directory (default %(default)s)")
    p.add_argument("--extra-states", type=Path, help="extra block states file passed to the exporter ('<modid:name> <meta>' lines)")
    p.add_argument("--extra-states-dump", type=Path, action="append", default=[], help="bmh dump.ndjson (world scan) whose non-vanilla states are exported too; repeatable")
    p.add_argument("--resource-pack", type=Path, action="append", default=[], help="client resource pack (zip or dir) enabled for this export only, first = highest priority; repeatable")
    p.add_argument("--extra-mod", type=Path, action="append", default=[], help="extra client mod jar installed for this export only; repeatable")
    p.add_argument("--pack-suffix", help="output pack name suffix: bluemap-gtnh-resources-<version>-<suffix>.zip")
    p.set_defaults(func=cmd_export)

    def coverage_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scene", type=Path, default=ws / "out" / "scenes" / "gtnh-zoo", help="scene output dir with dump.ndjson and scene.json (default %(default)s)")
        p.add_argument("--world", type=Path, default=ws / "worlds" / "gtnh-zoo", help="world to render (default %(default)s)")
        p.add_argument("--pack", type=Path, default=ws / "out" / "exporter" / RESOURCES_ZIP, help="exported resource pack (default %(default)s)")
        p.add_argument("--out", type=Path, default=ws / "out" / "exporter" / "coverage", help="work/output dir (default %(default)s)")
        p.add_argument("--no-build", action="store_true", help="use the existing CLI/addon jars instead of building them")
        p.add_argument("--port", type=int, default=8210, help="CLI webserver port (default %(default)s)")

    p = sub.add_parser("coverage", help="render a scene world with the BlueMap CLI and compare rendered states with its dump")
    coverage_args(p)
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("screens", help="serve the coverage render and take headless Chromium screenshots")
    coverage_args(p)
    p.add_argument("--shots", type=Path, default=ws / "out" / "exporter" / "screens", help="screenshot dir (default %(default)s)")
    p.add_argument("--view", action="append", default=[], metavar="NAME=HASH", help="view as name=<webapp url hash>; repeatable (default: a few zoo overviews)")
    p.set_defaults(func=cmd_screens)
    return parser


def bluemap_jars(no_build: bool) -> tuple[Path, list[Path]]:
    """(CLI shadow jar, [legacy addon, gtnh-render addon]) from the BlueMap build, built from the current tree."""
    root = exporter_project().parent.parent
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
        project = Path(__file__).resolve().parents[2]
        if subprocess.run(install, cwd=project).returncode != 0:
            raise CliError("playwright install chromium failed")
        return subprocess.run(shots, cwd=project).returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(_join_dash_values(sys.argv[1:] if argv is None else argv))
    try:
        sys.exit(args.func(args))
    except (CliError, PrepareError, ProfileError, DownloadError, FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"gtnh-client: error: {e}", file=sys.stderr)
        sys.exit(1)
