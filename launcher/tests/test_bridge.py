"""The client bridge (`gtnh-client serve` and BMGTNH_CLIENT_HOST forwarding): path mapping, the caller's checks, and a
server running a fake launcher (output streamed, exit code, the launcher stopped when the caller goes away)."""

import base64
import hashlib
import json
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from gtnh_client import bridge
from gtnh_client.pathmap import PathMap, PathMapError

CALLER = PurePosixPath("/caller/ws")  # not /ws: the container's own volumes under /ws must not matter here


def test_tokens_are_mapped_to_the_host(tmp_path: Path):
    pm = PathMap(str(CALLER), tmp_path)
    assert pm.token("/caller/ws/out/x") == str(tmp_path / "out" / "x")
    assert pm.token("--out=/caller/ws/out/../out/x") == "--out=" + str(tmp_path / "out" / "x")
    assert pm.token("-Dbluemap.out=/caller/ws/a") == "-Dbluemap.out=" + str(tmp_path / "a")
    assert pm.token("--jvm-arg=-Dk=/caller/ws") == "--jvm-arg=-Dk=" + str(tmp_path)
    assert pm.token("2.9.0") == "2.9.0" and pm.token("relative/x") == "relative/x"
    with pytest.raises(PathMapError, match="outside the workspace"):
        pm.token("/caller/wsx/a")
    with pytest.raises(PathMapError, match="outside the workspace"):
        pm.token("--mod=/tmp/a.jar")
    staged = {"/caller/ws/servers/bmh.jar": "C:\\staged\\bmh.jar"}
    assert pm.token("--bmh-jar=/caller/ws/servers/bmh.jar", staged) == "--bmh-jar=C:\\staged\\bmh.jar"
    assert pm.token("../servers/bmh.jar", staged, cwd="/caller/ws/e2e") == "C:\\staged\\bmh.jar"


def test_host_paths_in_output_become_caller_paths():
    pm = PathMap("/ws", Path(str(PureWindowsPath("D:/Codes/vibe/BlueMap-GTNH"))))
    line = b"[run] log: D:\\Codes\\vibe\\BlueMap-GTNH\\clients\\gtnh-2.9.0\\logs\\a.log; jar:file:/D:/Codes/vibe/BlueMap-GTNH/x.jar!\n"
    assert pm.to_remote(line) == b"[run] log: /ws/clients/gtnh-2.9.0/logs/a.log; jar:file://ws/x.jar!\n"
    assert pm.to_remote(b"d:\\codes\\vibe\\bluemap-gtnh done") == b"/ws done"
    assert pm.to_remote(b"D:\\Codes\\vibe\\BlueMap-GTNH-other\\a") == b"D:\\Codes\\vibe\\BlueMap-GTNH-other\\a"


MOUNTINFO = """\
24 1 0:22 / / rw,relatime - overlay overlay rw
40 24 0:51 / /caller/ws rw,relatime - 9p drvfs rw
41 40 8:32 /volumes/servers /caller/ws/servers rw,relatime - ext4 /dev/sdc rw
42 40 8:32 /volumes/o /caller/ws/out/oracle\\0402 rw,relatime - ext4 /dev/sdc rw
43 24 0:5 / /dev rw - tmpfs tmpfs rw
"""


def test_volume_mounts_below_the_root():
    assert bridge.volume_mounts(CALLER, MOUNTINFO) == [CALLER / "servers", CALLER / "out" / "oracle 2"]


