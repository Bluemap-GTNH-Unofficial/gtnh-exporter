import json
import time
import zipfile

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
