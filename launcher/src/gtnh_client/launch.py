"""Build the java command line for a resolved profile and run the client with log tee + timeout."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from gtnh_client.instance import Layout
from gtnh_client.profile import Profile, classpath

JAVA_TOOL = "java@temurin-25.0.5+8.0.LTS"
JAVA_ENV = "BMGTNH_JAVA"
# JAVA_TOOL's runtime where there is no mise (the Windows host): unpacked from Adoptium's zip into this directory,
# and its `release` file's JAVA_RUNTIME_VERSION / IMPLEMENTOR.
PINNED_JDK = Path.home() / ".jdks" / "temurin-25.0.5+8"
PINNED_RUNTIME = ("25.0.5+8", "Eclipse Adoptium")
EXPORTER_MOD_CLASS = "de/bluecolored/bluemap/gtnh/exporter/ExporterMod.class"
TIMEOUT_EXIT = 124


class LaunchError(Exception):
    pass


def _java_exe(path: Path, source: str) -> Path:
    exe = path if path.is_file() else path / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    if not exe.is_file():
        raise LaunchError(f"{source} {path}: no java there (expected a java executable or a JDK directory)")
    return exe


def jdk_release(exe: Path) -> dict[str, str]:
    """The `release` file of the JDK `exe` belongs to (JAVA_RUNTIME_VERSION, IMPLEMENTOR, ...), empty if it has none."""
    try:
        lines = (exe.parent.parent / "release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return {k: v.strip().strip('"') for k, sep, v in (line.partition("=") for line in lines) if sep}


@functools.cache
def java_command() -> tuple[str, ...]:
    """The java the clients run on: $BMGTNH_JAVA (a java executable or a JDK directory); else JAVA_TOOL through mise
    when mise is on PATH (the container, macOS); else the JDK in PINNED_JDK (the Windows host); else $JAVA_HOME. A java
    that is not JAVA_TOOL's runtime is used with a warning: the oracle's reference captures depend on it."""
    explicit = os.environ.get(JAVA_ENV)
    if not explicit and shutil.which("mise"):
        return ("mise", "exec", JAVA_TOOL, "--", "java")
    if explicit:
        exe = _java_exe(Path(explicit), JAVA_ENV)
    elif PINNED_JDK.is_dir():
        exe = _java_exe(PINNED_JDK, "pinned JDK")
    elif os.environ.get("JAVA_HOME"):
        exe = _java_exe(Path(os.environ["JAVA_HOME"]), "JAVA_HOME")
    else:
        raise LaunchError(f"no java: set {JAVA_ENV} to a Temurin {PINNED_RUNTIME[0]} JDK, unpack one into {PINNED_JDK} "
                          "or install mise")
    release = jdk_release(exe)
    runtime = (release.get("JAVA_RUNTIME_VERSION", "?"), release.get("IMPLEMENTOR", "?"))
    pinned = runtime[0].startswith(PINNED_RUNTIME[0]) and runtime[1] == PINNED_RUNTIME[1]
    warning = "" if pinned else f"; WARNING: not {JAVA_TOOL}, client renders may differ from the references"
    print(f"[run] java {exe} ({runtime[1]} {runtime[0]}){warning}", flush=True)
    return (str(exe),)


# lwjgl3ify 3.0.37 me.eigenraven.lwjgl3ify.core.Config, category "window": SDL window creation flags
# (focused -> SDL_WINDOW_INPUT_FOCUS|SDL_WINDOW_MOUSE_FOCUS, iconified -> SDL_WINDOW_MINIMIZED).
LWJGL3IFY_WINDOW = {"iconified": True, "focused": False}
# Vanilla 1.7.10 GameSettings: master volume 0 mutes every sound category.
MUTE_OPTION = ("soundCategory_master", "0.0")


def offline_uuid(username: str) -> str:
    """Java UUID.nameUUIDFromBytes("OfflinePlayer:<name>") (MD5, version 3, no namespace), without dashes."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return digest.hex()


def set_forge_booleans(text: str, category: str, values: dict[str, bool]) -> str:
    """Set `B:<key>=` entries inside a Forge Configuration category block, adding block/keys if absent."""
    lines = "".join(f"    B:{k}={str(v).lower()}\n" for k, v in values.items())
    block = re.search(rf"^{re.escape(category)} \{{\n(.*?)^\}}", text, re.M | re.S)
    if block is None:
        prefix = text.rstrip("\n") + "\n\n" if text.strip() else "# Configuration file\n\n"
        return f"{prefix}{category} {{\n{lines}}}\n\n"
    body = block.group(1)
    for key, value in values.items():
        body, n = re.subn(rf"^(\s*)B:{re.escape(key)}=.*$", rf"\g<1>B:{key}={str(value).lower()}", body, flags=re.M)
        if n == 0:
            body += f"    B:{key}={str(value).lower()}\n"
    return text[: block.start(1)] + body + text[block.end(1) :]


