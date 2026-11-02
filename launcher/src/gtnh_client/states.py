"""Per-state rendering oracle, client side: the list of block states to draw and checks of the client's output
(`gtnh-client states`, exporter mod mode `states`; the comparison is `e2e oracle states` in the sibling e2e checkout)."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

BLOCKS_NDJSON = "gtnh/blocks.ndjson"
FLATTENING_TSV = "legacy/src/main/resources/de/bluecolored/bluemap/gtnh/legacy/flattening_1_7_10.tsv"
INDEX = "index.json"


class StatesError(Exception):
    pass


def modded_states(pack: Path) -> list[tuple[str, int]]:
    """Every block state of an exported pack's `gtnh/blocks.ndjson` (the exporter's own state discovery: metas
    0..15, sub-blocks, extra states; probed metas that do not exist are not listed), whatever the export result."""
    with zipfile.ZipFile(pack) as z:
        try:
            lines = z.read(BLOCKS_NDJSON).decode("utf-8").splitlines()
        except KeyError:
            raise StatesError(f"{pack} has no {BLOCKS_NDJSON}; is it a gtnh-exporter pack?") from None
    states = []
    for line in lines:
        if not line.strip():
            continue
        block = json.loads(line)
        states.extend((block["name"], int(m["meta"])) for m in block.get("metas", []))
    return states


def vanilla_states(flattening_tsv: Path) -> list[tuple[str, int]]:
    """The vanilla 1.7.10 states Mojang's 1.13 flattening table knows (`registered` = 1), air excluded."""
    states = []
    for line in flattening_tsv.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        block_id, name, meta, _state, registered = line.split("\t")
        if registered == "1" and block_id != "0":
            states.append((name, int(meta)))
    return states


def write_states(pack: Path, flattening_tsv: Path, out: Path, only: str | None = None) -> int:
    """Writes the `<modid:name> <meta>` list for the client (vanilla first) and returns the number of states."""
    pattern = re.compile(only) if only else None
    seen: set[tuple[str, int]] = set()
    lines = []
    for name, meta in [*vanilla_states(flattening_tsv), *modded_states(pack)]:
        if (name, meta) in seen:
            continue
        seen.add((name, meta))
        if pattern and not pattern.search(f"{name} {meta}"):
            continue
        lines.append(f"{name} {meta}\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8")
    return len(lines)


def check_outputs(out: Path, expected: int, started_wall: float) -> tuple[list[str], dict]:
    """Problems with the client's `index.json` (missing, stale, wrong count, missing images) and a summary."""
    index_file = out / INDEX
    if not index_file.is_file():
        return [f"{index_file} missing"], {}
    if index_file.stat().st_mtime < started_wall:
        return [f"{index_file} is stale (older than this run)"], {}
    index = json.loads(index_file.read_text(encoding="utf-8"))
    states = index.get("states", [])
    problems = []
    if len(states) != expected:
        problems.append(f"{len(states)} states in {index_file}, expected {expected}")
    missing = [s["path"] for s in states if not (out / f"{s['path']}.png").is_file()]
    if missing:
        problems.append(f"{len(missing)} states without an image, e.g. {missing[0]}")
    summary = {
        "states": len(states),
        "errors": sum(1 for s in states if s.get("error")),
        "blank": sum(1 for s in states if s.get("blank")),
        "tesr": sum(1 for s in states if s.get("tesr")),
        "seconds": index.get("seconds"),
    }
    return problems, summary
