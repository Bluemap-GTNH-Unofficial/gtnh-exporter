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