def configure_lwjgl3ify(minecraft_dir: Path) -> Path:
    cfg = minecraft_dir / "config" / "lwjgl3ify.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    text = cfg.read_text() if cfg.is_file() else ""
    cfg.write_text(set_forge_booleans(text, "window", LWJGL3IFY_WINDOW))
    return cfg


def mute_sound(minecraft_dir: Path) -> Path:
    """Set the master volume to 0 in options.txt, keeping every other option."""
    options = minecraft_dir / "options.txt"
    key, value = MUTE_OPTION
    lines = options.read_text().splitlines() if options.is_file() else []
    lines = [line for line in lines if not line.startswith(f"{key}:")] + [f"{key}:{value}"]
    options.write_text("\n".join(lines) + "\n")
    return options


def is_exporter_mod(jar: Path) -> bool:
    """Whether `jar` is a build of the exporter mod, whatever its file name."""
    try:
        with zipfile.ZipFile(jar) as z:
            return EXPORTER_MOD_CLASS in z.NameToInfo
    except (OSError, zipfile.BadZipFile):
        return False


def install_mods(minecraft_dir: Path, jars: list[Path]) -> None:
    """Copies `jars` into `mods/`. An exporter mod jar replaces every other exporter jar there, whatever its name: FML
    loads one copy of a mod and disables the others ("Duplicate mod found"), not necessarily the new one."""
    mods = minecraft_dir / "mods"
    for jar in jars:
        if not jar.is_file():
            raise FileNotFoundError(f"mod jar {jar} does not exist")
        target = mods / jar.name
        if is_exporter_mod(jar):
            for old in mods.glob("*.jar"):
                if old.name != jar.name and is_exporter_mod(old):
                    old.unlink()
                    print(f"[run] removed {old} (another exporter mod jar)")
        if target.resolve() != jar.resolve():
            shutil.copy2(jar, target)
        print(f"[run] installed mod {target}")


def install_resource_packs(minecraft_dir: Path, packs: list[Path]) -> tuple[list[Path], str | None]:
    """Copies zip/dir resource packs into `resourcepacks/` and enables exactly them, in the given order (first = top
    priority; the 1.7 options list is ordered bottom to top, so it is written reversed). Returns the copied paths and
    the previous `resourcePacks:` options line (None if absent) for [restore_resource_packs]."""
    target_dir = minecraft_dir / "resourcepacks"
    target_dir.mkdir(exist_ok=True)
    installed = []
    for pack in packs:
        if not pack.exists():
            raise FileNotFoundError(f"resource pack {pack} does not exist")
        target = target_dir / pack.name
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.copytree(pack, target) if pack.is_dir() else shutil.copy2(pack, target)
        installed.append(target)
        print(f"[run] installed resource pack {target}")
    options = minecraft_dir / "options.txt"
    lines = options.read_text().splitlines() if options.is_file() else []
    previous = next((line for line in lines if line.startswith("resourcePacks:")), None)
    if packs:
        names = json.dumps([p.name for p in reversed(packs)])
        lines = [line for line in lines if not line.startswith("resourcePacks:")] + [f"resourcePacks:{names}"]
        options.write_text("\n".join(lines) + "\n")
    return installed, previous


def restore_resource_packs(minecraft_dir: Path, installed: list[Path], previous: str | None) -> None:
    """Undoes [install_resource_packs]: deletes the copies and restores the previous `resourcePacks:` line."""
    if not installed:
        return
    for path in installed:
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
    options = minecraft_dir / "options.txt"
    lines = [line for line in options.read_text().splitlines() if not line.startswith("resourcePacks:")]
    if previous is not None:
        lines.append(previous)
    options.write_text("\n".join(lines) + "\n")
    print(f"[run] removed {len(installed)} resource pack(s), options.txt resourcePacks restored")


