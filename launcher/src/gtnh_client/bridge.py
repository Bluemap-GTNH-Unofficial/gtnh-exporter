"""Client commands for a caller that should not run clients itself: `gtnh-client serve` runs them on this host (the
Windows host with the GPU), and a caller (the dev container) forwards them when `BMGTNH_CLIENT_HOST` is set, e.g.
`BMGTNH_CLIENT_HOST=http://host.wslc.internal:8390`.

Forwarded: the commands that prepare or launch clients ([FORWARDED]). The caller first does what needs its own tools
(builds the exporter mod when there is no `--mod-jar`, picks the default `--bmh-jar` of `cameras`), checks that every
path it passes is in the workspace the host shares (the bind-mounted checkout, not a container volume over it; a file
on a volume, such as the scene server's bmh jar, is sent along instead), then sends argv, cwd, its workspace root and
its BMGTNH_* variables. The server maps the caller's paths to its own workspace (argv, environment, and the paths in a
`cameras --jobs` file, of which it writes a copy with host paths), runs `gtnh-client <argv>` there and streams the output
back line by line, host paths written as caller paths; the caller exits with the command's exit code. When the caller
goes away (killed, timed out, Ctrl-C) the server closes the launcher's stdin, which stops it like SIGTERM (client trees
killed, cleanup done; `BMGTNH_STOP_ON_EOF`), and kills its tree after STOP_GRACE seconds if it is still running.

Protocol (HTTP, the server listens on 127.0.0.1 only): `POST /run` with the JSON body {argv, cwd, root, env, uploads}
answers 400 {"error"} for a command it cannot run, else 200 and NDJSON lines: {"o": <output line as text, see
[output_text]>} (stdout and stderr merged), {"hb": 1} every HEARTBEAT seconds while the command is quiet, finally
{"exit": <code>}. The caller writes the text as UTF-8, so it never relays bytes that are not UTF-8 (mise refuses them).
`GET /health` answers {"ok": true, ...}.
"""

from __future__ import annotations

import _thread
import base64
import hashlib
import http.client
import itertools
import json
import locale
import os
import posixpath
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath, PurePosixPath
from urllib.parse import urlsplit

from gtnh_client.launch import java_command, kill_tree, own_group
from gtnh_client.pathmap import PathMap, PathMapError

HOST_ENV = "BMGTNH_CLIENT_HOST"
STOP_ENV = "BMGTNH_STOP_ON_EOF"
DEFAULT_PORT = 8390
FORWARDED = frozenset({"prepare", "run", "play", "export", "cameras", "states"})
# Caller variables that name things of the caller's machine, never forwarded.
LOCAL_ENV = frozenset({HOST_ENV, "BMGTNH_JAVA", STOP_ENV})
CONNECT_TIMEOUT = 5.0
HEARTBEAT = 5.0
READ_TIMEOUT = 120.0  # a caller that hears nothing (not even a heartbeat) for this long gives up
STOP_GRACE = 90.0  # seconds a stopped launcher gets for its cleanup before its tree is killed
UPLOAD_LIMIT = 256 << 20
# What a host program may print in besides UTF-8: the ANSI code page (cp936 on a Chinese Windows), whatever Python's
# UTF-8 mode says, for native code in the client (drivers, SDL) and anything else that ignores stdout.encoding.
OUTPUT_FALLBACK = locale.getencoding()


def output_text(line: bytes, fallback: str | None = None) -> str:
    """The text of a line the launcher, its java or native code in the client printed: UTF-8; else the host's code page
    (`fallback`, default OUTPUT_FALLBACK); else UTF-8 with U+FFFD for the bytes that are neither."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return line.decode(fallback or OUTPUT_FALLBACK)
    except (UnicodeDecodeError, LookupError):
        return line.decode("utf-8", "replace")


class BridgeError(Exception):
    pass


# ---------------------------------------------------------------- caller


def volume_mounts(root: PurePosixPath, mountinfo: str | None = None) -> list[PurePosixPath]:
    """Mount points strictly below the workspace root (container volumes over the bind-mounted checkout): what is under
    them is not what the host sees at the same place. From /proc/self/mountinfo; empty off Linux."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return []
    mounts = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4:
            point = PurePosixPath(fields[4].encode().decode("unicode_escape"))  # \040 for a space, as in fstab
            if point != root and root in point.parents:
                mounts.append(point)
    return mounts


