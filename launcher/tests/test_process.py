"""Process trees, Java selection and natives on every platform (Windows: taskkill, working sets, SIGBREAK)."""

import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from gtnh_client import launch
from gtnh_client.cli import TERMINATE_SIGNALS
from gtnh_client.instance import Layout, extract_natives
from gtnh_client.launch import ClientProcess, LaunchError, run_process, tree_rss_kib
from gtnh_client.profile import Artifact, GradleSpec, Library, Profile, RuntimeContext

# A child that starts a grandchild (both sleep), prints the grandchild's pid and sleeps.
PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def _grandchild(log: Path) -> int:
    for _ in range(100):
        lines = [line for line in log.read_text().splitlines() if line.strip().isdigit()] if log.is_file() else []
        if lines:
            return int(lines[0])
        time.sleep(0.1)
    raise AssertionError(f"no grandchild pid in {log}")


def test_kill_tree_kills_the_whole_tree(tmp_path: Path):
    proc = ClientProcess([sys.executable, "-c", PARENT], cwd=tmp_path, log_file=tmp_path / "c.log")
    grandchild = _grandchild(tmp_path / "c.log")
    assert alive(proc.pid) and alive(grandchild)
    proc.kill()
    proc.finish(1)
    assert gone(proc.pid) and gone(grandchild)


def test_timeout_kills_the_tree_with_124(tmp_path: Path):
    log = tmp_path / "c.log"
    assert run_process([sys.executable, "-c", PARENT], cwd=tmp_path, log_file=log, timeout=3) == launch.TIMEOUT_EXIT
    assert gone(_grandchild(log))
    assert log.read_text().splitlines()[-1].startswith("# exit 124")


def test_tree_rss_counts_the_tree(tmp_path: Path):
    proc = ClientProcess([sys.executable, "-c", PARENT], cwd=tmp_path, log_file=tmp_path / "c.log")
    try:
        _grandchild(tmp_path / "c.log")
        rss = tree_rss_kib([proc.pid])
        assert rss[proc.pid] > 2 * 1024  # two Python interpreters, several MiB each
    finally:
        proc.kill()
        proc.finish(1)


def test_terminate_signals_of_the_platform():
    names = {signal.Signals(s).name for s in TERMINATE_SIGNALS}
    assert names == ({"SIGTERM", "SIGBREAK"} if sys.platform == "win32" else {"SIGTERM", "SIGHUP"})


def _jdk(root: Path, version: str, implementor: str = "Eclipse Adoptium") -> Path:
    exe = root / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (root / "release").write_text(f'IMPLEMENTOR="{implementor}"\nJAVA_RUNTIME_VERSION="{version}"\n')
    return exe


@pytest.fixture
def java_env(monkeypatch, tmp_path):
    """No mise, no BMGTNH_JAVA, no JAVA_HOME, no pinned JDK; java_command's cache cleared around the test."""
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    monkeypatch.delenv("BMGTNH_JAVA", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(launch, "PINNED_JDK", tmp_path / "no-pinned-jdk")
    launch.java_command.cache_clear()
    yield monkeypatch
    launch.java_command.cache_clear()


def test_java_from_bmgtnh_java_jdk_dir(java_env, tmp_path, capsys):
    exe = _jdk(tmp_path / "temurin", "25.0.5+8-LTS")
    java_env.setenv("BMGTNH_JAVA", str(tmp_path / "temurin"))
    java_env.setattr(launch.shutil, "which", lambda name: "/usr/bin/mise")  # an explicit java wins over mise
    assert launch.java_command() == (str(exe),)
    out = capsys.readouterr().out
    assert "Eclipse Adoptium 25.0.5+8-LTS" in out and "WARNING" not in out


def test_java_warns_about_another_runtime(java_env, tmp_path, capsys):
    exe = _jdk(tmp_path / "zulu", "25+36-LTS", "Azul Systems, Inc.")
    java_env.setenv("JAVA_HOME", str(tmp_path / "zulu"))
    assert launch.java_command() == (str(exe),)
    assert "WARNING: not java@temurin-25.0.5+8.0.LTS" in capsys.readouterr().out


def test_java_order_mise_then_pinned_then_java_home(java_env, tmp_path):
    pinned = _jdk(tmp_path / "pinned", "25.0.5+8-LTS")
    java_env.setattr(launch, "PINNED_JDK", tmp_path / "pinned")
    _jdk(tmp_path / "home", "25.0.5+8-LTS")
    java_env.setenv("JAVA_HOME", str(tmp_path / "home"))
    assert launch.java_command() == (str(pinned),)
    launch.java_command.cache_clear()
    java_env.setattr(launch.shutil, "which", lambda name: "/usr/bin/mise")
    assert launch.java_command() == ("mise", "exec", launch.JAVA_TOOL, "--", "java")


def test_no_java_is_an_error(java_env):
    with pytest.raises(LaunchError, match="BMGTNH_JAVA"):
        launch.java_command()


def test_natives_are_extracted_for_the_host_and_recorded(tmp_path: Path):
    layout = Layout(tmp_path / "client", tmp_path / "cache")
    spec = GradleSpec("net.java.jinput", "jinput-platform", "2.0.5", "natives-test")
    jar = layout.libraries_cache / spec.path
    jar.parent.mkdir(parents=True)
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("jinput.dll", b"native")
        z.writestr("META-INF/MANIFEST.MF", b"")
    profile = Profile(natives=[Library(spec, Artifact(spec.path), native=True, extract_exclude=("META-INF/",))])
    assert layout.natives_platform() is None
    assert extract_natives(layout, profile) == 1
    assert (layout.natives_dir / "jinput.dll").read_bytes() == b"native"
    assert layout.natives_platform() == RuntimeContext.host().classifier
    assert json.loads((layout.natives_dir / ".gtnh-client-natives.json").read_text())["platform"] == layout.natives_platform()
