"""Client directory layout and `prepare`: unpack the Prism instance zip, fetch libraries/assets, extract natives."""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from gtnh_client.download import Fetch, FetchStats, fetch_all
from gtnh_client.profile import Profile, artifact_file, build_profile, load_components

ASSET_OBJECTS_URL = "https://resources.download.minecraft.net"
_UNPACK_MARKER = ".gtnh-client-unpacked.json"


class PrepareError(Exception):
    pass


@dataclass(frozen=True)
class Layout:
    client_dir: Path
    cache_dir: Path

    @property
    def minecraft_dir(self) -> Path:
        return self.client_dir / ".minecraft"

    @property
    def local_libraries(self) -> Path:
        """Instance-local libraries (MMC-hint local), shipped in the zip."""
        return self.client_dir / "libraries"

    @property
    def natives_dir(self) -> Path:
        return self.client_dir / "natives"

    @property
    def libraries_cache(self) -> Path:
        return self.cache_dir / "libraries"

    @property
    def assets_dir(self) -> Path:
        return self.cache_dir / "assets"

    def load_profile(self) -> Profile:
        return build_profile(load_components(self.client_dir))


def _zip_identity(zip_path: Path) -> dict:
    st = zip_path.stat()
    return {"zip": str(zip_path.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def unpack(zip_path: Path, dest: Path) -> int:
    """Extract with the single top-level folder flattened; later entries overwrite earlier ones (`unzip -o`)."""
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        tops = {PurePosixPath(i.filename).parts[0] for i in infos}
        if len(tops) != 1:
            raise PrepareError(f"{zip_path}: expected one top-level folder, found {sorted(tops)[:5]}")
        count = 0
        for info in infos:
            rel = PurePosixPath(info.filename).parts[1:]
            if not rel:
                continue
            if any(p in ("..", "") for p in rel):
                raise PrepareError(f"{zip_path}: unsafe entry {info.filename!r}")
            target = dest.joinpath(*rel)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            count += 1
    return count


def asset_index_fetch(layout: Layout, profile: Profile) -> Fetch:
    index = profile.asset_index
    if index is None:
        raise PrepareError("profile has no assetIndex")
    return Fetch(index["url"], layout.assets_dir / "indexes" / f"{index['id']}.json", index["sha1"], index["size"])


def _asset_objects(layout: Layout, index_file: Path) -> list[Fetch]:
    data = json.loads(index_file.read_text())
    if data.get("virtual") or data.get("map_to_resources"):
        raise PrepareError(f"{index_file}: virtual/map_to_resources asset indexes are not supported")
    fetches = []
    for obj in data["objects"].values():
        h = obj["hash"]
        fetches.append(Fetch(f"{ASSET_OBJECTS_URL}/{h[:2]}/{h}", layout.assets_dir / "objects" / h[:2] / h, h, obj["size"]))
    return fetches


def _library_fetches(layout: Layout, profile: Profile) -> list[Fetch]:
    fetches = []
    for artifact in profile.artifacts():
        dest = artifact_file(artifact, layout.libraries_cache, layout.local_libraries)
        if artifact.local:
            if not dest.is_file():
                raise PrepareError(f"local library {dest} missing (should come from the instance zip)")
            continue
        fetches.append(Fetch(artifact.url, dest, artifact.sha1, artifact.size))
    return fetches


def extract_natives(layout: Layout, profile: Profile) -> int:
    shutil.rmtree(layout.natives_dir, ignore_errors=True)
    layout.natives_dir.mkdir(parents=True)
    count = 0
    for lib in profile.natives:
        jar = artifact_file(lib.artifact, layout.libraries_cache, layout.local_libraries)
        with zipfile.ZipFile(jar) as zf:
            for info in zf.infolist():
                if info.is_dir() or any(info.filename.startswith(ex) for ex in lib.extract_exclude):
                    continue
                target = layout.natives_dir / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    return count


def prepare(layout: Layout, zip_path: Path, clean: bool = False) -> Profile:
    t0 = time.monotonic()
    if clean and layout.client_dir.exists():
        print(f"[prepare] deleting {layout.client_dir}")
        shutil.rmtree(layout.client_dir)
    marker = layout.client_dir / _UNPACK_MARKER
    identity = _zip_identity(zip_path)
    if marker.is_file() and json.loads(marker.read_text()) == identity:
        print(f"[prepare] {layout.client_dir} already unpacked from {zip_path.name}; skipping unpack")
    else:
        t = time.monotonic()
        layout.client_dir.mkdir(parents=True, exist_ok=True)
        count = unpack(zip_path, layout.client_dir)
        marker.write_text(json.dumps(identity))
        print(f"[prepare] unpacked {count} files from {zip_path.name} in {time.monotonic() - t:.1f}s")

    profile = layout.load_profile()
    t = time.monotonic()
    index_fetch = asset_index_fetch(layout, profile)
    lib_stats = fetch_all([*_library_fetches(layout, profile), index_fetch])
    print(f"[prepare] libraries + client jar + asset index: {lib_stats} in {time.monotonic() - t:.1f}s")
    t = time.monotonic()
    asset_stats = fetch_all(_asset_objects(layout, index_fetch.dest))
    print(f"[prepare] asset objects: {asset_stats} in {time.monotonic() - t:.1f}s")
    natives = extract_natives(layout, profile)
    print(f"[prepare] extracted {natives} native files into {layout.natives_dir}")
    total = FetchStats(
        lib_stats.downloaded + asset_stats.downloaded,
        lib_stats.downloaded_bytes + asset_stats.downloaded_bytes,
        lib_stats.cached + asset_stats.cached,
        lib_stats.cached_bytes + asset_stats.cached_bytes,
    )
    print(f"[prepare] done in {time.monotonic() - t0:.1f}s; total {total}")
    return profile


def ensure_downloads(layout: Layout, profile: Profile) -> None:
    """Cheap pre-launch check (existence only); fetches anything missing from the cache."""
    index_fetch = asset_index_fetch(layout, profile)
    missing = [f for f in [*_library_fetches(layout, profile), index_fetch] if not f.dest.is_file()]
    if missing:
        print(f"[run] fetching {len(missing)} missing libraries")
        fetch_all(missing)
    objects = [f for f in _asset_objects(layout, index_fetch.dest) if not f.dest.is_file()]
    if objects:
        print(f"[run] fetching {len(objects)} missing asset objects")
        fetch_all(objects)
    if profile.natives and not layout.natives_dir.is_dir():
        extract_natives(layout, profile)
