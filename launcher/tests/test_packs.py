from pathlib import Path

import pytest

from gtnh_client import cli, packs


def _client(root: Path, version: str) -> Path:
    config = root / ".minecraft" / "config" / "GTNewHorizons" / "dreamcraft.cfg"
    config.parent.mkdir(parents=True)
    config.write_text(f"general {{\n    S:ModPackVersion={version}\n}}\n")
    return root


def test_pack_layouts(tmp_path: Path) -> None:
    stable = packs.get("2.9.0")
    assert (stable.zip_name, stable.client, stable.server) == (
        "GT_New_Horizons_2.9.0_Java_17-26.zip", "gtnh-2.9.0", "gtnh-2.9.0")
    assert stable.dir(tmp_path, "out/exporter") == tmp_path / "out" / "exporter-2.9.0"
    assert stable.dir(tmp_path, "worlds") == tmp_path / "worlds-2.9.0"
    assert packs.resources_zip("2.9.0") == "bluemap-gtnh-resources-2.9.0.zip"
    assert packs.resources_zip("2.9.0", "addons") == "bluemap-gtnh-resources-2.9.0-addons.zip"


def test_default_version_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BMGTNH_PACK_VERSION", raising=False)
    assert packs.get().version == packs.DEFAULT_VERSION == "2.9.0"
    with pytest.raises(ValueError, match="unknown pack version"):
        packs.get("2.8.4")


def test_client_version_reads_dreamcraft_cfg(tmp_path: Path) -> None:
    assert packs.client_version(_client(tmp_path / "c", "2.9.0-RC-2") / ".minecraft") == "2.9.0-RC-2"
    assert packs.client_version(tmp_path / "missing") is None


def test_resolve_pack_follows_flag_then_client_then_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BMGTNH_PACK_VERSION", raising=False)
    monkeypatch.setenv("BMGTNH_WORKSPACE", str(tmp_path))
    old_client = _client(tmp_path / "clients" / "old", "2.9.0-RC-2")  # a version no longer in packs.PACKS
    stable_client = _client(tmp_path / "clients" / "mine", "2.9.0")

    def resolved(*argv: str):
        args = cli.build_parser().parse_args(list(argv))
        cli.resolve_pack(args)
        return args

    args = resolved("cameras", "--scene", "vanilla-zoo")
    assert (args.gtnh_pack.version, args.server_dir, args.port, args.rcon_port) == ("2.9.0", "gtnh-2.9.0", 25569, 25579)
    args = resolved("cameras", "--scene", "vanilla-zoo", "--client-dir", str(stable_client))
    assert (args.gtnh_pack.version, args.server_dir, args.port) == ("2.9.0", "gtnh-2.9.0", 25569)
    args = resolved("coverage", "--pack-version", "2.9.0")
    assert (args.scene, args.world, args.pack) == (
        tmp_path / "out" / "scenes-2.9.0" / "gtnh-zoo", tmp_path / "worlds-2.9.0" / "gtnh-zoo",
        tmp_path / "out" / "exporter-2.9.0" / "bluemap-gtnh-resources-2.9.0.zip")
    with pytest.raises(cli.CliError, match="is a GTNH 2.9.0-RC-2 client"):
        resolved("export", "--client-dir", str(old_client), "--pack-version", "2.9.0")