def check_paths(paths: list[tuple[str, PurePosixPath]], *, root: PurePosixPath, mounts: list[PurePosixPath],
                upload: bool) -> list[dict]:
    """Checks that the host can read each path: it must be under the workspace root and, unless it is a file that can
    be sent along (`upload`), not on a container volume. Returns the uploads, keyed by the absolute path."""
    uploads = []
    for what, path in paths:
        if root != path and root not in path.parents:
            raise BridgeError(f"{what}: {path} is outside the workspace {root}, which is all the host sees")
        mount = next((m for m in mounts if m == path or m in path.parents), None)
        if mount is None:
            continue
        local = Path(path)
        if not (upload and local.is_file() and local.stat().st_size <= UPLOAD_LIMIT):
            raise BridgeError(f"{what}: {path} is on the container volume {mount}, which the host does not see; "
                              f"use a path in the shared checkout (the clients run on the host)")
        data = local.read_bytes()
        uploads.append({"arg": str(path), "name": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                        "data": base64.b64encode(data).decode("ascii")})
    return uploads


# The paths of a `cameras --jobs` job; JOB_FILES are read-only input files, sent along when they are on a container
# volume (the camera lists e2e writes into out/oracle-<V>/, a volume); worlds and outputs must be in the checkout.
JOB_PATHS = ("world", "cameras", "out")
JOB_FILES = ("cameras",)


