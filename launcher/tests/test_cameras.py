import json
import os
import time
from pathlib import Path

import pytest

from gtnh_client import cameras
from gtnh_client.cli import build_parser, resolve_pack

CAMERAS = {
    "format": 1,
    "scene": "vanilla-zoo",
    "world": "vanilla-zoo",
    "box": [-1, 4, -1, 37, 8, 35],
    "cameras": [
        {"name": "south", "eye": [18.5, 12.0, 45.0], "yaw": 180.0, "pitch": 30.0, "fov": 70.0, "width": 640, "height": 360},
    ],
}


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_read_cameras_validates(tmp_path: Path):
    assert cameras.read_cameras(_write(tmp_path / "c.json", CAMERAS))["scene"] == "vanilla-zoo"
    with pytest.raises(cameras.CamerasError, match="format"):
        cameras.read_cameras(_write(tmp_path / "f.json", {**CAMERAS, "format": 2}))
    broken = {**CAMERAS, "cameras": [{k: v for k, v in CAMERAS["cameras"][0].items() if k != "fov"}]}
    with pytest.raises(cameras.CamerasError, match="fov"):
        cameras.read_cameras(_write(tmp_path / "b.json", broken))
    twice = {**CAMERAS, "cameras": CAMERAS["cameras"] * 2}
    with pytest.raises(cameras.CamerasError, match="duplicate"):
        cameras.read_cameras(_write(tmp_path / "d.json", twice))
    with pytest.raises(cameras.CamerasError, match="does not exist"):
        cameras.read_cameras(tmp_path / "missing.json")


def _world(path: Path) -> Path:
    (path / "region").mkdir(parents=True)
    (path / "level.dat").write_bytes(b"level")
    (path / "region" / "r.0.0.mca").write_bytes(b"chunks")
    (path / "session.lock").write_bytes(b"lock")
    return path


def _jobs(tmp_path: Path, *entries: dict) -> Path:
    return _write(tmp_path / "jobs.json", {"jobs": list(entries)})


def test_read_jobs_resolves_paths_and_defaults(tmp_path: Path):
    world = _world(tmp_path / "worlds" / "vanilla-zoo")
    cameras_file = _write(tmp_path / "cameras.json", {**CAMERAS, "remove": [[1, 5, 2]]})
    jobs_file = _jobs(tmp_path, {"scene": "vanilla-zoo", "world": "worlds/vanilla-zoo", "cameras": str(cameras_file),
                                 "out": "out/a"})
    [job] = cameras.read_jobs(jobs_file)
    assert (job.scene, job.world, job.cameras_file, job.out) == ("vanilla-zoo", world, cameras_file, tmp_path / "out" / "a")
    assert job.remove == [[1, 5, 2]] and job.freeze_random_ticks is True  # remove from the camera list
    assert job.cameras["cameras"] == CAMERAS["cameras"]
    _jobs(tmp_path, {"scene": "vanilla-zoo", "world": str(world), "cameras": str(cameras_file), "out": "o",
                     "remove": [], "freeze_random_ticks": False})
    [job] = cameras.read_jobs(jobs_file)
    assert job.remove == [] and job.freeze_random_ticks is False


@pytest.mark.parametrize(("change", "message"), [
    ({"scene": "gt-ores"}, "is for scene vanilla-zoo"),
    ({"world": "nowhere"}, "is not a world"),
    ({"cameras": "missing.json"}, "does not exist"),
    ({"remove": [[1, 2]]}, "remove must be"),
    ({"remove": [[1, 2, True]]}, "remove must be"),
    ({"freeze_random_ticks": "yes"}, "freeze_random_ticks"),
    ({"out": None}, "must be a path"),
])
def test_read_jobs_rejects_bad_jobs(tmp_path: Path, change: dict, message: str):
    _world(tmp_path / "w")
    _write(tmp_path / "cameras.json", CAMERAS)
    job = {"scene": "vanilla-zoo", "world": "w", "cameras": "cameras.json", "out": "o"} | change
    with pytest.raises(cameras.CamerasError, match=message):
        cameras.read_jobs(_jobs(tmp_path, job))


def test_read_jobs_rejects_empty_and_shared_outputs(tmp_path: Path):
    _world(tmp_path / "w")
    _write(tmp_path / "cameras.json", CAMERAS)
    with pytest.raises(cameras.CamerasError, match="at least one job"):
        cameras.read_jobs(_jobs(tmp_path))
    with pytest.raises(cameras.CamerasError, match="does not exist"):
        cameras.read_jobs(tmp_path / "nope.json")
    job = {"scene": "vanilla-zoo", "world": "w", "cameras": "cameras.json", "out": "o"}
    with pytest.raises(cameras.CamerasError, match="used by another job"):
        cameras.read_jobs(_jobs(tmp_path, job, job))