def build_command(
    layout: Layout, profile: Profile, *, xmx: str, jvm_args: list[str], username: str, game_args: list[str] = ()
) -> list[str]:
    assert profile.minecraft_arguments is not None and profile.main_class is not None
    cmd = list(java_command())
    if "FirstThreadOnMacOS" in profile.traits and sys.platform == "darwin":
        cmd.append("-XstartOnFirstThread")
    # stdout/stderr in UTF-8 whatever the host's code page (Java 18+ default them to the native encoding, GBK on a
    # Chinese Windows); file.encoding is UTF-8 already (Java 18+ default, and lwjgl3ify's +jvmArgs set it)
    cmd += [f"-Xms{xmx}", f"-Xmx{xmx}", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", *profile.jvm_args,
            *jvm_args]
    if profile.natives:
        cmd.append(f"-Djava.library.path={layout.natives_dir}")
    cp = classpath(profile, layout.libraries_cache, layout.local_libraries)
    cmd += ["-cp", os.pathsep.join(str(p) for p in cp), profile.main_class]
    index_id = profile.asset_index["id"] if profile.asset_index else "legacy"
    substitutions = {
        "auth_player_name": username,
        "version_name": "1.7.10",
        "game_directory": str(layout.minecraft_dir),
        "assets_root": str(layout.assets_dir),
        "assets_index_name": index_id,
        "auth_uuid": offline_uuid(username),
        "auth_access_token": "0",
        "user_properties": "{}",
        "user_type": "legacy",
    }
    for token in profile.minecraft_arguments.split():
        def substitute(m: re.Match) -> str:
            if m.group(1) not in substitutions:
                raise ValueError(f"unknown minecraftArguments placeholder {m.group(0)}")
            return substitutions[m.group(1)]

        cmd.append(re.sub(r"\$\{(\w+)\}", substitute, token))
    for tweaker in profile.tweakers:
        cmd += ["--tweakClass", tweaker]
    cmd += game_args
    return cmd


