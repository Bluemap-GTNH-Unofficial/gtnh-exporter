"""Resolve a Prism/MultiMC OneSix instance (mmc-pack.json + patches/*.json) into a launch profile.

Mirrors Prism Launcher semantics for the subset of the component format GTNH uses:
rule evaluation (RuntimeContext::classifierMatches), native classifier lookup
(Library::getCompatibleNative), library de-duplication (LaunchProfile::applyLibrary) and
classpath order (libraries in component order, main jar last).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Component keys this resolver understands; anything else that would change the launch is rejected.
_IGNORED_KEYS = {
    "formatVersion", "name", "uid", "version", "order", "releaseTime", "type", "requires",
    "conflicts", "volatile", "compatibleJavaMajors", "compatibleJavaName",
}
_HANDLED_KEYS = {
    "libraries", "+jvmArgs", "+tweakers", "+traits", "mainClass", "mainJar",
    "minecraftArguments", "assetIndex",
}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class RuntimeContext:
    """Prism's RuntimeContext: `system` is the Mojang OS name, `arch` the mapped Java arch."""

    system: str = "osx"
    arch: str = "arm64"

    @property
    def classifier(self) -> str:
        return f"{self.system}-{self.arch}"

    @property
    def legacy_arch(self) -> bool:
        return self.arch in ("x86_64", "x86")

    def matches(self, target: str) -> bool:
        # Precise "[os]-[arch]" always; bare "[os]" only on the legacy (x86) architectures.
        return target == self.classifier or (self.legacy_arch and target == self.system)


MACOS_ARM64 = RuntimeContext("osx", "arm64")


def rules_allow(rules: list[dict] | None, ctx: RuntimeContext) -> bool:
    """Library::isActive rule part: no rules -> allowed; otherwise last applicable rule wins, default disallow."""
    if not rules:
        return True
    result = "disallow"
    for rule in rules:
        os_rule = rule.get("os")
        if os_rule is None or ctx.matches(os_rule.get("name", "")):
            result = rule["action"]
    return result == "allow"


@dataclass(frozen=True)
class GradleSpec:
    group: str
    artifact: str
    version: str
    classifier: str = ""
    extension: str = "jar"

    @classmethod
    def parse(cls, name: str) -> GradleSpec:
        extension = "jar"
        if "@" in name:
            name, extension = name.split("@", 1)
        parts = name.split(":")
        if len(parts) not in (3, 4):
            raise ProfileError(f"invalid gradle specifier {name!r}")
        return cls(*parts[:3], parts[3] if len(parts) == 4 else "", extension)

    def with_classifier(self, classifier: str) -> GradleSpec:
        return GradleSpec(self.group, self.artifact, self.version, classifier, self.extension)

    @property
    def artifact_prefix(self) -> str:
        prefix = f"{self.group}:{self.artifact}"
        return f"{prefix}:{self.classifier}" if self.classifier else prefix

    @property
    def filename(self) -> str:
        suffix = f"-{self.classifier}" if self.classifier else ""
        return f"{self.artifact}-{self.version}{suffix}.{self.extension}"

    @property
    def path(self) -> str:
        return f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}/{self.filename}"


@dataclass(frozen=True)
class Artifact:
    """One file to place on disk: maven-relative `path`, plus where to get it (url+sha1) or a local file."""

    path: str
    url: str | None = None
    sha1: str | None = None
    size: int | None = None
    local: bool = False


@dataclass(frozen=True)
class Library:
    spec: GradleSpec
    artifact: Artifact
    native: bool = False
    extract_exclude: tuple[str, ...] = ()


@dataclass
class Profile:
    libraries: list[Library] = field(default_factory=list)
    natives: list[Library] = field(default_factory=list)
    main_jar: Library | None = None
    main_class: str | None = None
    jvm_args: list[str] = field(default_factory=list)
    tweakers: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    minecraft_arguments: str | None = None
    asset_index: dict | None = None

    def artifacts(self) -> list[Artifact]:
        libs = [*self.libraries, *self.natives, *([self.main_jar] if self.main_jar else [])]
        return [lib.artifact for lib in libs]


def _version_key(version: str) -> list[tuple[int, int | str]]:
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_]", version) if p]


