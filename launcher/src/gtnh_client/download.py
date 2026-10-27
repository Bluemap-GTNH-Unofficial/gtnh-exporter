"""SHA-1 verified, atomic, parallel HTTP downloads into the shared cache."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "gtnh-client/0.1 (BlueMap-GTNH launcher)"
_CHUNK = 1 << 20


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class Fetch:
    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None


@dataclass
class FetchStats:
    downloaded: int = 0
    downloaded_bytes: int = 0
    cached: int = 0
    cached_bytes: int = 0

    def __str__(self) -> str:
        return (
            f"{self.downloaded} downloaded ({self.downloaded_bytes / 1e6:.1f} MB), "
            f"{self.cached} cached ({self.cached_bytes / 1e6:.1f} MB)"
        )


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _valid(item: Fetch) -> bool:
    if not item.dest.is_file():
        return False
    if item.size is not None and item.dest.stat().st_size != item.size:
        return False
    return item.sha1 is None or sha1_file(item.dest) == item.sha1


def fetch(item: Fetch, retries: int = 3) -> int:
    """Ensure `item.dest` holds the verified file; return the number of bytes downloaded (0 if cached)."""
    if _valid(item):
        return 0
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    part = item.dest.with_name(f"{item.dest.name}.{os.getpid()}.{threading.get_ident()}.part")
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            digest = hashlib.sha1()
            size = 0
            request = urllib.request.Request(item.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:
                while chunk := response.read(_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            if item.sha1 is not None and digest.hexdigest() != item.sha1:
                raise DownloadError(f"sha1 mismatch for {item.url}: {digest.hexdigest()} != {item.sha1}")
            if item.size is not None and size != item.size:
                raise DownloadError(f"size mismatch for {item.url}: {size} != {item.size}")
            os.replace(part, item.dest)
            return size
        except Exception as e:  # noqa: BLE001 - retried, then re-raised with context
            last_error = e
            part.unlink(missing_ok=True)
            time.sleep(1 + attempt)
    raise DownloadError(f"failed to download {item.url} after {retries} attempts: {last_error}")


def fetch_all(items: list[Fetch], workers: int = 16) -> FetchStats:
    stats = FetchStats()
    unique = list({item.dest: item for item in items}.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, downloaded in zip(unique, pool.map(fetch, unique)):
            if downloaded:
                stats.downloaded += 1
                stats.downloaded_bytes += downloaded
            else:
                stats.cached += 1
                stats.cached_bytes += item.dest.stat().st_size
    return stats
