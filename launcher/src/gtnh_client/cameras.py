"""`gtnh-client cameras`: reference screenshots of a scene world for the world-scene oracle (`e2e oracle world`).

The camera list comes from the e2e repo (`e2e oracle world` writes `cameras.json`, format 1). This module restores the
scene world onto a GTNH server with the sibling orchestrator (`uv run orch ... restore | start | stop`), applies the
capture settings to the client for this run only, and runs the client in the exporter mod's `cameras` mode, which
joins the server and writes `<view>.png`, `<view>.depth.bin` and `<view>.json` per camera plus `cameras-report.json`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

CAMERAS_FORMAT = 1
CAMERA_KEYS = ("name", "eye", "yaw", "pitch", "fov", "width", "height")
USERNAME = "BlueMapCamera"
LEVEL_PREFIX = "oracle-"  # server level folder: never the scene's own `scene-<name>` folder
REPORT = "cameras-report.json"

# options.txt for the capture run (1.7.10 keys): flat lighting, no mipmaps (texels as sharp as BlueMap's nearest
# sampling), no clouds or view bobbing, minimal particles, no pause menu when the (minimized) window has no focus.
CAPTURE_OPTIONS = {
    "ao": "0",
    "mipmapLevels": "0",
    "anisotropicFiltering": "1",
    "renderDistance": "12",
    "fboEnable": "true",
    "fancyGraphics": "true",
    "clouds": "false",
    "bobView": "false",
    "particles": "2",
    "anaglyph3d": "false",
    "pauseOnLostFocus": "false",
}
# Angelica 2.2.28 (`config/angelica-options.json`): plain nearest texel sampling instead of its RGSS/anisotropic
# filtering, and no culling of chunks by fog distance (the camera mode pushes the fog beyond the far plane).
ANGELICA_OPTIONS = {
    ("quality", "texture_filter_mode"): "NONE",
    ("quality", "texel_sampling"): "NEAREST",
    ("performance", "use_fog_occlusion"): False,
}
# Spec values the mod reads besides the cameras (see CameraCapture.readSpec).
SPEC_DEFAULTS = {
    "settleTicks": 60,  # chunks loaded and terrain built for this many ticks before frames are compared
    "stableFrames": 3,  # consecutive frames (stableIntervalTicks apart) that must agree
    "stableIntervalTicks": 10,
    "stableTolerance": 0.0005,  # share of pixels allowed to change by more than 8 in a channel between them
    "joinTimeoutSeconds": 900,
    "cameraTimeoutSeconds": 240,
}


class CamerasError(Exception):
    pass


def read_cameras(path: Path) -> dict:
    """`cameras.json` as written by `e2e oracle world`: {format: 1, scene, world, levelType?, box?, cameras: [{name,
    eye: [x, y, z], yaw, pitch, fov, width, height}], remove?: [[x, y, z]]} (MC degrees: yaw 0 = south, pitch 90 =
    down; vertical FOV). `remove`: overworld cells set to air before the client joins."""
    if not path.is_file():
        raise CamerasError(f"camera list {path} does not exist; `uv run e2e oracle world --scene <scene>` writes it")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != CAMERAS_FORMAT:
        raise CamerasError(f"{path}: format {data.get('format')!r}, expected {CAMERAS_FORMAT}")
    cameras = data.get("cameras") or []
    if not cameras:
        raise CamerasError(f"{path}: no cameras")
    names = set()
    for camera in cameras:
        missing = [k for k in CAMERA_KEYS if k not in camera]
        if missing:
            raise CamerasError(f"{path}: camera {camera.get('name', '?')} lacks {missing}")
        if camera["name"] in names:
            raise CamerasError(f"{path}: duplicate camera name {camera['name']}")
        names.add(camera["name"])
    remove = data.get("remove") or []
    if not all(isinstance(p, list) and len(p) == 3 and all(isinstance(v, int) for v in p) for p in remove):
        raise CamerasError(f"{path}: remove must be a list of [x, y, z] integer cells")
    return data


def build_spec(cameras: dict, *, rcon_port: int, password_file: Path, view_distance: int) -> dict:
    """The spec file for the mod: the camera list plus RCON access and the settle/stability settings."""
    return {
        "scene": cameras["scene"],
        "box": cameras.get("box"),
        "cameras": cameras["cameras"],
        "remove": cameras.get("remove") or [],
        "rcon": {"host": "127.0.0.1", "port": rcon_port, "passwordFile": str(password_file)},
        "viewDistance": view_distance,
        "renderDistance": int(CAPTURE_OPTIONS["renderDistance"]),
        **SPEC_DEFAULTS,
    }


def server_view_distance(server_dir: Path, default: int = 10) -> int:
    """`view-distance` of the server's server.properties (vanilla default 10)."""
    props = server_dir / "server.properties"
    if props.is_file():
        for line in props.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "view-distance" and value.strip().isdigit():
                return int(value.strip())
    return default


