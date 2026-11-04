"""`gtnh-client cameras`: reference screenshots of scene worlds for the world-scene oracle (`e2e oracle world`).

The e2e repo writes a jobs file (`{"jobs": [{scene, world, cameras, out, remove, freeze_random_ticks}]}`) whose
camera lists are `cameras.json` files (format 1). This module validates it, copies each job's world snapshot into a
client's `saves/` as a single-player world, writes the spec for the exporter mod's `cameras` mode (which opens the saves
one after another in one client session and writes `<view>.png`, `<view>.depth.bin` and `<view>.json` per camera plus
`cameras-report.json` per job) and applies the capture settings to the client for one run.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from gtnh_client.workers import clone

# Version of the capture conditions: bump it whenever anything that can change the client's pixels, depth or matrices
# changes (CAPTURE_OPTIONS, ANGELICA_OPTIONS, SPEC_DEFAULTS, VIEW_DISTANCE, the mod's CameraCapture, the single-player
# path). The e2e reference cache reads this literal (with `ast`) into its keys; every cameras-report.json carries it.
# 1: dedicated server + RCON; 2: single-player worlds, frozen game while frames are compared; 3: ANGELICA_OPTIONS also
# written for clients without config/angelica-options.json (before, Angelica's RGSS/linear defaults blurred them);
# 4: references come from the Windows host's native NVIDIA OpenGL through the bridge (gtnh_client.bridge), not from
# Mesa llvmpipe in the container (camera views differ in 0.1-0.7 % of the pixels by more than 8).
CAPTURE_VERSION = 4
CAMERAS_FORMAT = 1
CAMERA_KEYS = ("name", "eye", "yaw", "pitch", "fov", "width", "height")
USERNAME = "BlueMapCamera"
SAVE_PREFIX = "oracle-"  # saves/oracle-<i>-<scene>: the job worlds of one run, deleted afterwards
REPORT = "cameras-report.json"
SPEC = "cameras-spec.json"
SESSION = "cameras-session.json"
# Chunk radius the integrated server sends: the `view-distance` of the dedicated scene servers
# (`servers/gtnh-<V>/server.properties`) the references were first captured with, so a camera sees the same chunks.
VIEW_DISTANCE = 8
# Rough cost of a job for splitting jobs across workers: opening and leaving the world, and each camera.
JOB_SECONDS = 12.0
CAMERA_SECONDS = 2.0
# options.txt for the capture run (1.7.10 keys): flat lighting, no mipmaps (texels as sharp as BlueMap's nearest
# sampling), no clouds or view bobbing, minimal particles, no pause menu when the (minimized) window has no focus, no
# "Press E to open your inventory" achievement hint.
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
    "showInventoryAchievementHint": "false",
}
# Angelica 2.2.28 (`config/angelica-options.json`): plain nearest texel sampling instead of its RGSS/anisotropic
# filtering, and no culling of chunks by fog distance (the camera mode pushes the fog beyond the far plane).
ANGELICA_OPTIONS = {
    ("quality", "texture_filter_mode"): "NONE",
    ("quality", "texel_sampling"): "NEAREST",
    ("performance", "use_fog_occlusion"): False,
}
# Spec values the mod reads besides the jobs (see CameraCapture.readSpec).
SPEC_DEFAULTS = {
    "loadTicks": 5,  # every chunk of the scene box in range and of the view square on the client this many ticks
    "stableFrames": 3,  # frames of the frozen game (stableIntervalSeconds apart, terrain complete) that must agree
    "stableIntervalSeconds": 0.1,
    "stableTolerance": 0.0005,  # share of pixels allowed to change by more than 8 in a channel between them
    "worldTimeoutSeconds": 300,  # opening a save until the player is in the world
    "cameraTimeoutSeconds": 60,  # teleport until the chunks are on the client; then the camera fails
    "stableTimeoutSeconds": 30,  # frozen until the frames agree; then the frame is captured anyway (stable false)
}


class CamerasError(Exception):
    pass


@dataclass
class Job:
    """One scene world to capture: the world snapshot (copied, never modified), its camera list and output directory."""

    scene: str
    world: Path
    cameras_file: Path
    cameras: dict
    out: Path
    remove: list[list[int]]
    freeze_random_ticks: bool = True

    @property
    def cost(self) -> float:
        return JOB_SECONDS + CAMERA_SECONDS * len(self.cameras["cameras"])


def read_cameras(path: Path) -> dict:
    """`cameras.json` as written by `e2e oracle world`: {format: 1, scene, world, levelType?, box?, cameras: [{name,
    eye: [x, y, z], yaw, pitch, fov, width, height}], remove?: [[x, y, z]]} (MC degrees: yaw 0 = south, pitch 90 =
    down; vertical FOV). `remove`: overworld cells set to air before the player joins."""
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
    _check_cells(data.get("remove") or [], f"{path}: remove")
    return data


def _check_cells(cells: object, what: str) -> list[list[int]]:
    if not isinstance(cells, list) or not all(
        isinstance(p, list) and len(p) == 3 and all(isinstance(v, int) and not isinstance(v, bool) for v in p) for p in cells
    ):
        raise CamerasError(f"{what} must be a list of [x, y, z] integer cells")
    return cells


def read_jobs(path: Path) -> list[Job]:
    """The jobs file of `cameras --jobs`: {"jobs": [{"scene", "world": <world snapshot dir>, "cameras": <cameras.json>,
    "out": <dir>, "remove"?: [[x, y, z]], "freeze_random_ticks"?: true}]}. Relative paths are relative to the file.
    `remove` defaults to the camera list's. Every problem is reported before anything runs."""
    if not path.is_file():
        raise CamerasError(f"jobs file {path} does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise CamerasError(f"{path}: {e}") from None
    entries = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise CamerasError(f"{path}: expected {{\"jobs\": [...]}} with at least one job")

    def resolve(value: object, what: str) -> Path:
        if not isinstance(value, str) or not value:
            raise CamerasError(f"{path}: job {what} must be a path string")
        p = Path(value)
        return (p if p.is_absolute() else path.parent / p).resolve()

    jobs: list[Job] = []
    outs: set[Path] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise CamerasError(f"{path}: job {i} is not an object")
        scene = entry.get("scene")
        if not isinstance(scene, str) or not scene:
            raise CamerasError(f"{path}: job {i} has no scene")
        label = f"{path}: job {i} ({scene})"
        world = resolve(entry.get("world"), f"{i} world")
        if not (world / "level.dat").is_file():
            raise CamerasError(f"{label}: {world} is not a world (no level.dat)")
        cameras_file = resolve(entry.get("cameras"), f"{i} cameras")
        cameras = read_cameras(cameras_file)
        if cameras["scene"] != scene:
            raise CamerasError(f"{label}: {cameras_file} is for scene {cameras['scene']}")
        out = resolve(entry.get("out"), f"{i} out")
        if out in outs:
            raise CamerasError(f"{label}: output directory {out} is used by another job")
        outs.add(out)
        remove = _check_cells(entry["remove"], f"{label}: remove") if "remove" in entry else cameras.get("remove") or []
        freeze = entry.get("freeze_random_ticks", True)
        if not isinstance(freeze, bool):
            raise CamerasError(f"{label}: freeze_random_ticks must be true or false")
        jobs.append(Job(scene, world, cameras_file, cameras, out, remove, freeze))
    return jobs


def save_name(index: int, job: Job) -> str:
    """The single-player save folder of the `index`-th job of a worker: `oracle-<index>-<scene>`."""
    return f"{SAVE_PREFIX}{index}-{re.sub(r'[^A-Za-z0-9._-]', '_', job.scene)}"


def prepare_saves(minecraft_dir: Path, jobs: list[Job]) -> list[str]:
    """Copies each job's world snapshot into `saves/` (APFS clones on macOS, plain copies elsewhere; the snapshots are
    never touched) after removing the saves an earlier run left behind. Returns the save names in job order."""
    saves = minecraft_dir / "saves"
    remove_saves(minecraft_dir)
    saves.mkdir(parents=True, exist_ok=True)
    names = []
    for i, job in enumerate(jobs, 1):
        name = save_name(i, job)
        clone(job.world, saves / name)
        (saves / name / "session.lock").unlink(missing_ok=True)
        names.append(name)
    return names


def remove_saves(minecraft_dir: Path) -> None:
    saves = minecraft_dir / "saves"
    if saves.is_dir():
        for save in saves.glob(f"{SAVE_PREFIX}*"):
            shutil.rmtree(save, ignore_errors=True)


def build_spec(jobs: list[Job], saves: list[str], *, session: Path) -> dict:
    """The spec file for the mod: the jobs (save, scene box, cameras, cells to remove, output directory) and the
    view distance and wait settings. The mod writes its session summary to `session`."""
    return {
        "captureVersion": CAPTURE_VERSION,
        "viewDistance": VIEW_DISTANCE,
        "renderDistance": int(CAPTURE_OPTIONS["renderDistance"]),
        **SPEC_DEFAULTS,
        "session": str(session),
        "jobs": [
            {
                "scene": job.scene,
                "save": save,
                "box": job.cameras.get("box"),
                "cameras": job.cameras["cameras"],
                "remove": job.remove,
                "freezeRandomTicks": job.freeze_random_ticks,
                "out": str(job.out),
            }
            for job, save in zip(jobs, saves, strict=True)
        ],
    }


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
    files (or removes them if they did not exist) afterwards, also on errors and interrupts. A client that has never
    written its Angelica options (a freshly prepared one) gets a file with just ANGELICA_OPTIONS, which Angelica
    completes with its defaults; without it Angelica would sample textures with its RGSS/linear defaults."""
    files = [minecraft_dir / "options.txt", minecraft_dir / "config" / "angelica-options.json"]
    saved = {path: path.read_bytes() if path.is_file() else None for path in files}
    try:
        options, angelica = files
        options.write_text(set_options(options.read_text() if options.is_file() else "", CAPTURE_OPTIONS))
        data = json.loads(angelica.read_text(encoding="utf-8")) if angelica.is_file() else {}
        angelica.parent.mkdir(parents=True, exist_ok=True)
        angelica.write_text(json.dumps(set_angelica_options(data, ANGELICA_OPTIONS), indent=2) + "\n")
        print(f"[cameras] capture settings in {options} and {angelica} (restored afterwards)", flush=True)
        yield
    finally:
        for path, content in saved.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)


def check_outputs(out: Path, cameras: dict, started_wall: float, session_error: str | None = None) -> list[str]:
    """Problems with the outputs of a finished job (missing, stale or failed); empty when every camera was captured.
    `session_error`: the error of the client session that ran the job (its `cameras-session.json`), which explains a
    missing report (e.g. the capture's watchdog killed the stuck client)."""
    report = out / REPORT
    if not report.is_file() or report.stat().st_mtime < started_wall:
        why = f"; client session error: {session_error}" if session_error else ""
        return [f"{report} missing or older than this run{why}"]
    problems = []
    data = json.loads(report.read_text(encoding="utf-8"))
    if data.get("error"):
        problems.append(f"job error: {data['error']}")
    if data.get("capture_version") != CAPTURE_VERSION:
        problems.append(f"{report}: capture_version {data.get('capture_version')}, expected {CAPTURE_VERSION}")
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
