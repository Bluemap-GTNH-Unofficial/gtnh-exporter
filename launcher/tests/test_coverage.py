import json
from pathlib import Path

from gtnh_client.coverage import compare, normalize_registry_name, placed_states

VECTORS = Path(__file__).resolve().parents[4] / "testing" / "fixtures" / "resource-key-vectors.json"


def test_normalization_matches_golden_vectors():
    vectors = json.loads(VECTORS.read_text())["vectors"]
    assert vectors
    for v in vectors:
        assert normalize_registry_name(v["input"]) == (v["namespace"], v["value"]), v["input"]


def test_compare_classifies_placed_states(tmp_path):
    dump = tmp_path / "dump.ndjson"
    dump.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"name": "BiomesOPlenty:colorizedLeaves2", "meta": 1},
                {"name": "BiomesOPlenty:colorizedLeaves2", "meta": 1},  # duplicates count once
                {"name": "gregtech:gt.blockcasings2", "meta": 3},
                {"name": "gregtech:gt.blockcasings2", "meta": 4},
                {"name": "minecraft:stone", "meta": 0},  # vanilla is not counted
            ]
        )
    )
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "states": {
                    "~biomes~o~plenty": {"ok": ["~biomes~o~plenty:colorized~leaves2[meta=1]"]},
                    "gregtech": {"fallback": ["gregtech:gt.blockcasings2[meta=3]"]},
                }
            }
        )
    )
    assert set(placed_states(dump)) == {
        ("BiomesOPlenty:colorizedLeaves2", 1),
        ("gregtech:gt.blockcasings2", 3),
        ("gregtech:gt.blockcasings2", 4),
    }
    result = compare(dump, report)
    assert (result["placed"], result["rendered"]) == (3, 1)
    assert result["mods"]["gregtech"]["statuses"] == {"fallback": 1, "not_seen": 1}
