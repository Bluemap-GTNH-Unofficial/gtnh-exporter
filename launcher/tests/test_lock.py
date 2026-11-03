import json
import subprocess
import sys
import textwrap
import time

import pytest

from gtnh_client import lock

HOLD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from gtnh_client import lock
    with lock.client_lock(Path(sys.argv[1]), "holder test"):
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
""")


def _hold(ws, seconds):
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(ws), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_holder_record_written_and_cleared(tmp_path):
    with lock.client_lock(tmp_path, "gtnh-client cameras --jobs j.json") as path:
        assert path == (tmp_path / "locks" / "client.lock").resolve()
        holder = lock.read_holder(path)
        assert holder["command"] == "gtnh-client cameras --jobs j.json"
        assert holder["pid"] > 0
        with lock.client_lock(tmp_path, "nested"):  # re-entrant in one process
            assert lock.read_holder(path)["command"] == "gtnh-client cameras --jobs j.json"
    assert lock.read_holder(path) is None


def test_busy_lock_names_the_holder(tmp_path):
    proc = _hold(tmp_path, 30)
    try:
        with pytest.raises(lock.LockBusy, match=r"pid \d+ .*holder test"):
            with lock.client_lock(tmp_path, "second", wait=0):
                pass
    finally:
        proc.kill()
        proc.wait()
    with lock.client_lock(tmp_path, "after a killed holder", wait=0) as path:  # the kernel released it
        assert lock.read_holder(path)["command"] == "after a killed holder"


def test_waiter_queues_until_free(tmp_path, capsys):
    proc = _hold(tmp_path, 1.5)
    started = time.monotonic()
    with lock.client_lock(tmp_path, "waiter", wait=30) as path:
        assert time.monotonic() - started >= 0.5
        assert json.loads(path.read_text())["command"] == "waiter"
    proc.wait()
    assert "is held by pid" in capsys.readouterr().err