def test_check_paths_uploads_files_on_volumes_and_rejects_the_rest(tmp_path: Path):
    root = PurePosixPath(tmp_path.as_posix())
    volume = root / "servers"
    jar = tmp_path / "servers" / "bmh-1.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"jar bytes")
    uploads = bridge.check_paths([("--bmh-jar", root / "servers" / "bmh-1.jar"), ("--out", root / "out")],
                                 root=root, mounts=[volume], upload=True)
    assert [(u["arg"], u["name"], base64.b64decode(u["data"])) for u in uploads] == [
        (str(root / "servers" / "bmh-1.jar"), "bmh-1.jar", b"jar bytes")]
    with pytest.raises(bridge.BridgeError, match="container volume"):
        bridge.check_paths([("--out", volume / "x")], root=root, mounts=[volume], upload=True)
    with pytest.raises(bridge.BridgeError, match="container volume"):
        bridge.check_paths([("job 0 world", root / "servers" / "bmh-1.jar")], root=root, mounts=[volume], upload=False)
    with pytest.raises(bridge.BridgeError, match="outside the workspace"):
        bridge.check_paths([("--mod-jar", PurePosixPath("/elsewhere/a.jar"))], root=root, mounts=[], upload=True)


def test_jobs_request_resolves_like_the_launcher_and_sends_volume_inputs(tmp_path: Path):
    root = PurePosixPath(tmp_path.as_posix())
    volume = root / "out" / "oracle"  # like out/oracle-<V>, where e2e writes the camera lists
    (tmp_path / "out" / "oracle" / "s").mkdir(parents=True)
    (tmp_path / "out" / "oracle" / "s" / "cameras.json").write_text('{"format": 1}')
    (tmp_path / "logs").mkdir()
    jobs = {"jobs": [{"scene": "s", "world": "../worlds/s", "cameras": "../out/oracle/s/cameras.json",
                      "out": "../out/ref/s", "remove": [[1, 2, 3]]}]}
    (tmp_path / "logs" / "jobs.json").write_text(json.dumps(jobs))
    request, uploads = bridge.jobs_request(root / "logs" / "jobs.json", root=root, mounts=[volume])
    assert request["path"] == str(root / "logs" / "jobs.json")
    assert request["content"]["jobs"] == [{"scene": "s", "world": str(root / "worlds" / "s"),
                                           "cameras": str(volume / "s" / "cameras.json"),
                                           "out": str(root / "out" / "ref" / "s"), "remove": [[1, 2, 3]]}]
    assert [(u["arg"], base64.b64decode(u["data"])) for u in uploads] == [
        (str(volume / "s" / "cameras.json"), b'{"format": 1}')]
    jobs["jobs"][0]["out"] = "../out/oracle/s"  # an output (or a world) on a volume cannot be sent along
    (tmp_path / "logs" / "jobs.json").write_text(json.dumps(jobs))
    with pytest.raises(bridge.BridgeError, match="job 0 out: .* container volume"):
        bridge.jobs_request(root / "logs" / "jobs.json", root=root, mounts=[volume])


def _server(tmp_path: Path, monkeypatch, script: str) -> tuple[bridge.Server, str]:
    fake = tmp_path / "fake_launcher.py"
    fake.write_text(textwrap.dedent(script), encoding="utf-8")
    host = tmp_path / "host"
    (host / "sub").mkdir(parents=True)
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "stopped.txt"))
    monkeypatch.setenv("FAKE_SEEN", str(tmp_path / "seen.json"))
    monkeypatch.setattr(bridge, "HEARTBEAT", 0.5)
    monkeypatch.setattr(bridge, "STOP_GRACE", 20.0)
    server = bridge.Server(0, host, tmp_path / "serve.log", command=[sys.executable, str(fake)])
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


ECHO = """
    import json, os, sys
    with open(os.environ["FAKE_SEEN"], "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "ws": os.environ.get("BMGTNH_WORKSPACE"),
                   "stop": os.environ.get("BMGTNH_STOP_ON_EOF")}, f)
    print("starting", flush=True)
    print("log at " + os.path.join(os.getcwd(), "x.log"), flush=True)
    sys.exit(7)
"""


