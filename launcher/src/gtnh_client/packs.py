"""GTNH pack versions: the instance zip and client directory the launcher prepares, the scene server whose harness mod
the oracle cameras load, and where exports, scene outputs and oracle captures go.

Every per-version workspace directory is ``<base><suffix>`` (``out/exporter-2.9.0``, ``worlds-2.9.0``,
``out/oracle-2.9.0``, ...), the layout the sibling orchestrator and e2e checkouts use too. Add a version to ``PACKS``
to test it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VERSION = "2.9.0"


@dataclass(frozen=True)
class Pack:
    version: str
    suffix: str

    def dir(self, ws: Path, base: str) -> Path:
        """``<ws>/<base><suffix>``, e.g. ``out/exporter-2.9.0``."""
        return ws / f"{base}{self.suffix}"

    @property
    def zip_name(self) -> str:
        """The MultiMC/Prism instance zip from downloads.gtnewhorizons.com (Java 17-26 build)."""
        return f"GT_New_Horizons_{self.version}_Java_17-26.zip"

    @property
    def client(self) -> str:
        """Directory under ``<ws>/clients``."""
        return f"gtnh-{self.version}"

    @property
    def server(self) -> str:
        """Directory under ``<ws>/servers`` with the plain pack."""
        return f"gtnh-{self.version}"


PACKS = {
    "2.9.0": Pack("2.9.0", "-2.9.0"),
}


def get(version: str | None = None) -> Pack:
    """``version``'s layout; without one $BMGTNH_PACK_VERSION, else DEFAULT_VERSION."""
    version = version or os.environ.get("BMGTNH_PACK_VERSION") or DEFAULT_VERSION
    if version not in PACKS:
        raise ValueError(f"unknown pack version {version!r}; known: {', '.join(PACKS)}")
    return PACKS[version]


def resources_zip(version: str, suffix: str | None = None) -> str:
    """The exporter's pack name: ``bluemap-gtnh-resources-<version>[-<suffix>].zip``."""
    return f"bluemap-gtnh-resources-{version}" + (f"-{suffix}" if suffix else "") + ".zip"


_MOD_PACK_VERSION = re.compile(r"^\s*S:ModPackVersion=(\S+)\s*$", re.MULTILINE)


def client_version(minecraft_dir: Path) -> str | None:
    """The version a client instance reports itself (GTNH Core Mod's ``S:ModPackVersion`` in
    ``config/GTNewHorizons/dreamcraft.cfg``, which the exporter mod names its pack after), None if unknown."""
    config = minecraft_dir / "config" / "GTNewHorizons" / "dreamcraft.cfg"
    if not config.is_file():
        return None
    match = _MOD_PACK_VERSION.search(config.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None
