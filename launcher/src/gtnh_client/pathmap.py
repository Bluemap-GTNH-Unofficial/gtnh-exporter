"""Workspace paths of a bridge caller (`gtnh-client serve`): the caller (the container) names files under its own
workspace root (`/ws`); the host where the clients run sees the same bind-mounted tree under its workspace root
(`D:\\Codes\\vibe\\BlueMap-GTNH`)."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path


class PathMapError(Exception):
    pass


@dataclass(frozen=True)
class PathMap:
    """The caller's workspace root `remote` (a POSIX path) and the host's `host`."""

    remote: str
    host: Path

    def to_host(self, value: str) -> str | None:
        """The host path of a caller path under its root (normalized: `/ws/a/../b` is `/ws/b`), else None."""
        if not value.startswith("/"):
            return None
        path, root = posixpath.normpath(value), posixpath.normpath(self.remote)
        if path == root:
            return str(self.host)
        if path.startswith(root.rstrip("/") + "/"):
            return str(self.host.joinpath(*path[len(root.rstrip("/")) + 1 :].split("/")))
        return None

    def token(self, token: str, replace: dict[str, str] | None = None, cwd: str | None = None) -> str:
        """A command-line token with the caller path in it mapped: the whole token, or the part after an `=`
        (`--out=/ws/x`, `--jvm-arg=-Dkey=/ws/x`). `replace` maps caller paths (absolute; a relative value is taken
        relative to the caller's `cwd`) to host files first (uploads). An absolute path outside the root cannot be
        mapped and raises [PathMapError]."""
        for start in (0, *(m.end() for m in re.finditer("=", token))):
            value = token[start:]
            if replace and value:
                key = posixpath.normpath(value if value.startswith("/") or cwd is None else posixpath.join(cwd, value))
                if key in replace:
                    return token[:start] + replace[key]
            mapped = self.to_host(value)
            if mapped is not None:
                return token[:start] + mapped
            if value.startswith("/"):
                raise PathMapError(f"{token!r}: {value} is outside the workspace {self.remote}, which is all the host sees")
        return token

    def to_remote(self, text: bytes) -> bytes:
        """Output text with host paths under the host root written as caller paths (`D:\\ws\\a\\b` -> `/ws/a/b`)."""
        return self._host_pattern.sub(lambda m: self.remote.encode() + (m.group(1) or b"").replace(b"\\", b"/"), text)

    @cached_property
    def _host_pattern(self) -> re.Pattern[bytes]:
        parts = [re.escape(p.encode()) for p in re.split(r"[\\/]", str(self.host).rstrip("\\/"))]
        rest = rb"(?:([\\/][^\s\"'<>|;,]*)|(?![\w.\-]))"  # the path below the root, or the root itself
        return re.compile(rb"[\\/]".join(parts) + rest, re.IGNORECASE)