def set_options(text: str, values: dict[str, str]) -> str:
    """options.txt with `key:value` lines replaced (or appended), every other line kept in place."""
    remaining = dict(values)
    lines = []
    for line in text.splitlines():
        key, sep, _ = line.partition(":")
        if sep and key in remaining:
            lines.append(f"{key}:{remaining.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{k}:{v}" for k, v in remaining.items())
    return "\n".join(lines) + "\n"


def set_angelica_options(data: dict, values: dict[tuple[str, str], object]) -> dict:
    result = json.loads(json.dumps(data))
    for (section, key), value in values.items():
        result.setdefault(section, {})[key] = value
    return result


@contextlib.contextmanager
def capture_settings(minecraft_dir: Path):
    """Writes the capture settings into `options.txt` and `config/angelica-options.json` for one run and restores both
    files (or removes them if they did not exist) afterwards, also on errors and interrupts."""
    files = [minecraft_dir / "options.txt", minecraft_dir / "config" / "angelica-options.json"]
    saved = {path: path.read_bytes() if path.is_file() else None for path in files}
    try:
        options, angelica = files
        options.write_text(set_options(options.read_text() if options.is_file() else "", CAPTURE_OPTIONS))
        if angelica.is_file():
            data = json.loads(angelica.read_text(encoding="utf-8"))
            angelica.write_text(json.dumps(set_angelica_options(data, ANGELICA_OPTIONS), indent=2) + "\n")
        print(f"[cameras] capture settings in {options} and {angelica} (restored afterwards)", flush=True)
        yield
    finally:
        for path, content in saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)


@dataclass
class Orchestrator:
    """The sibling `orchestrator` checkout's `orch` command (run with uv) for one server directory, level and pack
    version (whose world snapshots `restore` reads)."""

    checkout: Path
    server: str
    port: int
    rcon_port: int
    level: str
    level_type: str = "FLAT"
    pack_version: str | None = None

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        version = ["--pack-version", self.pack_version] if self.pack_version else []
        cmd = ["uv", "run", "orch", *version, "--server", self.server, "--port", str(self.port),
               "--rcon-port", str(self.rcon_port), "--level", self.level, "--level-type", self.level_type, *args]  # fmt: skip
        print(f"[cameras] {subprocess.list2cmdline(cmd)}", flush=True)
        proc = subprocess.run(cmd, cwd=self.checkout, capture_output=True, text=True)
        if proc.stdout.strip():
            print(proc.stdout.strip(), flush=True)
        if check and proc.returncode != 0:
            raise CamerasError(f"orch {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[-2000:]}")
        return proc

    def running(self) -> bool:
        return self.run("status").stdout.strip().startswith("running")

    def remove_blocks(self, cells: list[list[int]]) -> None:
        """Sets overworld cells to air (`bmh set`, no neighbour updates), all in one RCON session."""
        if not cells:
            return
        replies = self.run("rcon", *(f"bmh set 0 {x} {y} {z} minecraft:air 0" for x, y, z in cells)).stdout.splitlines()
        failed = [r for r in replies if not r.startswith("OK")]
        if len(replies) != len(cells) or failed:
            raise CamerasError(f"removing {len(cells)} cells: {len(replies)} replies, failures {failed[:5]}")


def check_outputs(out: Path, cameras: dict, started_wall: float) -> list[str]:
    """Problems with the outputs of a finished run (missing or stale files); empty when every camera was captured."""
    problems = []
    report = out / REPORT
    if not report.is_file() or report.stat().st_mtime < started_wall:
        return [f"{report} missing or older than this run"]
    for camera in cameras["cameras"]:
        for suffix in (".png", ".depth.bin", ".json"):
            path = out / f"{camera['name']}{suffix}"
            if not path.is_file() or path.stat().st_mtime < started_wall:
                problems.append(f"{path} missing or stale")
    return problems


def clean_outputs(out: Path, cameras: dict) -> None:
    """Removes earlier outputs of these cameras, so a failed camera cannot leave a stale capture behind."""
    for camera in cameras["cameras"]:
        for suffix in (".png", ".depth.bin", ".json"):
            (out / f"{camera['name']}{suffix}").unlink(missing_ok=True)
    (out / REPORT).unlink(missing_ok=True)


def write_spec(spec: dict, out: Path) -> Path:
    path = out / "spec.json"
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return path
