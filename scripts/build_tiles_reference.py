"""Build a self-contained standalone HTML from the claude.ai design canvas file.

Takes ``assets/orbit-tiles.dc.html`` (exported from the claude.ai Design canvas,
wrapped in the canvas-only ``<x-dc>`` / ``<helmet>`` tags and a ``support.js``
runtime + a Google-Fonts ``<link>``) and emits a real, self-contained page to
``out/orbit-tiles-reference.html``:

  * drops ``<script src="./support.js">`` (canvas runtime, not needed standalone),
  * drops the remote Google-Fonts ``<link>`` / ``preconnect`` lines and inlines
    ``assets/fonts/fonts-inline.css`` (base64 woff2 — fully offline) instead,
  * unwraps ``<x-dc>`` / ``<helmet>`` into a normal ``<head>`` + ``<body>``.

This is the visual reference target for the data-wired renderer (lib.render) and
proves the inlined-font pipeline renders the exact typography off disk.

Rule 5: deterministic string surgery, no LLM. Run after ``build_fonts.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_ASSETS_DIR = Path(__file__).parent / "assets"
_SOURCE_HTML = _ASSETS_DIR / "orbit-tiles.dc.html"
_FONTS_CSS = _ASSETS_DIR / "fonts" / "fonts-inline.css"
_OUTPUT_HTML = Path(__file__).parent.parent / "out" / "orbit-tiles-reference.html"

_PAGE_TITLE: str = "Orbit · Today"


def build_reference_page() -> int:
    """Assemble the standalone self-contained reference page.

    Returns:
        The byte size of the written page.
    """
    raw = _SOURCE_HTML.read_text(encoding="utf-8")
    fonts_css = _FONTS_CSS.read_text(encoding="utf-8")

    # The design's own component CSS lives in the single <style> block inside <helmet>.
    style_match = re.search(r"<style>(.*?)</style>", raw, re.DOTALL)
    design_css = style_match.group(1) if style_match else ""

    # The page body is the screen <div data-screen-label=...> ... </div>, i.e. everything
    # between </helmet> and </x-dc>.
    body_match = re.search(r"</helmet>(.*?)</x-dc>", raw, re.DOTALL)
    body_html = body_match.group(1).strip() if body_match else ""

    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_PAGE_TITLE}</title>\n"
        f"<style>\n{fonts_css}\n{design_css}\n</style>\n"
        "</head>\n<body>\n"
        f"{body_html}\n"
        "</body>\n</html>\n"
    )
    _OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_HTML.write_text(page, encoding="utf-8")
    return len(page.encode("utf-8"))


if __name__ == "__main__":
    written = build_reference_page()
    print(f"wrote {_OUTPUT_HTML} ({written} bytes)")
