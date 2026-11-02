import json
import os
import time
from pathlib import Path

import pytest

from gtnh_client import cameras
from gtnh_client.cli import build_parser

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


def test_build_spec_adds_rcon_and_settle_settings(tmp_path: Path):
    spec = cameras.build_spec(CAMERAS, rcon_port=25578, password_file=tmp_path / ".rcon-password", view_distance=8)
    assert spec["rcon"] == {"host": "127.0.0.1", "port": 25578, "passwordFile": str(tmp_path / ".rcon-password")}
    assert spec["viewDistance"] == 8 and spec["renderDistance"] == 12
    assert spec["cameras"] == CAMERAS["cameras"] and spec["box"] == CAMERAS["box"]
    assert spec["stableFrames"] >= 2 and spec["settleTicks"] > 0


def test_server_view_distance(tmp_path: Path):
    assert cameras.server_view_distance(tmp_path) == 10
    (tmp_path / "server.properties").write_text("#x\nview-distance=8\nlevel-name=World\n")
    assert cameras.server_view_distance(tmp_path) == 8


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
    (tmp_path / cameras.REPORT).write_text("{}")
    for suffix in (".png", ".depth.bin"):
        (tmp_path / f"south{suffix}").write_text("new")
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [f"{tmp_path / 'south.json'} missing or stale"]
    (tmp_path / "south.json").write_text("{}")
    old = started - 100
    os.utime(tmp_path / "south.png", (old, old))
    assert cameras.check_outputs(tmp_path, CAMERAS, started) == [f"{tmp_path / 'south.png'} missing or stale"]


def test_cameras_parser():
    args = build_parser().parse_args(
        ["cameras", "--scene", "gt-pipes", "--resource-pack", "a.zip", "--extra-mod", "b.jar", "--port", "25568"]
    )
    assert args.scene == "gt-pipes" and args.variant == "default"
    assert args.resource_pack == [Path("a.zip")] and args.extra_mod == [Path("b.jar")]
    assert args.port == 25568 and args.rcon_port == 25575 and not args.running_server
