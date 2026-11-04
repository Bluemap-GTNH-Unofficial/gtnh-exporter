import json
import zipfile
from pathlib import Path

import pytest

from gtnh_client import packs
from gtnh_client.cli import default_zip
from gtnh_client.launch import offline_uuid
from gtnh_client.profile import LINUX_X86_64, MACOS_ARM64, RuntimeContext, build_profile, classpath, rules_allow


@pytest.fixture(scope="module", params=list(packs.PACKS))
def components(request) -> list[dict]:
    """The MultiMC components of the instance zip of every pack version present in WS/downloads."""
    zip_path = default_zip(packs.get(request.param))
    if not zip_path.is_file():
        pytest.skip(f"{zip_path} not present")
    top = f"GT New Horizons {request.param}"
    with zipfile.ZipFile(zip_path) as zf:
        pack = json.loads(zf.read(f"{top}/mmc-pack.json"))
        return [json.loads(zf.read(f"{top}/patches/{c['uid']}.json")) for c in pack["components"]]


def lwjgl3ify_patches(components: list[dict]) -> str:
    """The instance-local lwjgl3ify forge patches jar (its version changes between pack versions)."""
    (version,) = [c["version"] for c in components if c["uid"] == "me.eigenraven.lwjgl3ify.forgepatches"]
    return f"lwjgl3ify-{version}-forgePatches.jar"


def osr(name: str, action: str = "allow") -> dict:
    return {"action": action, "os": {"name": name}}


def test_rules_osx_arm64_requires_precise_classifier():
    # macos-arm64 natives: allow osx + allow osx-arm64
    assert rules_allow([osr("osx"), osr("osx-arm64")], MACOS_ARM64)
    # x86_64 macos natives: only "osx" -> inactive on arm64 (not a legacy arch)
    assert not rules_allow([osr("osx")], MACOS_ARM64)
    assert rules_allow([osr("osx")], RuntimeContext("osx", "x86_64"))
    assert not rules_allow([osr("linux")], MACOS_ARM64)


@pytest.mark.parametrize(("platform", "machine", "expected"), [
    ("darwin", "arm64", MACOS_ARM64),
    ("linux", "x86_64", LINUX_X86_64),
    ("linux", "aarch64", RuntimeContext("linux", "arm64")),
    ("win32", "AMD64", RuntimeContext("windows", "x86_64")),
])
def test_host_context_uses_prism_names(monkeypatch, platform, machine, expected):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert RuntimeContext.host() == expected


def test_rules_default_and_last_match():
    assert rules_allow(None, MACOS_ARM64)
    assert rules_allow([{"action": "allow"}, osr("linux", "disallow")], MACOS_ARM64)
    assert not rules_allow([{"action": "allow"}, osr("osx-arm64", "disallow")], MACOS_ARM64)


def test_natives_resolution(components):
    profile = build_profile(components, MACOS_ARM64)
    natives = {lib.spec.artifact: lib.artifact.path for lib in profile.natives}
    # twitch-platform only ships "osx" natives (x86) -> inactive; jinput has an osx-arm64 classifier
    assert natives == {
        "jinput-platform": "net/java/jinput/jinput-platform/2.0.5/jinput-platform-2.0.5-natives-osx-arm64.jar"
    }


def test_classpath_order(components):
    profile = build_profile(components, MACOS_ARM64)
    names = [p.name for p in classpath(profile, Path("/libs"), Path("/local"))]
    patches = lwjgl3ify_patches(components)
    # lwjgl3 component first, x86_64 macos natives excluded, arm64 natives present
    assert names[0] == "lwjgl-freetype-natives-macos-arm64-3.4.3.jar"
    assert "lwjgl-natives-macos-3.4.3.jar" not in names
    assert "lwjgl-opengl-natives-macos-arm64-3.4.3.jar" in names
    assert not any("windows" in n or "linux" in n for n in names)
    # minecraft libs, guava upgraded in place by forge's 17.0
    assert "guava-15.0.jar" not in names
    assert names.index("guava-17.0.jar") < names.index(patches)
    # lwjgl3ify forge patches (local) before forge universal, main jar last
    assert names.index(patches) + 1 == names.index("forge-1.7.10-10.13.4.1614-1.7.10-universal.jar")
    assert names[-1] == "minecraft-1.7.10-client.jar"
    assert classpath(profile, Path("/libs"), Path("/local"))[names.index(patches)] == Path(f"/local/{patches}")


def test_linux_x86_64_natives(components):
    # the container: bare "linux" rules and classifiers apply (x86_64 is a legacy arch), no macOS or Windows jar
    profile = build_profile(components, LINUX_X86_64)
    natives = {lib.spec.artifact: lib.artifact.path for lib in profile.natives}
    assert natives == {
        "jinput-platform": "net/java/jinput/jinput-platform/2.0.5/jinput-platform-2.0.5-natives-linux.jar"
    }
    names = [p.name for p in classpath(profile, Path("/libs"), Path("/local"))]
    assert {"lwjgl-natives-linux-3.4.3.jar", "lwjgl-opengl-natives-linux-3.4.3.jar",
            "lwjgl-sdl-natives-linux-3.4.3.jar"} <= set(names)
    assert not any("macos" in n or "windows" in n or "linux-arm64" in n for n in names)


def test_windows_x86_64_natives(components):
    # the Windows host: bare "windows" rules and classifiers apply (x86_64 is a legacy arch); Prism puts the x86 and
    # arm64 LWJGL jars on the classpath too (their rules allow bare "windows"), LWJGL picks its architecture at runtime
    profile = build_profile(components, RuntimeContext("windows", "x86_64"))
    natives = {lib.spec.artifact: lib.artifact.path for lib in profile.natives}
    assert natives["jinput-platform"] == "net/java/jinput/jinput-platform/2.0.5/jinput-platform-2.0.5-natives-windows.jar"
    assert natives["twitch-platform"].endswith("twitch-platform-5.16-natives-windows-64.jar")
    names = [p.name for p in classpath(profile, Path("/libs"), Path("/local"))]
    assert {"lwjgl-natives-windows-3.4.3.jar", "lwjgl-opengl-natives-windows-3.4.3.jar",
            "lwjgl-sdl-natives-windows-3.4.3.jar"} <= set(names)
    assert not any("macos" in n or "linux" in n for n in names)


def test_profile_launch_fields(components):
    profile = build_profile(components, MACOS_ARM64)
    assert profile.main_class == "com.gtnewhorizons.retrofuturabootstrap.MainStartOnFirstThread"
    assert profile.tweakers == ["cpw.mods.fml.common.launcher.FMLTweaker"]
    assert "FirstThreadOnMacOS" in profile.traits
    assert "-Djava.system.class.loader=com.gtnewhorizons.retrofuturabootstrap.RfbSystemClassLoader" in profile.jvm_args
    assert profile.asset_index["id"] == "1.7.10"


def test_offline_uuid_matches_java():
    # values from java.util.UUID.nameUUIDFromBytes("OfflinePlayer:<name>".getBytes(UTF_8))
    assert offline_uuid("BlueMapExporter") == "114609c097c23fac907486bea01ecc81"
    assert offline_uuid("Notch") == "b50ad385829d3141a2167e7d7539ba7f"
