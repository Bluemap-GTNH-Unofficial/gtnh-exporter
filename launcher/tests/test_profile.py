import json
import zipfile
from pathlib import Path

import pytest

from gtnh_client.cli import default_zip
from gtnh_client.launch import offline_uuid
from gtnh_client.profile import MACOS_ARM64, RuntimeContext, build_profile, classpath, rules_allow

TOP = "GT New Horizons 2.9.0-RC-2"


@pytest.fixture(scope="module")
def components() -> list[dict]:
    zip_path = default_zip()
    if not zip_path.is_file():
        pytest.skip(f"{zip_path} not present")
    with zipfile.ZipFile(zip_path) as zf:
        pack = json.loads(zf.read(f"{TOP}/mmc-pack.json"))
        return [json.loads(zf.read(f"{TOP}/patches/{c['uid']}.json")) for c in pack["components"]]


def osr(name: str, action: str = "allow") -> dict:
    return {"action": action, "os": {"name": name}}


def test_rules_osx_arm64_requires_precise_classifier():
    # macos-arm64 natives: allow osx + allow osx-arm64
    assert rules_allow([osr("osx"), osr("osx-arm64")], MACOS_ARM64)
    # x86_64 macos natives: only "osx" -> inactive on arm64 (not a legacy arch)
    assert not rules_allow([osr("osx")], MACOS_ARM64)
    assert rules_allow([osr("osx")], RuntimeContext("osx", "x86_64"))
    assert not rules_allow([osr("linux")], MACOS_ARM64)


def test_rules_default_and_last_match():
    assert rules_allow(None, MACOS_ARM64)
    assert rules_allow([{"action": "allow"}, osr("linux", "disallow")], MACOS_ARM64)
    assert not rules_allow([{"action": "allow"}, osr("osx-arm64", "disallow")], MACOS_ARM64)


def test_natives_resolution(components):
    profile = build_profile(components)
    natives = {lib.spec.artifact: lib.artifact.path for lib in profile.natives}
    # twitch-platform only ships "osx" natives (x86) -> inactive; jinput has an osx-arm64 classifier
    assert natives == {
        "jinput-platform": "net/java/jinput/jinput-platform/2.0.5/jinput-platform-2.0.5-natives-osx-arm64.jar"
    }


def test_classpath_order(components):
    profile = build_profile(components)
    names = [p.name for p in classpath(profile, Path("/libs"), Path("/local"))]
    assert len(names) == len(set(names))
    # lwjgl3 component first, x86_64 macos natives excluded, arm64 natives present
    assert names[0] == "lwjgl-freetype-natives-macos-arm64-3.4.3.jar"
    assert "lwjgl-natives-macos-3.4.3.jar" not in names
    assert "lwjgl-opengl-natives-macos-arm64-3.4.3.jar" in names
    assert not any("windows" in n or "linux" in n for n in names)
    # minecraft libs, guava upgraded in place by forge's 17.0
    assert "guava-15.0.jar" not in names
    assert names.index("guava-17.0.jar") < names.index("lwjgl3ify-3.0.37-forgePatches.jar")
    # lwjgl3ify forge patches (local) before forge universal, main jar last
    assert names.index("lwjgl3ify-3.0.37-forgePatches.jar") + 1 == names.index(
        "forge-1.7.10-10.13.4.1614-1.7.10-universal.jar"
    )
    assert names[-1] == "minecraft-1.7.10-client.jar"
    assert classpath(profile, Path("/libs"), Path("/local"))[names.index("lwjgl3ify-3.0.37-forgePatches.jar")] == Path(
        "/local/lwjgl3ify-3.0.37-forgePatches.jar"
    )


def test_profile_launch_fields(components):
    profile = build_profile(components)
    assert profile.main_class == "com.gtnewhorizons.retrofuturabootstrap.MainStartOnFirstThread"
    assert profile.tweakers == ["cpw.mods.fml.common.launcher.FMLTweaker"]
    assert "FirstThreadOnMacOS" in profile.traits
    assert "-Djava.system.class.loader=com.gtnewhorizons.retrofuturabootstrap.RfbSystemClassLoader" in profile.jvm_args
    assert profile.asset_index["id"] == "1.7.10"


def test_offline_uuid_matches_java():
    # values from java.util.UUID.nameUUIDFromBytes("OfflinePlayer:<name>".getBytes(UTF_8))
    assert offline_uuid("BlueMapExporter") == "114609c097c23fac907486bea01ecc81"
    assert offline_uuid("Notch") == "b50ad385829d3141a2167e7d7539ba7f"