def own_group() -> dict:
    """Popen arguments that start a process as the root of its own tree for [kill_tree]: a new session (process group)
    on POSIX; on Windows a new process group (the console's Ctrl-C does not reach it, the launcher kills it) without a
    console window of its own (a launcher without a console, e.g. under `gtnh-client serve`, would open one)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(proc: subprocess.Popen) -> None:
    """Kills a process started with [own_group] and everything it started: on POSIX SIGTERM, then SIGKILL after 15 s,
    to its process group (mise wrapper + java); on Windows `taskkill /T /F` (there is no SIGTERM for a java without a
    console; the tree is walked while its root is still alive)."""
    if sys.platform == "win32":
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"[run] taskkill {proc.pid} failed ({e}); killing the process only", flush=True)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return
    pgid = proc.pid  # start_new_session: the child leads its own group
    for sig, grace in ((signal.SIGTERM, 15.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()  # reap our direct child so it does not keep the group alive as a zombie
            if not _group_alive(pgid):
                break
            time.sleep(0.2)
        else:
            continue
        break
    proc.wait()


class ClientProcess:
    """A client command as its own process tree ([own_group]); merged stdout/stderr go to `log_file` and, each line
    prefixed with `prefix`, to the console."""

    def __init__(
        self, cmd: list[str], *, cwd: Path, log_file: Path, prefix: str = "", env: dict[str, str] | None = None
    ) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = log_file
        self._log = log_file.open("wb")
        display = f"# DISPLAY: {env['DISPLAY']}\n" if env and "DISPLAY" in env else ""
        self._log.write(f"# cwd: {cwd}\n# cmd: {subprocess.list2cmdline(cmd)}\n{display}".encode())
        self._log.flush()
        self._prefix = prefix.encode()
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **own_group())
        self.code: int | None = None
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        console: dict = {}
        for line in self.proc.stdout:
            _console_write(self._prefix + line, console)
            self._log.write(line)
            self._log.flush()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll()

    def kill(self) -> None:
        kill_tree(self.proc)

    def finish(self, code: int) -> int:
        """Records the exit code in the log and closes it."""
        self.code = code
        self._reader.join(timeout=10)
        self._log.write(f"# exit {code} after {time.monotonic() - self.started:.1f}s\n".encode())
        self._log.close()
        return code


def run_process(
    cmd: list[str], *, cwd: Path, log_file: Path, timeout: float, env: dict[str, str] | None = None
) -> int:
    """Run `cmd` in its own process group, tee merged stdout/stderr to the console and `log_file`."""
    return run_processes([ClientProcess(cmd, cwd=cwd, log_file=log_file, env=env)], timeout=timeout)[0]


def run_processes(procs: list[ClientProcess], *, timeout: float, sample: Callable[[], None] | None = None) -> list[int]:
    """Waits for every process. When `timeout` expires every process tree still running is killed ([kill_tree]) with
    exit code 124; Ctrl-C / SIGTERM / SIGHUP / SIGBREAK (as KeyboardInterrupt) kill them all with 128 + the signal
    number. `sample` is called about every 2 s while they run."""
    deadline = min(p.started for p in procs) + timeout
    next_sample = 0.0
    try:
        while True:
            running = [p for p in procs if p.code is None]
            for p in running:
                code = p.poll()
                if code is not None:
                    p.finish(code)
            running = [p for p in procs if p.code is None]
            if not running:
                break
            if time.monotonic() > deadline:
                print(f"[run] timeout after {timeout:.0f}s; killing {len(running)} process tree(s)", flush=True)
                for p in running:
                    p.kill()
                    p.finish(TIMEOUT_EXIT)
                break
            if sample is not None and time.monotonic() >= next_sample:
                sample()
                next_sample = time.monotonic() + 2.0
            time.sleep(0.2)
    except KeyboardInterrupt as interrupt:
        print("[run] interrupted; killing process tree(s)", flush=True)
        # 128 + signal number: 130 for Ctrl-C (SIGINT), 143 for SIGTERM, 129 for SIGHUP
        code = 128 + getattr(interrupt, "signum", signal.SIGINT)
        for p in procs:
            if p.code is None:
                p.kill()
                p.finish(code)
    return [p.code if p.code is not None else 1 for p in procs]


def tree_rss_kib(roots: list[int]) -> dict[int, int]:
    """Resident memory in KiB of each process tree started by [ClientProcess] (`roots`: their pids): the sum over its
    process group (`ps -A -o pgid=,rss=`), on Windows the working sets of the tree; empty if it cannot be read."""
    if sys.platform == "win32":
        from gtnh_client import winproc

        try:
            return winproc.tree_working_set_kib(roots)
        except OSError:
            return {}
    try:
        out = subprocess.run(["ps", "-A", "-o", "pgid=,rss="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    totals: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            totals[int(parts[0])] = totals.get(int(parts[0]), 0) + int(parts[1])
    return {root: totals[root] for root in roots if root in totals}  # start_new_session: pgid == the root's pid


def _console_write(line: bytes, state: dict) -> None:
    """Writes to the console until it fails (e.g. `| head` closed the pipe); afterwards only the log is written,
    so the client never blocks on a full stdout pipe."""
    if state.get("closed"):
        return
    try:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError, ValueError):
        state["closed"] = True


CAFFEINATE_FLAGS = ["-d", "-i", "-m", "-s", "-u"]


@contextlib.contextmanager
def keep_awake(seconds: float):
    """On macOS, holds `caffeinate -dimsu` while the client runs: with the display asleep the client's GL loop is
    throttled (SDL_GL_SwapWindow/frame pacing) so much that Forge's login handshake times out. `-u` also wakes the
    display (it needs `-t`, so the assertion is bounded by the client timeout plus a minute). On Windows the system is
    kept from sleeping (SetThreadExecutionState ES_SYSTEM_REQUIRED) for the block. No-op elsewhere."""
    if sys.platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
        try:
            yield None
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)
        return
    exe = shutil.which("caffeinate") if sys.platform == "darwin" else None
    if exe is None:
        yield None
        return
    proc = subprocess.Popen(
        [exe, *CAFFEINATE_FLAGS, "-t", str(int(seconds) + 60)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[run] caffeinate {' '.join(CAFFEINATE_FLAGS)} (pid {proc.pid}) keeps the display awake", flush=True)
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


XVFB_SCREEN = "1920x1080x24"  # larger than any capture size (the oracle resizes the client to its camera, 1280x720)
XVFB_READY_TIMEOUT = 30.0


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextlib.contextmanager
def virtual_display(log_file: Path):
    """On Linux without a DISPLAY (a container), a private Xvfb server for one client; yields the environment to start
    the client with. With a DISPLAY, or off Linux, yields None (inherit). One server per client keeps parallel workers
    apart: each has its own GL context and its own input focus. Xvfb's own output goes to `log_file`; a server that
    is not ready within XVFB_READY_TIMEOUT seconds is killed and the run fails instead of waiting."""
    if sys.platform != "linux" or os.environ.get("DISPLAY"):
        yield None
        return
    exe = shutil.which("Xvfb")
    if exe is None:
        raise RuntimeError("no DISPLAY and no Xvfb on PATH (install xvfb, or set DISPLAY)")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    with log_file.open("wb") as log:
        proc = subprocess.Popen(
            [exe, "-displayfd", str(write_fd), "-screen", "0", XVFB_SCREEN, "-nolisten", "tcp", "+extension", "GLX",
             "-noreset"],
            pass_fds=(write_fd,), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    os.close(write_fd)
    number = b""
    deadline = time.monotonic() + XVFB_READY_TIMEOUT
    with os.fdopen(read_fd, "rb") as ready:  # Xvfb writes its display number once it accepts connections
        while not number.endswith(b"\n") and time.monotonic() < deadline:
            if select.select([ready], [], [], 1.0)[0]:
                chunk = os.read(ready.fileno(), 64)
                if not chunk:
                    break  # Xvfb exited
                number += chunk
    if not number.endswith(b"\n"):
        exited = proc.poll()
        _stop(proc)
        why = f"exited with {exited}" if exited is not None else f"not ready after {XVFB_READY_TIMEOUT:.0f}s"
        raise RuntimeError(f"Xvfb {why}; see {log_file}")
    display = f":{number.decode().strip()}"
    print(f"[run] Xvfb {display} ({XVFB_SCREEN}, pid {proc.pid}, log {log_file})", flush=True)
    try:
        yield {**os.environ, "DISPLAY": display}
    finally:
        _stop(proc)