def jobs_request(jobs_file: PurePosixPath, *, root: PurePosixPath, mounts: list[PurePosixPath]) -> tuple[dict, list[dict]]:
    """A `cameras --jobs` file for the server, which writes a copy with host paths for the launcher: its content with
    every job path absolute (relative ones resolved against the file's directory, as the launcher does), and the
    uploads of the job input files on container volumes. Checked with [check_paths]."""
    try:
        data = json.loads(Path(jobs_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BridgeError(f"--jobs {jobs_file}: {e}") from None
    uploads = []
    for i, job in enumerate(data.get("jobs") or [] if isinstance(data, dict) else []):
        for key in JOB_PATHS:
            value = job.get(key) if isinstance(job, dict) else None
            if isinstance(value, str) and value:
                path = PurePosixPath(posixpath.normpath(posixpath.join(jobs_file.parent, value)))
                job[key] = str(path)
                uploads += check_paths([(f"{jobs_file} job {i} {key}", path)], root=root, mounts=mounts,
                                       upload=key in JOB_FILES)
    return {"path": str(jobs_file), "content": data}, uploads


def forward(url: str, argv: list[str], *, root: PurePosixPath, cwd: PurePosixPath,
            paths: list[tuple[str, PurePath]], jobs_file: PurePath | None = None) -> int:
    """Runs `gtnh-client <argv>` on the server at `url` with the output streamed to stdout; returns its exit code.
    `root` is this workspace, `cwd` the directory relative paths are relative to; `paths` are the command's path
    arguments (named for messages), checked with [check_paths]; `jobs_file` is a `cameras --jobs` file, sent as
    [jobs_request]."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise BridgeError(f"{HOST_ENV}={url!r}: expected http://<host>:<port>")
    mounts = volume_mounts(root)

    def absolute(path: PurePath) -> PurePosixPath:
        return PurePosixPath(posixpath.normpath(posixpath.join(str(cwd), path.as_posix())))

    uploads = check_paths([(what, absolute(p)) for what, p in paths], root=root, mounts=mounts, upload=True)
    jobs = None
    if jobs_file is not None:
        jobs, job_uploads = jobs_request(absolute(jobs_file), root=root, mounts=mounts)
        uploads += job_uploads
    env = {k: v for k, v in os.environ.items() if k.startswith("BMGTNH_") and k not in LOCAL_ENV}
    body = json.dumps({"argv": argv, "cwd": str(cwd), "root": str(root), "env": env, "uploads": uploads,
                       "jobs": jobs}).encode()
    conn = http.client.HTTPConnection(parts.hostname, parts.port or DEFAULT_PORT, timeout=CONNECT_TIMEOUT)
    try:
        conn.connect()
    except OSError as e:
        raise BridgeError(
            f"no gtnh-client server at {url} ({HOST_ENV}): {e}. Start it on the host "
            f"(gtnh-exporter/launcher/windows/serve.ps1, see gtnh-exporter/launcher/README.md) or unset {HOST_ENV} "
            "to run the client here") from None
    try:
        conn.sock.settimeout(READ_TIMEOUT)
        conn.request("POST", "/run", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            text = response.read().decode("utf-8", "replace")
            try:
                text = json.loads(text).get("error", text)
            except ValueError:
                pass
            raise BridgeError(f"the gtnh-client server at {url} refused the command: {text}")
        print(f"[bridge] gtnh-client {subprocess.list2cmdline(argv)} runs on {url}"
              + (f" ({len(uploads)} file(s) sent along)" if uploads else ""), flush=True)
        while True:
            try:
                line = response.readline()
            except TimeoutError:
                raise BridgeError(f"no word from the gtnh-client server at {url} for {READ_TIMEOUT:.0f}s") from None
            if not line:
                raise BridgeError(f"the gtnh-client server at {url} closed the connection before the command ended")
            message = json.loads(line)
            if "o" in message:
                try:
                    sys.stdout.buffer.write(message["o"].encode("utf-8", "replace"))  # valid UTF-8, always
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass  # like the launcher's own console: keep running when the reader is gone
            elif "exit" in message:
                code = int(message["exit"])
                if not 0 <= code <= 255:
                    print(f"[bridge] exit code {code} ({code & 0xFFFFFFFF:#x}) on the host; exiting with 1", flush=True)
                    return 1
                return code
    finally:
        conn.close()


# ---------------------------------------------------------------- launcher under the server


def stop_on_eof() -> None:
    """For a launcher the server started (`BMGTNH_STOP_ON_EOF`): the server closes our stdin when its caller went
    away; that interrupts the main thread like SIGTERM where a handler for it is installed (a client run: the client
    trees are killed, the run's cleanup happens), else like Ctrl-C."""

    def watch() -> None:
        try:  # the raw fd: a thread blocked in sys.stdin's buffered reader breaks interpreter shutdown
            while os.read(sys.stdin.fileno(), 65536):
                pass
        except (OSError, ValueError):
            pass
        print("[bridge] stop requested by the server (its caller went away)", flush=True)
        _thread.interrupt_main(signal.SIGTERM if callable(signal.getsignal(signal.SIGTERM)) else signal.SIGINT)

    threading.Thread(target=watch, name="stop-on-eof", daemon=True).start()


# ---------------------------------------------------------------- server


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"  # on Windows SO_REUSEADDR lets a second server bind the same port

    def __init__(self, port: int, host_root: Path, log_file: Path, command: list[str] | None = None) -> None:
        self.host_root = host_root
        self.log_file = log_file
        self.command = command or [sys.executable, "-m", "gtnh_client"]
        self.staging = Path(tempfile.gettempdir()) / "gtnh-client-bridge"
        self.ids = itertools.count(1)
        self.active: dict[int, subprocess.Popen] = {}
        self._log_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    def log(self, message: str) -> None:
        """Appends to the log file and, in a terminal, prints (detached, the output goes to a file of its own)."""
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        with self._log_lock:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:
                if sys.stdout.isatty():
                    print(line, flush=True)
            except (OSError, ValueError):
                pass

    def prepare(self, request: dict) -> tuple[list[str], Path, dict[str, str], PathMap]:
        """The launcher's argv, cwd and environment for a caller's request, its paths mapped to this host."""
        argv, root, env = request["argv"], request["root"], request.get("env", {})
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise BridgeError("argv must be a non-empty list of strings")
        if argv[0] not in FORWARDED:
            raise BridgeError(f"{argv[0]!r} is not a client command ({', '.join(sorted(FORWARDED))})")
        if not isinstance(root, str) or not root.startswith("/"):
            raise BridgeError(f"root {root!r} must be the caller's absolute workspace path")
        path_map = PathMap(root, self.host_root)
        replace = {}
        for upload in request.get("uploads", []):
            data = base64.b64decode(upload["data"])
            if hashlib.sha256(data).hexdigest() != upload["sha256"]:
                raise BridgeError(f"upload {upload['arg']}: checksum mismatch")
            replace[posixpath.normpath(upload["arg"])] = str(self.stage(Path(upload["name"]).name, data))
        jobs = request.get("jobs")
        if jobs is not None:  # a copy of the cameras --jobs file with host paths, read by the launcher instead
            content = jobs["content"]
            for i, job in enumerate((content.get("jobs") if isinstance(content, dict) else None) or []):
                for key in JOB_PATHS:
                    value = job.get(key) if isinstance(job, dict) else None
                    if isinstance(value, str) and value:
                        host = replace.get(posixpath.normpath(value)) or path_map.to_host(value)
                        if host is None:
                            raise BridgeError(f"{jobs['path']} job {i} {key}: {value} is outside the workspace {root}")
                        job[key] = host
            data = (json.dumps(content, indent=1) + "\n").encode("utf-8")
            replace[posixpath.normpath(jobs["path"])] = str(self.stage(PurePosixPath(jobs["path"]).name, data))
        mapped = [argv[0], *(path_map.token(token, replace, cwd=request["cwd"]) for token in argv[1:])]
        cwd = path_map.to_host(request["cwd"])
        if cwd is None or not Path(cwd).is_dir():
            raise BridgeError(f"cwd {request['cwd']} is not a directory of the shared workspace on the host ({cwd}); "
                              f"does the server serve the checkout the caller has at {root}?")
        child_env = {k: v for k, v in os.environ.items() if k != HOST_ENV}
        for key, value in env.items():
            if key.startswith("BMGTNH_") and key not in LOCAL_ENV:
                child_env[key] = path_map.to_host(value) or value
        child_env.update({STOP_ENV: "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
        return mapped, Path(cwd), child_env, path_map

    def stage(self, name: str, data: bytes) -> Path:
        """A caller's file on this host: `<staging>/<sha256 prefix>/<name>`, written once per content."""
        target = self.staging / hashlib.sha256(data).hexdigest()[:16] / name
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            partial.write_bytes(data)
            partial.replace(target)
        return target

    def run(self, rid: int, argv: list[str], cwd: Path, env: dict[str, str], path_map: PathMap, send) -> None:
        """Runs the launcher, streams its output with `send` until it exits or the caller is gone (`send` raises)."""
        started = time.monotonic()
        try:
            proc = subprocess.Popen([*self.command, *argv], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **own_group())
        except OSError as e:
            self.log(f"#{rid} cannot start the launcher: {e}")
            send({"o": f"gtnh-client serve: cannot start the launcher: {e}\n"})
            send({"exit": 1})
            return
        self.active[rid] = proc
        self.log(f"#{rid} pid {proc.pid}: gtnh-client {subprocess.list2cmdline(argv)} (cwd {cwd})")
        lines: queue.Queue[bytes | None] = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, name=f"pump-{rid}", daemon=True).start()
        try:
            while True:
                try:
                    line = lines.get(timeout=HEARTBEAT)
                except queue.Empty:
                    send({"hb": 1})
                    continue
                if line is None:
                    break
                send({"o": output_text(path_map.to_remote(line))})
            code = proc.wait()
            self.log(f"#{rid} exit {code} after {time.monotonic() - started:.0f}s")
            send({"exit": code})
        except OSError as e:  # the caller is gone: broken pipe, reset, or the watcher's ConnectionAbortedError
            self.log(f"#{rid} caller gone after {time.monotonic() - started:.0f}s ({e}); stopping pid {proc.pid}")
            self.stop(rid, proc)
        finally:
            self.active.pop(rid, None)

    def stop(self, rid: int, proc: subprocess.Popen) -> None:
        """Stops a launcher: stdin closed (it stops like on SIGTERM, see [stop_on_eof]), its tree killed after
        STOP_GRACE seconds."""
        if proc.poll() is None:
            try:
                assert proc.stdin is not None
                proc.stdin.close()
            except OSError:
                pass
            try:
                code = proc.wait(timeout=STOP_GRACE)
                self.log(f"#{rid} stopped (exit {code})")
                return
            except subprocess.TimeoutExpired:
                self.log(f"#{rid} still running {STOP_GRACE:.0f}s after the stop request; killing its tree")
        kill_tree(proc)
        self.log(f"#{rid} killed (exit {proc.returncode})")

    def stop_all(self) -> None:
        for rid, proc in list(self.active.items()):
            self.log(f"#{rid} server shutting down; stopping pid {proc.pid}")
            self.stop(rid, proc)


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args) -> None:
        self.server.log(f"{self.address_string()} {format % args}")

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(404, {"error": "GET /health or POST /run"})
            return
        self._json(200, {"ok": True, "root": str(self.server.host_root), "pid": os.getpid(),
                         "running": len(self.server.active)})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._json(404, {"error": "GET /health or POST /run"})
            return
        rid = next(self.server.ids)
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            argv, cwd, env, path_map = self.server.prepare(request)
        except (ValueError, KeyError, TypeError, OSError, BridgeError, PathMapError) as e:
            self.server.log(f"#{rid} refused: {e}")
            self._json(400, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        gone = threading.Event()

        def watch() -> None:  # the caller sends nothing after its request: end of stream or an error means it left
            try:
                while self.connection.recv(4096):
                    pass
            except OSError:
                pass
            gone.set()

        threading.Thread(target=watch, name=f"watch-{rid}", daemon=True).start()

        def send(message: dict) -> None:
            if gone.is_set():
                raise ConnectionAbortedError("connection closed by the caller")
            self.wfile.write((json.dumps(message) + "\n").encode())
            self.wfile.flush()

        self.server.run(rid, argv, cwd, env, path_map, send)


def serve(port: int, host_root: Path, log_file: Path) -> int:
    """`gtnh-client serve`: runs client commands for bridge callers until interrupted."""
    java = subprocess.list2cmdline(java_command())  # fails now rather than on the first request
    try:
        server = Server(port, host_root, log_file)
    except OSError as e:
        raise BridgeError(f"cannot listen on 127.0.0.1:{port}: {e} (is a server already running?)") from None
    started = (f"gtnh-client server on http://127.0.0.1:{port} (pid {os.getpid()}), workspace {host_root}, java "
               f"{java}, log {log_file}; callers in the container set {HOST_ENV}=http://host.wslc.internal:{port}")
    server.log(started)
    if not sys.stdout.isatty():
        print(started, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        server.log("interrupted")
    finally:
        server.server_close()
        server.stop_all()
    return 0
