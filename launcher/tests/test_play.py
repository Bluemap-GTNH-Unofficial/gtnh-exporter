import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from gtnh_client.cli import recover_parked_mods
from gtnh_client.proxy import DelayProxy


def test_delay_proxy_holds_first_server_reply_then_streams():
    server = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = server.accept()
        with conn:
            request = conn.recv(100)
            conn.sendall(b"reply:" + request)
            conn.sendall(b"|more")

    threading.Thread(target=serve, daemon=True).start()
    proxy = DelayProxy("127.0.0.1", server.getsockname()[1], 0.5).start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as client:
            start = time.monotonic()
            client.sendall(b"hello")
            data = b""
            while len(data) < len(b"reply:hello|more"):
                chunk = client.recv(100)
                if not chunk:
                    break
                data += chunk
            elapsed = time.monotonic() - start
        assert data == b"reply:hello|more"
        assert elapsed >= 0.45
    finally:
        proxy.close()
        server.close()


def test_recover_parked_mods(tmp_path: Path):
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods-disabled").mkdir()
    (tmp_path / "mods-disabled" / "bluemap-gtnh-exporter-0.1.0.jar").write_text("jar")
    recover_parked_mods(tmp_path)
    assert (tmp_path / "mods" / "bluemap-gtnh-exporter-0.1.0.jar").read_text() == "jar"
    assert not (tmp_path / "mods-disabled").exists()


SCRIPT = """
import signal, sys, pathlib
from gtnh_client.cli import _interrupt
from gtnh_client.launch import run_process
signal.signal(signal.SIGTERM, _interrupt)
code = run_process(["sleep", "60"], cwd=pathlib.Path("."), log_file=pathlib.Path(sys.argv[1]), timeout=120)
print("exit", code, flush=True)
"""


def test_sigterm_kills_client_tree_and_returns(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT, str(tmp_path / "c.log")], stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    child = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
    assert child, "sleep child not started"
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert "exit 143" in out
    for pid in child:
        try:
            os.kill(int(pid), 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive


def _fake_caffeinate(tmp_path: Path, monkeypatch) -> Path:
    """A `caffeinate` on PATH that records its pid and arguments, then sleeps until killed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "caffeinate.txt"
    script = bin_dir / "caffeinate"
    script.write_text(f'#!/bin/sh\necho "$$ $*" > {record}\nexec sleep 600\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(sys, "platform", "darwin")
    return record


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a reaped-later zombie still answers kill(0); check its state
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_for(path: Path) -> str:
    for _ in range(50):
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.1)
    raise AssertionError(f"{path} not written")


def test_keep_awake_holds_caffeinate_for_the_block(tmp_path: Path, monkeypatch):
    from gtnh_client.launch import keep_awake

    record = _fake_caffeinate(tmp_path, monkeypatch)
    with keep_awake(100) as proc:
        pid, *args = _wait_for(record).split()
        assert int(pid) == proc.pid and _alive(proc.pid)
        assert args == ["-d", "-i", "-m", "-s", "-u", "-t", "160"]
    assert not _alive(int(pid))


def test_keep_awake_stops_caffeinate_on_interrupt(tmp_path: Path, monkeypatch):
    from gtnh_client.launch import keep_awake

    record = _fake_caffeinate(tmp_path, monkeypatch)
    try:
        with keep_awake(10):
            pid = int(_wait_for(record).split()[0])
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    assert not _alive(pid)


def test_keep_awake_is_a_no_op_off_macos(monkeypatch):
    from gtnh_client.launch import keep_awake

    monkeypatch.setattr(sys, "platform", "linux")
    with keep_awake(10) as proc:
        assert proc is None
