"""Headless Chromium screenshots of a running BlueMap webapp.

Run through `uv run --with playwright python -m gtnh_client.screens ...` (Playwright is not a project dependency;
`gtnh-client screens` does this for you). For each view it opens `<url>#<map>:<x>:<y>:<z>:<distance>:<rotation>:
<angle>:<tilt>:<ortho>:<mode>`, waits until no tiles are loading and saves a page screenshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--views", required=True, help="JSON list of {name, hash}")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    views = json.loads(args.views)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        for view in views:
            page.goto(f"{args.url}#{view['hash']}", wait_until="load")
            page.wait_for_function("() => window.bluemap && window.bluemap.mapViewer && window.bluemap.mapViewer.map", timeout=args.timeout * 1000)
            # hash-only navigations keep the page: reset the idle counter and let the camera move first
            page.evaluate("() => { window.__bmIdle = 0; }")
            page.wait_for_timeout(1500)
            # tiles queue up after the camera settles; wait until nothing is loading for a while
            page.wait_for_function(
                """() => {
                    const m = window.bluemap.mapViewer.map;
                    const low = Array.isArray(m.lowresTileManager) ? m.lowresTileManager.reduce((a, t) => a + t.currentlyLoading, 0) : 0;
                    const loading = m.hiresTileManager.currentlyLoading + low;
                    window.__bmIdle = loading === 0 ? (window.__bmIdle || 0) + 1 : 0;
                    return window.__bmIdle > 20;
                }""",
                polling=100,
                timeout=args.timeout * 1000,
            )
            file = args.out / f"{view['name']}.png"
            page.screenshot(path=str(file))
            print(f"[screens] {file}", flush=True)
        browser.close()
    (args.out / "console-errors.json").write_text(json.dumps(errors, indent=1))
    if errors:
        print(f"[screens] {len(errors)} console errors, see {args.out / 'console-errors.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