def _resolve_library(raw: dict, ctx: RuntimeContext) -> Library | None:
    if not rules_allow(raw.get("rules"), ctx):
        return None
    spec = GradleSpec.parse(raw["name"])
    downloads = raw.get("downloads", {})
    natives = raw.get("natives")
    if natives is not None:
        classifier = natives.get(ctx.classifier)
        if classifier is None and ctx.legacy_arch:
            classifier = natives.get(ctx.system)
        if classifier is None:
            return None  # Prism: a native library without a compatible classifier is inactive
        classifier = classifier.replace("${arch}", "64")
        info = downloads.get("classifiers", {}).get(classifier)
        if info is None:
            raise ProfileError(f"{raw['name']}: no download for native classifier {classifier}")
        native_spec = spec.with_classifier(classifier)
        artifact = Artifact(native_spec.path, info["url"], info.get("sha1"), info.get("size"))
        exclude = tuple(raw.get("extract", {}).get("exclude", ()))
        return Library(spec, artifact, native=True, extract_exclude=exclude)
    if raw.get("MMC-hint") == "local":
        return Library(spec, Artifact(spec.filename, local=True))
    info = downloads.get("artifact")
    if info is not None:
        return Library(spec, Artifact(spec.path, info["url"], info.get("sha1"), info.get("size")))
    if "url" in raw:
        return Library(spec, Artifact(spec.path, raw["url"].rstrip("/") + "/" + spec.path))
    raise ProfileError(f"{raw['name']}: no download information")


def _apply_library(target: list[Library], lib: Library) -> None:
    for i, existing in enumerate(target):
        if existing.spec.artifact_prefix == lib.spec.artifact_prefix:
            if _version_key(lib.spec.version) > _version_key(existing.spec.version):
                target[i] = lib
            return
    target.append(lib)


def build_profile(components: list[dict], ctx: RuntimeContext = MACOS_ARM64) -> Profile:
    """Apply component JSONs in order (Prism VersionFile::applyTo)."""
    profile = Profile()
    for comp in components:
        unknown = set(comp) - _IGNORED_KEYS - _HANDLED_KEYS
        if unknown:
            raise ProfileError(f"component {comp.get('uid')}: unsupported keys {sorted(unknown)}")
        for raw in comp.get("libraries", []):
            lib = _resolve_library(raw, ctx)
            if lib is not None:
                _apply_library(profile.natives if lib.native else profile.libraries, lib)
        if "mainJar" in comp:
            lib = _resolve_library(comp["mainJar"], ctx)
            if lib is None:
                raise ProfileError(f"component {comp.get('uid')}: main jar not active for {ctx.classifier}")
            profile.main_jar = lib
        profile.jvm_args += comp.get("+jvmArgs", [])
        profile.tweakers += comp.get("+tweakers", [])
        profile.traits += [t for t in comp.get("+traits", []) if t not in profile.traits]
        profile.main_class = comp.get("mainClass", profile.main_class)
        profile.minecraft_arguments = comp.get("minecraftArguments", profile.minecraft_arguments)
        profile.asset_index = comp.get("assetIndex", profile.asset_index)
    if profile.main_jar is None or profile.main_class is None or profile.minecraft_arguments is None:
        raise ProfileError("components define no mainJar / mainClass / minecraftArguments")
    return profile


def load_components(instance_dir: Path) -> list[dict]:
    """Component JSONs in mmc-pack.json order, read from the instance's patches/ directory."""
    pack = json.loads((instance_dir / "mmc-pack.json").read_text())
    components = []
    for comp in pack["components"]:
        patch = instance_dir / "patches" / f"{comp['uid']}.json"
        if not patch.is_file():
            raise ProfileError(f"{patch} missing (component {comp['uid']} {comp.get('version')})")
        components.append(json.loads(patch.read_text()))
    return components


def artifact_file(artifact: Artifact, libraries_dir: Path, local_dir: Path) -> Path:
    return (local_dir if artifact.local else libraries_dir) / artifact.path


def classpath(profile: Profile, libraries_dir: Path, local_dir: Path) -> list[Path]:
    """Libraries in component order, then the main jar (Prism LaunchProfile::getLibraryFiles)."""
    assert profile.main_jar is not None
    libs = [*profile.libraries, profile.main_jar]
    return [artifact_file(lib.artifact, libraries_dir, local_dir) for lib in libs]