def test_saves_are_copies_and_the_spec_lists_them(tmp_path: Path):
    world = _world(tmp_path / "w")
    mc = tmp_path / ".minecraft"
    (mc / "saves" / "oracle-9-stale").mkdir(parents=True)
    (mc / "saves" / "NEI").mkdir()
    _write(tmp_path / "cameras.json", CAMERAS)
    jobs = cameras.read_jobs(_jobs(tmp_path, {"scene": "vanilla-zoo", "world": "w", "cameras": "cameras.json", "out": "a"},
                                   {"scene": "vanilla-zoo", "world": "w", "cameras": "cameras.json", "out": "b"}))
    saves = cameras.prepare_saves(mc, jobs)
    assert saves == ["oracle-1-vanilla-zoo", "oracle-2-vanilla-zoo"]
    assert sorted(p.name for p in (mc / "saves").iterdir()) == ["NEI", *saves]  # stale save gone, others kept
    copy = mc / "saves" / saves[0]
    assert (copy / "region" / "r.0.0.mca").read_bytes() == b"chunks" and not (copy / "session.lock").exists()
    (copy / "level.dat").write_bytes(b"changed by the game")
    assert (world / "level.dat").read_bytes() == b"level" and (world / "session.lock").exists()  # snapshot untouched

    spec = cameras.build_spec(jobs, saves, session=tmp_path / "session.json")
    assert spec["captureVersion"] == cameras.CAPTURE_VERSION and spec["viewDistance"] == cameras.VIEW_DISTANCE == 8
    assert spec["renderDistance"] == 12 and spec["stableFrames"] >= 2 and "settleTicks" not in spec
    assert [j["save"] for j in spec["jobs"]] == saves
    assert spec["jobs"][0] == {"scene": "vanilla-zoo", "save": saves[0], "box": CAMERAS["box"], "cameras": CAMERAS["cameras"],
                               "remove": [], "freezeRandomTicks": True, "out": str(tmp_path / "a")}
    cameras.remove_saves(mc)
    assert [p.name for p in (mc / "saves").iterdir()] == ["NEI"]


def test_set_options_replaces_and_appends():
    text = "ao:2\nfov:0.0\nkey_key.jump:57\n"
    result = cameras.set_options(text, {"ao": "0", "clouds": "false"})
    assert result == "ao:0\nfov:0.0\nkey_key.jump:57\nclouds:false\n"


def test_capture_settings_restore_both_files(tmp_path: Path):
    mc = tmp_path / ".minecraft"
    (mc / "config").mkdir(parents=True)
    options = mc / "options.txt"
    options.write_text("ao:2\nmipmapLevels:4\n")
    angelica = mc / "config" / "angelica-options.json"
    original = {"quality": {"texel_sampling": "LINEAR", "biome_blend_radius": 3}, "performance": {"use_fog_occlusion": True}}
    angelica.write_text(json.dumps(original))
    with cameras.capture_settings(mc):
        lines = options.read_text().splitlines()
        assert "ao:0" in lines and "mipmapLevels:0" in lines and "pauseOnLostFocus:false" in lines
        data = json.loads(angelica.read_text())
        assert data["quality"] == {"texel_sampling": "NEAREST", "biome_blend_radius": 3, "texture_filter_mode": "NONE"}
        assert data["performance"]["use_fog_occlusion"] is False
    assert options.read_text() == "ao:2\nmipmapLevels:4\n"
    assert json.loads(angelica.read_text()) == original


def test_capture_settings_restore_on_error_and_remove_new_options(tmp_path: Path):
    mc = tmp_path / ".minecraft"
    mc.mkdir()
    with pytest.raises(RuntimeError):
        with cameras.capture_settings(mc):
            assert (mc / "options.txt").is_file()
            data = json.loads((mc / "config" / "angelica-options.json").read_text())
            assert data["quality"] == {"texel_sampling": "NEAREST", "texture_filter_mode": "NONE"}
            assert data["performance"] == {"use_fog_occlusion": False}
            raise RuntimeError("client failed")
    assert not (mc / "options.txt").exists()
    assert not (mc / "config" / "angelica-options.json").exists()


def test_outputs_are_cleaned_and_checked(tmp_path: Path):
    started = time.time() - 5
    for suffix in (".png", ".depth.bin", ".json"):
        (tmp_path / f"south{suffix}").write_text("old")
    cameras.clean_outputs(tmp_path, CAMERAS)
    assert not any(tmp_path.iterdir())
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [f"{tmp_path / cameras.REPORT} missing or older than this run"]
    # the client ended before the job's report (the watchdog killed a stuck client): the session's error says why
    stuck = "the client thread made no progress for 300 s (STABILIZE, job vanilla-zoo; its stack is in the log)"
    assert cameras.check_outputs(tmp_path, CAMERAS, started, stuck) == [
        f"{tmp_path / cameras.REPORT} missing or older than this run; client session error: {stuck}"]
    _write(tmp_path / cameras.REPORT, {"capture_version": cameras.CAPTURE_VERSION})
    for suffix in (".png", ".depth.bin"):
        (tmp_path / f"south{suffix}").write_text("new")
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [f"{tmp_path / 'south.json'} missing or stale"]
    (tmp_path / "south.json").write_text("{}")
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == []
    old = started - 100
    os.utime(tmp_path / "south.png", (old, old))
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [f"{tmp_path / 'south.png'} missing or stale"]
    os.utime(tmp_path / "south.png")
    _write(tmp_path / cameras.REPORT, {"capture_version": 1, "error": "disconnected"})
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [
        "job error: disconnected", f"{tmp_path / cameras.REPORT}: capture_version 1, expected {cameras.CAPTURE_VERSION}"]


def test_cameras_parser(monkeypatch):
    monkeypatch.delenv("BMGTNH_PACK_VERSION", raising=False)
    args = build_parser().parse_args(
        ["cameras", "--jobs", "j.json", "--resource-pack", "a.zip", "--extra-mod", "b.jar", "--parallel", "3"]
    )
    resolve_pack(args)
    assert args.jobs == Path("j.json") and args.parallel == 3 and args.bmh_jar is None
    assert args.resource_pack == [Path("a.zip")] and args.extra_mod == [Path("b.jar")]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cameras", "--scene", "gt-pipes"])  # the single-scene server mode is gone