def test_forward_streams_output_maps_paths_and_returns_the_exit_code(tmp_path, monkeypatch, capfd):
    server, url = _server(tmp_path, monkeypatch, ECHO)
    requests = []
    prepare = server.prepare
    server.prepare = lambda request: requests.append(request) or prepare(request)
    try:
        monkeypatch.setenv("BMGTNH_WORKSPACE", str(CALLER))
        monkeypatch.setenv("BMGTNH_JAVA", "/opt/java/bin/java")  # the caller's java: never forwarded
        code = bridge.forward(url, ["run", "--log", "/caller/ws/sub/run.log", "--timeout", "5"], root=CALLER,
                              cwd=CALLER / "sub", paths=[("--log", PurePosixPath("/caller/ws/sub/run.log"))])
        assert code == 7
        assert requests[0]["env"]["BMGTNH_WORKSPACE"] == str(CALLER) and "BMGTNH_JAVA" not in requests[0]["env"]
        out = [line for line in capfd.readouterr().out.splitlines() if not line[:4].isdigit()]  # not the server log
        assert out[0].startswith("[bridge] gtnh-client run --log /caller/ws/sub/run.log")
        assert out[1] == "starting"
        seen = json.loads((tmp_path / "seen.json").read_text())
        host = tmp_path / "host"
        assert seen["argv"] == ["run", "--log", str(host / "sub" / "run.log"), "--timeout", "5"]
        assert Path(seen["cwd"]) == host / "sub"
        assert seen["ws"] == str(host) and seen["stop"] == "1"
        assert out[2] == "log at /caller/ws/sub/x.log"  # host paths written back as caller paths
    finally:
        server.shutdown()
        server.server_close()


def test_output_text_is_always_valid_text():
    assert bridge.output_text("ünï 中文\n".encode()) == "ünï 中文\n"
    assert bridge.output_text("中文\r\n".encode("gbk"), "gbk") == "中文\r\n"  # a Windows JVM or DLL in the code page
    assert bridge.output_text(b"bad \xff\x80 end\n", "gbk") == "bad \ufffd\ufffd end\n"
    assert bridge.output_text(b"bad \xff\n", "no-such-codec") == "bad \ufffd\n"


BYTES = """
    import sys
    sys.stdout.buffer.write("utf8 ünï\\n".encode())
    sys.stdout.buffer.write("gbk 中文\\r\\n".encode("gbk"))
    sys.stdout.buffer.write(b"bad \\xff\\x80 end\\n")
    sys.stdout.buffer.flush()
"""


def test_forward_relays_only_utf8(tmp_path, monkeypatch, capfdbinary):
    server, url = _server(tmp_path, monkeypatch, BYTES)
    monkeypatch.setattr(bridge, "OUTPUT_FALLBACK", "gbk")  # the Chinese Windows host's code page
    try:
        assert bridge.forward(url, ["run"], root=CALLER, cwd=CALLER / "sub", paths=[]) == 0
        text = capfdbinary.readouterr().out.decode("utf-8")  # strict: raises on any byte that is not UTF-8
        lines = [line for line in text.splitlines() if not line.startswith("[bridge]")]
        assert lines == ["utf8 ünï", "gbk 中文", "bad \ufffd\ufffd end"]
    finally:
        server.shutdown()
        server.server_close()


def test_server_refuses_what_it_cannot_run(tmp_path, monkeypatch):
    server, url = _server(tmp_path, monkeypatch, ECHO)
    try:
        with pytest.raises(bridge.BridgeError, match="not a client command"):
            bridge.forward(url, ["coverage"], root=CALLER, cwd=CALLER, paths=[])
        with pytest.raises(bridge.BridgeError, match="outside the workspace"):
            bridge.forward(url, ["run", "--jvm-arg=-Dx=/tmp/y"], root=CALLER, cwd=CALLER, paths=[])
        with pytest.raises(bridge.BridgeError, match="not a directory of the shared workspace"):
            bridge.forward(url, ["run"], root=CALLER, cwd=CALLER / "missing", paths=[])
    finally:
        server.shutdown()
        server.server_close()


