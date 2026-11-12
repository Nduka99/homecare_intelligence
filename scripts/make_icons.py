"""Render the app icon (static/icon.svg) to the PNG sizes the web app manifest names.

    uv run python scripts/make_icons.py

Run it after changing the icon; the PNGs are committed, because the serving image has no
browser. It uses the Playwright Chromium already on the machine.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "src" / "homecare" / "app" / "static"
SIZES = [192, 512]


def main() -> None:
    svg = (STATIC / "icon.svg").read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for size in SIZES:
            page = browser.new_page(viewport={"width": size, "height": size})
            sized = svg.replace("<svg ", f"<svg width={size} height={size} ", 1)
            page.set_content(f'<body style="margin:0">{sized}</body>')
            page.screenshot(path=STATIC / f"icon-{size}.png")
            page.close()
        browser.close()
    print(f"wrote icon-{SIZES[0]}.png and icon-{SIZES[1]}.png")


if __name__ == "__main__":
    main()
