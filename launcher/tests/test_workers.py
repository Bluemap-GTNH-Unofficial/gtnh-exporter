import os
from pathlib import Path

from gtnh_client import workers
from gtnh_client.instance import Layout


def test_split_balances_cost_and_keeps_order():
    jobs = [("a", 10), ("b", 1), ("c", 6), ("d", 5), ("e", 1)]
    groups = workers.split(jobs, 2, cost=lambda j: j[1])
    assert groups == [[("a", 10), ("b", 1), ("e", 1)], [("c", 6), ("d", 5)]]  # 12 vs 11, each in input order
    assert workers.split(jobs, 1, cost=lambda j: j[1]) == [jobs]
    assert workers.split(jobs[:2], 4, cost=lambda j: j[1]) == [[("a", 10)], [("b", 1)]]  # no empty workers
    assert sorted(sum(workers.split(jobs, 3, cost=lambda j: j[1]), [])) == sorted(jobs)
    assert workers.split(jobs, 3, cost=lambda j: j[1]) == workers.split(jobs, 3, cost=lambda j: j[1])


def test_worker_layout_shares_the_pack_and_owns_its_game_dir(tmp_path: Path):
    base = Layout(tmp_path / "clients" / "gtnh-2.9.0", tmp_path / "cache")
    layout = workers.worker_layout(base, 2)
    worker = tmp_path / "clients" / "gtnh-2.9.0-worker-2"
    assert workers.worker_dir(base.client_dir, 2) == worker
    assert layout.minecraft_dir == worker / ".minecraft" and layout.instance_dir == worker
    assert (layout.local_libraries, layout.natives_dir, layout.libraries_cache, layout.assets_dir) == (
        base.local_libraries, base.natives_dir, base.libraries_cache, base.assets_dir)
    assert base.instance_dir == base.client_dir and base.minecraft_dir == base.client_dir / ".minecraft"


def _tree(root: Path) -> dict[str, bytes | str]:
    result = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[key] = "-> " + os.readlink(path)
        elif path.is_file():
            result[key] = path.read_bytes()
    return result


def test_sync_worker_mirrors_everything_but_instance_state(tmp_path: Path):
    base = Layout(tmp_path / "base", tmp_path / "cache")
    mc = base.minecraft_dir
    (mc / "mods" / "1.7.10").mkdir(parents=True)
    (mc / "config" / "deep").mkdir(parents=True)
    (mc / "mods" / "a.jar").write_bytes(b"a")
    (mc / "mods" / "1.7.10" / "b.jar").write_bytes(b"b")
    (mc / "config" / "deep" / "c.cfg").write_text("c")
    (mc / "options.txt").write_text("fov:0\n")
    (mc / "saves" / "NEI").mkdir(parents=True)
    (mc / "logs").mkdir()
    (mc / "logs" / "latest.log").write_text("base log")
    (mc / "link").symlink_to("mods/a.jar")

    layout, stats = workers.sync_worker(base, 1)
    worker = layout.minecraft_dir
    assert worker == tmp_path / "base-worker-1" / ".minecraft"
    expected = {k: v for k, v in _tree(mc).items() if not k.startswith(("saves", "logs"))}
    assert _tree(worker) == expected and stats.copied > 0
    assert not (worker / "saves").exists() and not (worker / "logs").exists()

    # the worker's own state survives, its other changes are undone, base changes arrive
    (worker / "saves" / "oracle-1-x").mkdir(parents=True)
    (worker / "logs").mkdir()
    (worker / "logs" / "latest.log").write_text("worker log")
    (worker / "config" / "deep" / "c.cfg").write_text("rewritten by a mod at boot")
    (worker / "mods" / "bluemap-gtnh-exporter-0.2.0.jar").write_bytes(b"installed for a run")
    (mc / "mods" / "a.jar").unlink()
    (mc / "mods" / "new.jar").write_bytes(b"new")
    (mc / "link").unlink()
    (mc / "link").mkdir()
    layout, stats = workers.sync_worker(base, 1)
    assert _tree(worker) | {} == {**{k: v for k, v in _tree(mc).items() if not k.startswith(("saves", "logs"))},
                                  "logs/latest.log": b"worker log"}
    assert (worker / "saves" / "oracle-1-x").is_dir() and (worker / "link").is_dir()
    assert stats.removed >= 2

    layout, stats = workers.sync_worker(base, 1)
    assert (stats.copied, stats.removed) == (0, 0)  # nothing changed: nothing copied