def test_uploads_are_staged_on_the_host(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, ECHO)
    server.staging = tmp_path / "staging"
    data = b"bmh jar"
    try:
        argv, _, _, _ = server.prepare({
            "argv": ["cameras", "--bmh-jar", "/caller/ws/servers/bmh-1.jar"], "cwd": "/caller/ws/sub",
            "root": str(CALLER), "env": {},
            "uploads": [{"arg": "/caller/ws/servers/bmh-1.jar", "name": "bmh-1.jar",
                         "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode()}]})
        staged = Path(argv[2])
        assert staged.name == "bmh-1.jar" and staged.read_bytes() == data and staged.is_relative_to(server.staging)
    finally:
        server.shutdown()
        server.server_close()


def test_jobs_file_is_rewritten_with_host_paths_and_uploaded_inputs(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, ECHO)
    server.staging = tmp_path / "staging"
    host = tmp_path / "host"
    cameras = b'{"format": 1, "cameras": []}'
    try:
        argv, _, _, _ = server.prepare({
            "argv": ["cameras", "--jobs", "../logs/jobs.json"], "cwd": "/caller/ws/sub", "root": str(CALLER),
            "env": {},
            "uploads": [{"arg": "/caller/ws/out/oracle/s/cameras.json", "name": "cameras.json",
                         "sha256": hashlib.sha256(cameras).hexdigest(), "data": base64.b64encode(cameras).decode()}],
            "jobs": {"path": "/caller/ws/logs/jobs.json", "content": {"jobs": [
                {"scene": "s", "world": "/caller/ws/worlds/s", "cameras": "/caller/ws/out/oracle/s/cameras.json",
                 "out": "/caller/ws/out/ref/s", "remove": []}]}}})
        jobs_copy = Path(argv[2])
        assert jobs_copy.name == "jobs.json" and jobs_copy.is_relative_to(server.staging)
        (job,) = json.loads(jobs_copy.read_text(encoding="utf-8"))["jobs"]
        assert job["world"] == str(host / "worlds" / "s") and job["out"] == str(host / "out" / "ref" / "s")
        assert Path(job["cameras"]).read_bytes() == cameras and Path(job["cameras"]).is_relative_to(server.staging)
    finally:
        server.shutdown()
        server.server_close()


def test_no_server_fails_fast():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens once closed
    started = time.monotonic()
    with pytest.raises(bridge.BridgeError, match="no gtnh-client server"):
        bridge.forward(f"http://127.0.0.1:{port}", ["run"], root=CALLER, cwd=CALLER, paths=[])
    assert time.monotonic() - started < bridge.CONNECT_TIMEOUT + 2


SLEEPER = """
    import os, sys, time
    from pathlib import Path
    from gtnh_client import bridge
    from gtnh_client.cli import terminate_as_interrupt
    bridge.stop_on_eof()
    try:
        with terminate_as_interrupt():
            print("sleeping", flush=True)
            for _ in range(600):
                time.sleep(0.1)
    except KeyboardInterrupt as e:
        Path(os.environ["FAKE_MARKER"]).write_text(str(getattr(e, "signum", "int")))
        sys.exit(143)
"""

CALLER_SCRIPT = """
import sys
from pathlib import PurePosixPath
from gtnh_client import bridge
sys.exit(bridge.forward(sys.argv[1], ["run"], root=PurePosixPath("/caller/ws"), cwd=PurePosixPath("/caller/ws/sub"),
                        paths=[]))
"""


def test_caller_going_away_stops_the_launcher_with_its_cleanup(tmp_path, monkeypatch):
    server, url = _server(tmp_path, monkeypatch, SLEEPER)
    try:
        caller = subprocess.Popen([sys.executable, "-c", CALLER_SCRIPT, url], stdout=subprocess.PIPE)
        assert caller.stdout is not None
        caller.stdout.readline()  # [bridge] ...
        assert caller.stdout.readline().strip() == b"sleeping"
        assert len(server.active) == 1
        (launcher,) = server.active.values()
        caller.kill()  # like a timed-out or killed caller: the connection just closes
        caller.wait()
        launcher.wait(timeout=30)
        marker = tmp_path / "stopped.txt"
        assert marker.read_text() == "15"  # stopped through its SIGTERM handler: the run's cleanup happened
        assert "caller gone" in (tmp_path / "serve.log").read_text()
    finally:
        server.shutdown()
        server.server_close()
