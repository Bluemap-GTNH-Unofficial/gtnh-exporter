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
# Version of the per-state capture (the mod's StateCapture and its settings): bump it whenever it can change the
# outputs. The e2e reference cache reads this literal (with `ast`) into its keys.
STATES_CAPTURE_VERSION = 1
# StateCapture draws this many states per framebuffer batch; shards start on a batch boundary, so every state is drawn
# in the same batch as in a single-client run.
BATCH = 64


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


def shard_lines(text: str, workers: int) -> list[list[str]]:
    """The state list's lines cut into at most `workers` contiguous shards of whole batches ([BATCH] states), in
    order, as even as whole batches allow; no shard is empty."""
    lines = text.splitlines(keepends=True)
    batches = [lines[i:i + BATCH] for i in range(0, len(lines), BATCH)]
    count = max(1, min(workers, len(batches)))
    size, extra = divmod(len(batches), count)
    shards, start = [], 0
    for i in range(count):
        end = start + size + (1 if i < extra else 0)
        shards.append([line for batch in batches[start:end] for line in batch])
        start = end
    return shards


def merge_indexes(shards: list[Path], out: Path, started_wall: float) -> None:
    """Writes `out` (`index.json`) from the shard indexes of a parallel run, in shard order: the states concatenated,
    `seconds` the slowest shard's drawing time, everything else from the first shard."""
    merged: dict | None = None
    for path in shards:
        if not path.is_file() or path.stat().st_mtime < started_wall:
            raise StatesError(f"shard index {path} missing or older than this run")
        index = json.loads(path.read_text(encoding="utf-8"))
        if merged is None:
            merged = index
        else:
            merged["states"].extend(index["states"])
            merged["seconds"] = max(merged.get("seconds", 0), index.get("seconds", 0))
    assert merged is not None
    partial = out.with_name(out.name + ".part")
    partial.write_text(json.dumps(merged, indent=1) + "\n", encoding="utf-8")
    partial.replace(out)


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
