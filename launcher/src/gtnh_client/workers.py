"""Worker instances for parallel client runs (`cameras --parallel N`, `states --parallel N`).

Worker `i` of a base client `WS/clients/<name>` is `WS/clients/<name>-worker-<i>/` with its own game directory
`.minecraft/` (mods, configs, options, saves, logs), mirrored from the base's before every run. The pack itself
(patches, instance libraries, natives) is the base's, and libraries and assets come from the shared download cache, so
a worker is a [Layout] with `game_dir` set. Copies are APFS clones (`clonefile`, copy on write) where the file system
supports them, so a worker costs almost no disk space and is created in about a second.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from gtnh_client.instance import Layout

WORKER_INFIX = "-worker-"
# Entries of the base's .minecraft that are each instance's own state and are never mirrored.
UNSHARED = frozenset({"saves", "logs", "crash-reports", "screenshots", "backups", "mods-disabled"})

T = TypeVar("T")


def _load_clonefile() -> Callable[[bytes, bytes, int], int] | None:
    if sys.platform != "darwin":
        return None
    try:
        fn = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn


_clonefile = _load_clonefile()


def clone(src: Path, dst: Path) -> None:
    """Copies a file or a whole directory tree to `dst` (which must not exist), keeping modification times: one APFS
    `clonefile` call where possible, else `shutil.copy2`/`copytree`."""
    if _clonefile is not None and _clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
        return
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


@dataclass
class MirrorStats:
    copied: int = 0
    removed: int = 0


def mirror(src: Path, dst: Path, *, exclude: frozenset[str] = frozenset(), stats: MirrorStats | None = None) -> MirrorStats:
    """Makes `dst` an exact copy of `src`: files that differ in size or modification time are cloned again, entries
    missing in `src` are removed, a missing directory is cloned whole. Top-level names in `exclude` are left alone."""
    stats = stats or MirrorStats()
    if not dst.is_dir():
        if dst.exists() or dst.is_symlink():
            _remove(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not exclude:
            clone(src, dst)
            stats.copied += 1
            return stats
        dst.mkdir()
    sources = {entry.name: entry for entry in os.scandir(src) if entry.name not in exclude}
    for entry in os.scandir(dst):
        if entry.name in exclude:
            continue
        source = sources.get(entry.name)
        if (source is None or source.is_symlink() != entry.is_symlink()
                or source.is_dir(follow_symlinks=False) != entry.is_dir(follow_symlinks=False)):  # fmt: skip
            _remove(Path(entry.path))
            stats.removed += 1
    for name, source in sources.items():
        target = dst / name
        if source.is_symlink():
            link = os.readlink(source.path)
            if not target.is_symlink() or os.readlink(target) != link:
                target.unlink(missing_ok=True)
                target.symlink_to(link)
                stats.copied += 1
        elif source.is_dir(follow_symlinks=False):
            mirror(Path(source.path), target, stats=stats)
        elif source.is_file(follow_symlinks=False):
            st = source.stat(follow_symlinks=False)
            try:
                tt = target.stat()
                if tt.st_size == st.st_size and tt.st_mtime_ns == st.st_mtime_ns:
                    continue
                target.unlink()
            except FileNotFoundError:
                pass
            clone(Path(source.path), target)
            stats.copied += 1
    return stats


def worker_dir(base_client_dir: Path, index: int) -> Path:
    """`WS/clients/<name>-worker-<index>/` (1-based) next to the base client `WS/clients/<name>/`."""
    return base_client_dir.parent / f"{base_client_dir.name}{WORKER_INFIX}{index}"


def worker_layout(base: Layout, index: int) -> Layout:
    return Layout(base.client_dir, base.cache_dir, game_dir=worker_dir(base.client_dir, index) / ".minecraft")


def sync_worker(base: Layout, index: int) -> tuple[Layout, MirrorStats]:
    """The worker's layout, its game directory mirrored from the base's (everything but [UNSHARED])."""
    layout = worker_layout(base, index)
    stats = mirror(base.minecraft_dir, layout.minecraft_dir, exclude=UNSHARED)
    return layout, stats


def split(items: Sequence[T], workers: int, cost: Callable[[T], float]) -> list[list[T]]:
    """Splits `items` into at most `workers` non-empty groups of similar total cost (largest first onto the least
    loaded group, ties to the lower index); each group keeps the input order. Deterministic."""
    count = max(1, min(workers, len(items)))
    loads = [0.0] * count
    groups: list[list[int]] = [[] for _ in range(count)]
    for i in sorted(range(len(items)), key=lambda i: (-cost(items[i]), i)):
        target = min(range(count), key=lambda g: (loads[g], g))
        groups[target].append(i)
        loads[target] += cost(items[i])
    return [[items[i] for i in sorted(group)] for group in groups if group]
