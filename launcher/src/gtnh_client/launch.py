"""Build the java command line for a resolved profile and run the client with log tee + timeout."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from gtnh_client.instance import Layout
from gtnh_client.profile import Profile, classpath

JAVA_TOOL = "java@temurin-25.0.5+8.0.LTS"
JAVA_CMD = ["mise", "exec", JAVA_TOOL, "--", "java"]
EXPORTER_MOD_PREFIX = "bluemap-gtnh-exporter"
TIMEOUT_EXIT = 124

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


def install_mods(minecraft_dir: Path, jars: list[Path]) -> None:
    mods = minecraft_dir / "mods"
    for jar in jars:
        if not jar.is_file():
            raise FileNotFoundError(f"mod jar {jar} does not exist")
        target = mods / jar.name
        if jar.name.startswith(EXPORTER_MOD_PREFIX):
            for old in mods.glob(f"{EXPORTER_MOD_PREFIX}*.jar"):
                if old.resolve() != jar.resolve():
                    old.unlink()
        if target.resolve() != jar.resolve():
            shutil.copy2(jar, target)
        print(f"[run] installed mod {target}")


def build_command(
    layout: Layout, profile: Profile, *, xmx: str, jvm_args: list[str], username: str
) -> list[str]:
    assert profile.minecraft_arguments is not None and profile.main_class is not None
    cmd = list(JAVA_CMD)
    if "FirstThreadOnMacOS" in profile.traits:
        cmd.append("-XstartOnFirstThread")
    cmd += [f"-Xms{xmx}", f"-Xmx{xmx}", *profile.jvm_args, *jvm_args]
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
    return cmd


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tree(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL, the whole process group (mise wrapper + java)."""
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


def run_process(cmd: list[str], *, cwd: Path, log_file: Path, timeout: float) -> int:
    """Run `cmd` in its own process group, tee merged stdout/stderr to the console and `log_file`."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("wb") as log:
        header = f"# cwd: {cwd}\n# cmd: {subprocess.list2cmdline(cmd)}\n".encode()
        log.write(header)
        log.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        assert proc.stdout is not None

        def pump() -> None:
            for line in proc.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                log.write(line)
                log.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        start = time.monotonic()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[run] timeout after {timeout:.0f}s; killing process tree", flush=True)
            _kill_tree(proc)
            code = TIMEOUT_EXIT
        except KeyboardInterrupt:
            print("[run] interrupted; killing process tree", flush=True)
            _kill_tree(proc)
            code = 130
        reader.join(timeout=10)
        log.write(f"# exit {code} after {time.monotonic() - start:.1f}s\n".encode())
    return code
