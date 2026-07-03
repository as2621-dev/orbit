"""One-time build step: vendor Google Fonts woff2 into a self-contained CSS.

Downloads every ``woff2`` referenced by ``assets/fonts/google.css`` (the
``@font-face`` sheet fetched from fonts.googleapis.com with a desktop UA) and
rewrites each remote ``url(https://fonts.gstatic.com/...woff2)`` into an inline
``url(data:font/woff2;base64,...)`` so the digest renders the exact newspaper
typography fully offline — no CDN fetch, honoring design brief §5.

The unicode-range ``@font-face`` blocks are preserved verbatim; only the URL
inside each ``src: url(...)`` is swapped, so the browser still picks the right
subset per glyph. Run once; the renderer reads the emitted ``fonts-inline.css``
from disk (it never fetches at digest time).

Usage:
    python scripts/build_fonts.py

Rule 5: deterministic transform, no LLM. Rule 12: fails loud on a download error.
"""

from __future__ import annotations

import base64
import re
import sys
import urllib.request
from pathlib import Path

_ASSETS_DIR = Path(__file__).parent / "assets" / "fonts"
_SOURCE_CSS = _ASSETS_DIR / "google.css"
_OUTPUT_CSS = _ASSETS_DIR / "fonts-inline.css"

# A desktop Chrome UA so gstatic serves woff2 (not ttf) for the bare urllib fetch.
_DESKTOP_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_WOFF2_URL_PATTERN = re.compile(r"https://fonts\.gstatic\.com/[^\s)]+\.woff2")
_FONT_FACE_BLOCK_PATTERN = re.compile(r"/\*[^*]*\*/\s*@font-face\s*\{[^}]*\}", re.DOTALL)
# The Latin subset's unicode-range always begins with U+0000-00FF and includes the
# curly quotes (U+2018-2019/201C-201D) the design uses — that is all an English
# digest needs, so we drop the Cyrillic/Greek/Vietnamese/latin-ext blocks (which
# otherwise bloat the inline CSS ~10x).
_LATIN_RANGE_MARKER: str = "U+0000-00FF"


def _keep_latin_only(source_css: str) -> str:
    """Keep ONE Latin ``@font-face`` per family, weight-ranged for the variable font.

    Google emits one ``@font-face`` per requested weight, but all weights of a family
    share a single *variable* woff2 — so we dedupe by woff2 URL (one block per family)
    and rewrite ``font-weight: N`` to the full ``100 900`` range so the browser drives
    the weight axis off the one variable file instead of faux-bolding a single weight.

    Args:
        source_css: The raw Google Fonts CSS (comment-tagged blocks per subset).

    Returns:
        CSS with one weight-ranged Latin block per family.
    """
    kept_blocks: list[str] = []
    seen_woff2_urls: set[str] = set()
    for block in _FONT_FACE_BLOCK_PATTERN.findall(source_css):
        if _LATIN_RANGE_MARKER not in block:
            continue
        urls_in_block = _WOFF2_URL_PATTERN.findall(block)
        if not urls_in_block or urls_in_block[0] in seen_woff2_urls:
            continue
        seen_woff2_urls.add(urls_in_block[0])
        kept_blocks.append(re.sub(r"font-weight:\s*[^;]+;", "font-weight: 100 900;", block))
    return "\n".join(kept_blocks) + "\n"


def _download_woff2(url: str) -> bytes:
    """Download one woff2 file, raising loud on any failure (Rule 12)."""
    request = urllib.request.Request(url, headers={"User-Agent": _DESKTOP_USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (trusted gstatic host)
        return response.read()


def build_inline_font_css() -> int:
    """Rewrite ``google.css`` into ``fonts-inline.css`` with base64 woff2.

    Returns:
        The byte size of the emitted self-contained CSS.
    """
    source_css = _keep_latin_only(_SOURCE_CSS.read_text(encoding="utf-8"))
    unique_urls = sorted(set(_WOFF2_URL_PATTERN.findall(source_css)))
    url_to_data_uri: dict[str, str] = {}
    for woff2_url in unique_urls:
        woff2_bytes = _download_woff2(woff2_url)
        encoded = base64.b64encode(woff2_bytes).decode("ascii")
        url_to_data_uri[woff2_url] = f"data:font/woff2;base64,{encoded}"
        print(f"inlined {len(woff2_bytes):>7} bytes  {woff2_url.split('/')[-1]}", file=sys.stderr)

    inline_css = _WOFF2_URL_PATTERN.sub(lambda match: url_to_data_uri[match.group(0)], source_css)
    _OUTPUT_CSS.write_text(inline_css, encoding="utf-8")
    return len(inline_css.encode("utf-8"))


if __name__ == "__main__":
    output_bytes = build_inline_font_css()
    print(f"wrote {_OUTPUT_CSS} ({output_bytes} bytes)", file=sys.stderr)
