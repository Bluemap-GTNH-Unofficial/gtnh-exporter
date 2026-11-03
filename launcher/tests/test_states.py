import json
import time
import zipfile

import pytest

from gtnh_client import states


def _pack(tmp_path):
    pack = tmp_path / "pack.zip"
    blocks = [
        {"name": "IC2:blockOreCopper", "metas": [{"meta": 0, "result": "cube"}]},
        {"name": "gregtech:gt.blockmachines", "metas": [{"meta": 4, "result": "failed"}, {"meta": 1002, "result": "quads"}]},
        {"name": "minecraft:stone", "metas": [{"meta": 0}]},  # never exported, but a duplicate must not repeat
    ]
    with zipfile.ZipFile(pack, "w") as z:
        z.writestr(states.BLOCKS_NDJSON, "\n".join(json.dumps(b) for b in blocks) + "\n")
    tsv = tmp_path / "flattening.tsv"
    tsv.write_text("0\tminecraft:air\t0\tminecraft:air\t1\n"
                   "1\tminecraft:stone\t0\tminecraft:stone\t1\n"
                   "1\tminecraft:stone\t7\tminecraft:stone\t0\n"
                   "3\tminecraft:dirt\t2\tminecraft:podzol[snowy=false]\t1\n")
    return pack, tsv


def test_write_states_lists_vanilla_then_exported_states(tmp_path):
    pack, tsv = _pack(tmp_path)
    out = tmp_path / "states.txt"
    assert states.write_states(pack, tsv, out) == 5
    assert out.read_text().splitlines() == [
        "minecraft:stone 0", "minecraft:dirt 2", "IC2:blockOreCopper 0", "gregtech:gt.blockmachines 4",
        "gregtech:gt.blockmachines 1002",
    ]
    assert states.write_states(pack, tsv, out, only=r"^gregtech:\S+ \d{4}$") == 1
    assert out.read_text() == "gregtech:gt.blockmachines 1002\n"


def test_check_outputs(tmp_path):
    started = time.time() - 1
    (tmp_path / "ns" / "block").mkdir(parents=True)
    (tmp_path / "ns" / "block" / "0.png").write_bytes(b"")
    index = {"states": [{"path": "ns/block/0", "tesr": True}, {"path": "ns/block/1", "error": "boom", "blank": True}]}
    (tmp_path / states.INDEX).write_text(json.dumps({**index, "seconds": 2.5}))
    problems, summary = states.check_outputs(tmp_path, 2, started)
    assert problems == ["1 states without an image, e.g. ns/block/1"]
    assert summary == {"states": 2, "errors": 1, "blank": 1, "tesr": 1, "seconds": 2.5}
    problems, _ = states.check_outputs(tmp_path, 3, time.time() + 60)
    assert problems and "stale" in problems[0]


def test_shard_lines_cuts_whole_batches_in_order():
    lines = [f"mod:block {i}\n" for i in range(5 * states.BATCH + 3)]  # 6 batches, the last one short
    shards = states.shard_lines("".join(lines), 4)
    assert [len(s) for s in shards] == [2 * states.BATCH, 2 * states.BATCH, states.BATCH, 3]
    assert sum(shards, []) == lines
    assert all(len(s) % states.BATCH == 0 for s in shards[:-1])
    assert states.shard_lines("".join(lines[:10]), 4) == [lines[:10]]  # one batch: one shard
    assert states.shard_lines("a 0\nb 1\n", 1) == [["a 0\n", "b 1\n"]]


def test_merge_indexes_concatenates_shards(tmp_path):
    started = time.time() - 1
    first, second = tmp_path / "w1.json", tmp_path / "w2.json"
    first.write_text(json.dumps({"format": 1, "image": {"width": 2}, "seconds": 3.0, "states": [{"path": "a/b/0"}]}))
    second.write_text(json.dumps({"format": 1, "image": {"width": 2}, "seconds": 4.5, "states": [{"path": "c/d/1"}]}))
    out = tmp_path / states.INDEX
    states.merge_indexes([first, second], out, started)
    assert json.loads(out.read_text()) == {"format": 1, "image": {"width": 2}, "seconds": 4.5,
                                           "states": [{"path": "a/b/0"}, {"path": "c/d/1"}]}
    with pytest.raises(states.StatesError, match="missing or older"):
        states.merge_indexes([first, tmp_path / "w3.json"], out, started)
