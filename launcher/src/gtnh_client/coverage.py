"""Render coverage of an exported resource pack: render a scene world with the BlueMap CLI (legacy reader +
gtnh-render addons + the pack) and compare the block states BlueMap met with the scene's ground-truth dump."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

JAVA_TOOL = "java@temurin-25.0.5+8.0.LTS"
COVERAGE_PROPERTY = "bluemap.gtnh.coverageReport"
RENDERED = ("ok",)
STATUSES = ("ok", "fallback", "missing_model", "no_variant", "no_blockstate", "not_seen")


# --- resource-key normalization N (contract C1; golden vectors in BlueMap/testing/fixtures/resource-key-vectors.json) --

def normalize(segment: str) -> str:
    out = []
    for unit in _utf16_units(segment):
        c = chr(unit) if unit < 0xD800 or unit > 0xDFFF else None
        if c is not None and ("a" <= c <= "z" or "0" <= c <= "9" or c in "_.-"):
            out.append(c)
        elif c is not None and "A" <= c <= "Z":
            out.append("~" + c.lower())
        elif unit <= 0xFF:
            out.append(f"!{unit:02x}")
        else:
            out.append(f"!u{unit:04x}")
    return "".join(out)


def _utf16_units(s: str):
    data = s.encode("utf-16-be")
    for i in range(0, len(data), 2):
        yield (data[i] << 8) | data[i + 1]


def normalize_registry_name(name: str) -> tuple[str, str]:
    namespace, sep, value = name.partition(":")
    if not sep:
        namespace, value = "minecraft", name
    return normalize(namespace), normalize(value)


# --- BlueMap CLI config -------------------------------------------------------------------------------------------

def write_config(
    config: Path,
    *,
    world: Path,
    packs: list[Path],
    box: list[int] | None,
    port: int,
    render_threads: int = 8,
) -> None:
    if config.exists():
        shutil.rmtree(config)
    (config / "maps").mkdir(parents=True)
    (config / "packs").mkdir()
    (config / "storages").mkdir()
    for pack in packs:
        shutil.copy2(pack, config / "packs" / pack.name)
    (config / "core.conf").write_text(
        "accept-download: true\n"
        'data: "data"\n'
        f"render-thread-count: {render_threads}\n"
        "scan-for-mod-resources: false\n"
        "metrics: false\n"
        'log: { file: "data/logs/debug.log", append: false }\n'
    )
    (config / "webapp.conf").write_text('enabled: true\nwebroot: "web"\nupdate-settings-file: true\nuse-cookies: true\n')
    (config / "webserver.conf").write_text(
        f'enabled: true\nwebroot: "web"\nip: "127.0.0.1"\nport: {port}\n'
        'log: { file: "data/logs/webserver.log", append: false }\n'
    )
    (config / "storages" / "file.conf").write_text('storage-type: file\nroot: "web/maps"\ncompression: gzip\n')
    mask = ""
    if box:
        x1, y1, z1, x2, y2, z2 = box
        mask = (
            f"render-mask: [{{ min-x: {min(x1, x2) - 2}, max-x: {max(x1, x2) + 2}, "
            f"min-z: {min(z1, z2) - 2}, max-z: {max(z1, z2) + 2} }}]\n"
        )
    (config / "maps" / "overworld.conf").write_text(
        f'world: "{world}"\n'
        'loader: "gtnh:anvil_1_7"\n'
        'dimension: "minecraft:overworld"\n'
        f'name: "{world.name}"\n'
        "sorting: 0\n"
        "start-pos: { x: 128, z: 128 }\n"
        'sky-color: "#7dabff"\n'
        "sky-light: 1\n"
        "ambient-light: 0.1\n"
        "remove-caves-below-y: -10000\n"
        "cave-detection-uses-block-light: false\n"
        "min-inhabited-time: 0\n"
        + mask
        + "render-edges: true\n"
        "enable-perspective-view: true\n"
        "enable-flat-view: true\n"
        "enable-free-flight-view: true\n"
        "enable-hires: true\n"
        'storage: "file"\n'
        "ignore-missing-light-data: false\n"
    )


def cli_command(cli_jar: Path, config: Path, *, extra_jvm: list[str], args: list[str]) -> list[str]:
    return ["mise", "exec", JAVA_TOOL, "--", "java", "-Xmx8G", *extra_jvm, "-jar", str(cli_jar), "-c", str(config), *args]


def render(cli_jar: Path, out: Path, report: Path) -> float:
    """Force-renders the map in `out/config`; returns seconds."""
    if report.exists():
        report.unlink()
    for stale in (out / "web" / "maps", out / "data"):
        if stale.exists():
            shutil.rmtree(stale)
    cmd = cli_command(cli_jar, out / "config", extra_jvm=[f"-D{COVERAGE_PROPERTY}={report}"], args=["-r", "-f", "-g"])
    print(f"[coverage] {subprocess.list2cmdline(cmd)} (cwd {out})", flush=True)
    t0 = time.monotonic()
    with (out / "render.log").open("w") as log:
        code = subprocess.run(cmd, cwd=out, stdout=log, stderr=subprocess.STDOUT).returncode
    if code != 0:
        raise RuntimeError(f"BlueMap CLI render failed with {code}, see {out / 'render.log'}")
    if not report.is_file():
        raise RuntimeError(f"no coverage report written to {report} (is the gtnh-render addon in packs/?)")
    return time.monotonic() - t0


# --- comparison ---------------------------------------------------------------------------------------------------

@dataclass
class ModCoverage:
    placed: int = 0
    statuses: Counter = field(default_factory=Counter)
    #: exporter result of the rendered states (cube / quads / bbox) and exporter reason of the others
    kinds: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=lambda: defaultdict(list))

    @property
    def rendered(self) -> int:
        return sum(self.statuses[s] for s in RENDERED)

    def to_json(self) -> dict:
        return {
            "placed": self.placed,
            "rendered": self.rendered,
            "ratio": round(self.rendered / self.placed, 4) if self.placed else None,
            "statuses": dict(self.statuses),
            "exporter": dict(self.kinds),
            "examples": {k: v[:20] for k, v in self.examples.items() if k not in RENDERED},
        }


def exporter_results(pack: Path) -> dict[tuple[str, int], str]:
    """(name, meta) -> exporter result (`cube`, `quads`, `bbox`) or `failed: <reason>`, from the pack's
    `gtnh/blocks.ndjson`."""
    results = {}
    with zipfile.ZipFile(pack) as z, z.open("gtnh/blocks.ndjson") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            block = json.loads(line)
            for m in block["metas"]:
                result = m.get("result", "?")
                if result == "failed":
                    result = "failed: " + m.get("reason", "?").split(":", 1)[0]
                results[(block["name"], m["meta"])] = result
    return results


def placed_states(dump: Path) -> dict[tuple[str, int], str]:
    """Distinct non-vanilla (name, meta) in the dump -> BlueMap state string (normalized, C1)."""
    states = {}
    with dump.open() as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            name, meta = entry["name"], int(entry["meta"])
            if name.startswith("minecraft:") or (name, meta) in states:
                continue
            namespace, value = normalize_registry_name(name)
            states[(name, meta)] = f"{namespace}:{value}[meta={meta}]"
    return states


def write_extra_states(dumps: list[Path], file: Path) -> int:
    """Writes the distinct non-vanilla `(name, meta)` of world dumps as exporter extra states; returns the count."""
    states: set[tuple[str, int]] = set()
    for dump in dumps:
        states.update(placed_states(dump))
    file.write_text("".join(f"{name} {meta}\n" for name, meta in sorted(states)))
    return len(states)


def report_statuses(report: Path) -> dict[str, str]:
    data = json.loads(report.read_text())
    result = {}
    for by_status in data["states"].values():
        for status, states in by_status.items():
            for state in states:
                result[state] = status
    return result


def compare(dump: Path, report: Path, pack: Path | None = None) -> dict:
    statuses = report_statuses(report)
    results = exporter_results(pack) if pack else {}
    mods: dict[str, ModCoverage] = defaultdict(ModCoverage)
    for (name, meta), state in sorted(placed_states(dump).items()):
        mod = mods[name.split(":", 1)[0]]
        status = statuses.get(state, "not_seen")
        mod.placed += 1
        mod.statuses[status] += 1
        kind = results.get((name, meta), "not exported")
        mod.kinds[kind if status in RENDERED else f"{status} ({kind})"] += 1
        mod.examples[status].append(f"{name}:{meta} ({kind})")
    placed = sum(m.placed for m in mods.values())
    rendered = sum(m.rendered for m in mods.values())
    total = Counter()
    kinds = Counter()
    for m in mods.values():
        total.update(m.statuses)
        kinds.update(m.kinds)
    worst = sorted(mods.items(), key=lambda kv: (kv[1].rendered / kv[1].placed, -kv[1].placed))
    return {
        "placed": placed,
        "rendered": rendered,
        "ratio": round(rendered / placed, 4) if placed else None,
        "statuses": dict(total),
        "exporter": dict(kinds.most_common()),
        "worst": [
            {"mod": k, "placed": v.placed, "rendered": v.rendered, "ratio": round(v.rendered / v.placed, 4)}
            for k, v in worst[:25]
            if v.rendered < v.placed
        ],
        "mods": {k: v.to_json() for k, v in sorted(mods.items())},
    }


def print_summary(result: dict) -> None:
    print(
        f"[coverage] non-vanilla placed states: {result['placed']}, rendered with a model: {result['rendered']} "
        f"({100 * (result['ratio'] or 0):.2f}%)"
    )
    print("[coverage] statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(result["statuses"].items())))
    print("[coverage] by exporter result: " + ", ".join(f"{k}={v}" for k, v in result["exporter"].items()))
    print("[coverage] worst mods (rendered/placed):")
    for w in result["worst"][:15]:
        print(f"  {w['mod']:<28} {w['rendered']:>5}/{w['placed']:<5} {100 * w['ratio']:6.2f}%")
