"""Low-level HTML/CSS primitives for the Orbit digest (Phase 3 / Stage 7a).

This is the HTML layer under :mod:`lib.render`: a self-contained page template
(sentinel-replacement, lifted in shape from last30days/html_render.py), the
XSS-safe link allowlist, ``html.escape`` wrappers, and small element builders
(cards, chapter lists, the index strip, the TL;DR header). It owns ALL markup and
styling; :mod:`lib.render` owns orchestration (which items, which tier, what order).

The page is fully self-contained per the design brief: an inline ``<style>``, NO
external/CDN fetches, NO ``<link>`` / ``<script src>``. Opened straight off disk it
renders identically offline.

Security (design brief §5, non-negotiable):
  * Every URL that becomes an ``<a href>`` passes :func:`is_safe_link_url` (scheme
    allowlist ``http`` / ``https`` / ``mailto``, plus relative URLs). A
    ``javascript:`` / ``data:`` URL is dropped to ``"#"`` — never clickable.
  * Every user-controlled string (title, channel name, chapter title) is
    ``html.escape``-d via :func:`escape` so a ``<script>`` in a creator title
    renders as inert text, never markup.
  * Chapter/card deep-links are trusted constructed ``watch?v=ID&t=Ns`` URLs and
    therefore pass the allowlist and survive escaping intact.

Rule 5: no LLM here — this is pure deterministic string building.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.html_render`` (via orbit.py's sys.path insert of the scripts dir) or run
# from the scripts dir directly. Mirrors rerank.py / density.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

# --- Link-safety allowlist (lifted/adapted from last30days/html_render.py) --
# The artifact is opened in a browser, so a permissive link parser is a stored-XSS
# vector: a `javascript:` / `data:text/html,...` URL surviving into an `href` would
# render as a clickable script payload. Restrict hrefs to a small scheme allowlist
# (plus relative URLs). Anything else is dropped to a non-clickable "#".
_SAFE_LINK_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

# href emitted when a candidate URL fails the allowlist — a no-op anchor that is
# never a script payload.
_UNSAFE_HREF_PLACEHOLDER: str = "#"

# --- Image-src safety allowlist (the <img src> sink, Phase 7) ----------------
# The Tiles digest inlines images as base64 ``data:`` URIs (self-contained, no CDN at
# open). The ``<img src>`` sink needs its OWN allowlist, distinct from the href one:
# `data:` is REJECTED for hrefs (XSS) but REQUIRED here — yet ONLY for real image
# payloads. A `data:text/html,<script>` src is a stored-XSS vector, so we allow only
# `data:image/<fmt>;base64,...` for a known raster format, plus plain http(s) URLs.
_SAFE_IMG_DATA_URI_PATTERN: re.Pattern[str] = re.compile(
    r"^data:image/(?:png|jpe?g|webp|gif|avif);base64,[A-Za-z0-9+/=\s]+$",
    re.IGNORECASE,
)

# Schemes permitted for a remote (non-data) image src.
_SAFE_IMG_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# src emitted when a candidate fails the allowlist — empty so the renderer drops the
# <img> entirely (falls back to the .ph placeholder), never emits a broken/unsafe src.
_UNSAFE_IMG_SRC_PLACEHOLDER: str = ""


def escape(text: Any) -> str:
    """HTML-escape an arbitrary value for safe embedding as text OR an attribute.

    Coerces ``text`` to ``str`` then ``html.escape(..., quote=True)`` so ``<``,
    ``>``, ``&``, ``"`` and ``'`` all become entities. Use this on EVERY
    user-controlled string before it enters the markup — a ``<script>`` in a
    creator title must render as inert text, never an executable tag.

    Args:
        text: Any value (commonly a title / channel name / chapter label).

    Returns:
        The escaped string, safe in element text and double-quoted attributes.

    Example:
        >>> escape("<script>alert(1)</script>")
        '&lt;script&gt;alert(1)&lt;/script&gt;'
    """
    return html.escape(str(text), quote=True)


def is_safe_link_url(url: str) -> bool:
    """Return True if ``url`` is safe to render as an ``<a href>``.

    A URL is safe when it either has no scheme (relative URL / fragment / path) or
    uses a scheme in :data:`_SAFE_LINK_SCHEMES` (``http`` / ``https`` / ``mailto``).
    The scheme check is case-insensitive and rejects any control character (e.g. a
    smuggled ``java\\rscript:``). Lifted/adapted from last30days/html_render.py's
    ``_is_safe_link_url``.

    Args:
        url: The candidate URL (raw — this function does the scheme parse itself;
            :func:`safe_href` is the caller that also escapes for the attribute).

    Returns:
        True if the URL may be used as an ``href``; False otherwise.

    Example:
        >>> is_safe_link_url("https://www.youtube.com/watch?v=abc&t=90s")
        True
        >>> is_safe_link_url("javascript:alert(1)")
        False
        >>> is_safe_link_url("/relative/path")
        True
    """
    stripped = url.strip()
    if not stripped:
        return False
    # Reject control characters — a bare CR/LF/NUL inside a scheme name is stripped
    # by the browser's URL parser and could smuggle `java\rscript:` past a check.
    if any(ord(character) < 0x20 for character in stripped):
        return False
    colon_index = stripped.find(":")
    if colon_index == -1:
        # No scheme — relative URL or fragment. Safe.
        return True
    slash_index = stripped.find("/")
    question_index = stripped.find("?")
    hash_index = stripped.find("#")
    # The first `:` that comes after a `/`, `?`, or `#` is path/query/fragment, not a
    # scheme separator — e.g. `/path:with:colons` is a relative URL.
    earlier_delimiters = [pos for pos in (slash_index, question_index, hash_index) if 0 <= pos < colon_index]
    if earlier_delimiters:
        return True
    scheme = stripped[:colon_index].lower()
    return scheme in _SAFE_LINK_SCHEMES


def safe_href(url: str) -> str:
    """Return an escaped, allowlist-checked ``href`` value — or ``"#"`` if unsafe.

    Validates ``url`` against :func:`is_safe_link_url` FIRST (on the raw string, so
    a literal ``:`` is detected), then ``html.escape``-s the result for safe
    embedding in a double-quoted attribute. An unsafe URL (e.g. ``javascript:...``)
    becomes :data:`_UNSAFE_HREF_PLACEHOLDER` (``"#"``) so it is never a clickable
    script payload.

    Args:
        url: The candidate URL.

    Returns:
        An escaped href string safe to drop into ``href="..."`` — the original URL
        if allowlisted, else ``"#"``.

    Example:
        >>> safe_href("https://www.youtube.com/watch?v=abc&t=90s")
        'https://www.youtube.com/watch?v=abc&amp;t=90s'
        >>> safe_href("javascript:alert(1)")
        '#'
    """
    if is_safe_link_url(url):
        return html.escape(url, quote=True)
    return _UNSAFE_HREF_PLACEHOLDER


def safe_img_src(src: str) -> str:
    """Return an escaped, allowlist-checked ``<img src>`` value — or ``""`` if unsafe.

    The image-sink analog of :func:`safe_href`. Allows exactly two safe shapes and
    rejects everything else to ``""`` (so the renderer drops the ``<img>`` and falls
    back to the hatched ``.ph`` placeholder rather than emitting an unsafe src):

      * a base64 image ``data:`` URI — ``data:image/(png|jpe?g|webp|gif|avif);base64,...``
        (the build-time inlined thumbnails/avatars from :mod:`lib.images`);
      * a plain ``http`` / ``https`` URL.

    Rejected: ``data:text/html,...`` (stored-XSS via an HTML payload), ``javascript:``,
    any other scheme, and any string carrying a control character (a smuggled
    ``data:image/png\\x00;...``). The check runs on the RAW string first, then the
    result is ``html.escape``-d for safe embedding in a double-quoted attribute.

    Args:
        src: The candidate image source (a ``data:`` URI from :func:`lib.images.fetch_and_inline`
            or a remote http(s) URL).

    Returns:
        An escaped src string safe to drop into ``src="..."`` — the original value if
        allowlisted, else ``""``.

    Example:
        >>> safe_img_src("data:image/png;base64,iVBORw0KGgo=")
        'data:image/png;base64,iVBORw0KGgo='
        >>> safe_img_src("data:text/html,<script>alert(1)</script>")
        ''
        >>> safe_img_src("javascript:alert(1)")
        ''
        >>> safe_img_src("https://i.ytimg.com/vi/abc/mqdefault.jpg")
        'https://i.ytimg.com/vi/abc/mqdefault.jpg'
    """
    stripped = src.strip()
    if not stripped:
        return _UNSAFE_IMG_SRC_PLACEHOLDER
    # Reject control characters up front — a NUL/CR/LF inside the value can smuggle a
    # payload past the scheme/format check the same way it can for hrefs.
    if any(ord(character) < 0x20 for character in stripped):
        return _UNSAFE_IMG_SRC_PLACEHOLDER
    # Allowed shape 1: a base64 raster image data URI.
    if _SAFE_IMG_DATA_URI_PATTERN.match(stripped):
        return html.escape(stripped, quote=True)
    # Allowed shape 2: a plain http(s) remote URL.
    colon_index = stripped.find(":")
    if colon_index != -1:
        scheme = stripped[:colon_index].lower()
        if scheme in _SAFE_IMG_SCHEMES:
            return html.escape(stripped, quote=True)
    return _UNSAFE_IMG_SRC_PLACEHOLDER


def _format_timestamp(start_seconds: float) -> str:
    """Format a second offset as ``M:SS`` (or ``H:MM:SS``) for a chapter label.

    Args:
        start_seconds: The chapter start offset in seconds.

    Returns:
        A human-readable timestamp like ``"1:30"`` or ``"1:02:05"``.

    Example:
        >>> _format_timestamp(90.0)
        '1:30'
        >>> _format_timestamp(3725.0)
        '1:02:05'
    """
    total_seconds = max(0, int(start_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def render_link(url: str, label: Any, *, css_class: str = "") -> str:
    """Build one safe ``<a>`` — escaped label, allowlist-checked href.

    Args:
        url: The link target (validated + escaped via :func:`safe_href`).
        label: The visible link text (escaped via :func:`escape`).
        css_class: Optional CSS class for the anchor.

    Returns:
        The ``<a ...>label</a>`` markup string.
    """
    class_attribute = f' class="{escape(css_class)}"' if css_class else ""
    return f'<a{class_attribute} href="{safe_href(url)}">{escape(label)}</a>'


def render_chapter_list(chapters: Iterable[Any]) -> str:
    """Render a chapter list: one deep-link ``<li>`` per chapter (the headline feature).

    Each chapter ``<li>`` is ``<a href="{safe deep_link}">{M:SS} {escaped title}</a>``.
    The ``deep_link`` is a trusted ``watch?v=ID&t=Ns`` URL (built by
    ``lib.transcribe.build_deep_link``), so it passes the allowlist and survives
    escaping intact. Returns an empty string for an empty chapter list so the card
    renders no empty container (design brief §3).

    Args:
        chapters: An iterable of :class:`lib.chapterize.Chapter` (``.title``,
            ``.start_seconds``, ``.deep_link``).

    Returns:
        A ``<ul class="chapters">...</ul>`` string, or ``""`` if there are no chapters.
    """
    chapter_items = [
        f'<li><a class="chapter-link" href="{safe_href(getattr(chapter, "deep_link", ""))}">'
        f'<span class="chapter-time">{escape(_format_timestamp(getattr(chapter, "start_seconds", 0.0)))}</span> '
        f'<span class="chapter-title">{escape(getattr(chapter, "title", ""))}</span></a></li>'
        for chapter in chapters
    ]
    if not chapter_items:
        return ""
    return '<ul class="chapters">' + "".join(chapter_items) + "</ul>"


def render_meta_line(item: Any) -> str:
    """Render a one-line engagement/meta string for a card (channel · views).

    Pure formatting — counts that are None are simply omitted. Channel name is
    escaped (it is user-controlled).

    Args:
        item: A :class:`lib.rerank.RankableItem` (``.channel_name``, ``.view_count``).

    Returns:
        A ``<div class="card-meta">...</div>`` string.
    """
    parts: list[str] = []
    channel_name = getattr(item, "channel_name", "") or ""
    if channel_name:
        parts.append(f'<span class="channel">{escape(channel_name)}</span>')
    view_count = getattr(item, "view_count", None)
    if isinstance(view_count, (int, float)) and view_count > 0:
        parts.append(f'<span class="views">{_format_count(int(view_count))} views</span>')
    return '<div class="card-meta">' + " · ".join(parts) + "</div>"


def _format_count(count: int) -> str:
    """Abbreviate a count for display (1234 -> ``1.2k``, 1_500_000 -> ``1.5M``).

    Args:
        count: A non-negative integer count.

    Returns:
        A short human-readable count string.

    Example:
        >>> _format_count(1234)
        '1.2k'
        >>> _format_count(1500000)
        '1.5M'
        >>> _format_count(42)
        '42'
    """
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def render_card(item: Any, card_url: str, tier_class: str, *, with_chapters: bool) -> str:
    """Render a full creator episode card (Hero / Standard tiers).

    The card title links to ``card_url`` (the whole-video ``watch?v=ID&t=0s``
    deep-link). When ``with_chapters`` is True and the item carries chapters, the
    full deep-link chapter list is appended. Title is escaped; href is allowlisted.

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        card_url: The whole-video deep-link for the card title.
        tier_class: The CSS tier class (``"hero"`` / ``"standard"``).
        with_chapters: Whether to render the chapter list (Hero/Standard: True).

    Returns:
        A ``<article class="card {tier_class}">...</article>`` string.
    """
    title = getattr(item, "title", "") or ""
    title_link = f'<h2 class="card-title">{render_link(card_url, title)}</h2>'
    meta_line = render_meta_line(item)
    chapter_list = render_chapter_list(getattr(item, "chapters", []) or []) if with_chapters else ""
    return f'<article class="card {escape(tier_class)}">{title_link}{meta_line}{chapter_list}</article>'


def render_compact_row(item: Any, card_url: str) -> str:
    """Render a condensed single-line Compact-tier row (no chapter list).

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        card_url: The whole-video deep-link for the linked title.

    Returns:
        A ``<div class="card compact">...</div>`` row string.
    """
    title = getattr(item, "title", "") or ""
    channel_name = getattr(item, "channel_name", "") or ""
    channel_html = f' <span class="channel">· {escape(channel_name)}</span>' if channel_name else ""
    return (
        '<div class="card compact">'
        f'<span class="compact-title">{render_link(card_url, title)}</span>{channel_html}'
        "</div>"
    )


def render_index_line(item: Any, card_url: str) -> str:
    """Render one Index-tier "they also posted" line (linked title only).

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        card_url: The whole-video deep-link for the linked title.

    Returns:
        A ``<li class="index-line">...</li>`` string.
    """
    title = getattr(item, "title", "") or ""
    channel_name = getattr(item, "channel_name", "") or ""
    channel_html = f' <span class="channel">· {escape(channel_name)}</span>' if channel_name else ""
    return f'<li class="index-line">{render_link(card_url, title)}{channel_html}</li>'


def render_index_strip(line_items_html: list[str]) -> str:
    """Wrap the rendered Index lines in the bottom "they also posted" section.

    Returns an empty string when there are no index items so the section is absent
    (no empty container) per the design brief.

    Args:
        line_items_html: The pre-rendered ``<li>`` strings from :func:`render_index_line`.

    Returns:
        A ``<section class="index-strip">...</section>`` string, or ``""`` if empty.
    """
    if not line_items_html:
        return ""
    heading = '<h2 class="section-heading">They also posted</h2>'
    return f'<section class="index-strip">{heading}<ul class="index-list">' + "".join(line_items_html) + "</ul></section>"


def render_tldr(episode_count: int, creator_count: int) -> str:
    """Render the one-line TL;DR header (pure counting — Rule 5, no LLM).

    Args:
        episode_count: How many items are in the digest.
        creator_count: How many distinct creators those items span.

    Returns:
        A ``<header class="tldr">...</header>`` string.

    Example:
        >>> "7 episodes from 4 creators" in render_tldr(7, 4)
        True
    """
    episode_word = "episode" if episode_count == 1 else "episodes"
    creator_word = "creator" if creator_count == 1 else "creators"
    summary = f"{episode_count} {episode_word} from {creator_count} {creator_word} today"
    return f'<header class="tldr"><span class="tldr-label">TL;DR</span> {escape(summary)}</header>'


# --- Self-contained page template (sentinel-replacement, lifted in shape) ----
# __TITLE__ / __CSS__ / __BODY__ are swapped by wrap_page via str.replace. No
# external fetches: the <style> is inline (base64 fonts + the Tiles classes), there
# is no <link> or <script src>. The Tiles body carries its OWN outer wrapper divs
# (matching out/orbit-tiles-reference.html), so there is no forced <main> wrapper.
HTML_TEMPLATE: str = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
__BODY__
</body>
</html>
"""

# --- Tiles stylesheet (Phase 7) ----------------------------------------------
# The newspaper "Tiles" design (scripts/assets/orbit-tiles.dc.html) is mostly
# inline-styled per element; only a handful of shared classes live here. This block
# is the CLASS layer (.ph hatched placeholder / .tile card / .chip timestamp / .kp
# key-point row / .col packed masonry column) ported from the design's <style>. The
# .col rules are Orbit's own: lib.tiles packs the columns in Python and the
# last-child stretch makes every column end flush, so the grid has no dead space
# (the design's CSS `column-count` left ragged bottoms). The base64 @font-face
# rules are NOT here — wrap_page prepends them from the prebuilt
# scripts/assets/fonts/fonts-inline.css so the page stays self-contained (no CDN).
CSS: str = """  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:#EDE7DA}
  .ph{background-image:repeating-linear-gradient(135deg,rgba(31,27,22,.07) 0 7px,rgba(31,27,22,.02) 7px 14px);background-color:#e3dccc;display:flex;align-items:flex-end;justify-content:flex-start;overflow:hidden;position:relative;flex:none}
  .ph span{font-family:'JetBrains Mono',monospace;font-size:8.5px;letter-spacing:.04em;color:#8a7f6c;text-transform:uppercase;padding:4px 5px;background:rgba(244,240,232,.82)}
  a{color:inherit;text-decoration:none}
  .tile{break-inside:avoid;background:#F7F3EA;border:1px solid rgba(31,27,22,.14);margin-bottom:18px;transition:border-color .15s,box-shadow .15s}
  .tile:hover{border-color:rgba(183,71,42,.55);box-shadow:0 2px 10px rgba(31,27,22,.07)}
  .col{flex:1 1 0;min-width:0;display:flex;flex-direction:column}
  .col>.tile{flex:1 1 auto}
  .col>.tile:last-child{margin-bottom:0}
  .chip{font-family:'JetBrains Mono',monospace;font-size:10.5px;font-weight:500;color:#B7472A;background:rgba(183,71,42,.1);padding:2px 6px;border-radius:2px;white-space:nowrap}
  .kp{display:flex;gap:9px;align-items:baseline;padding:5px 0;border-bottom:1px dotted rgba(31,27,22,.16)}
  .kp:last-child{border-bottom:none}
  .kp span.t{font-size:13.5px;color:#2a251e;line-height:1.3;flex:1}
"""

# --- Inlined font CSS (read once, cached) ------------------------------------
# wrap_page prepends these base64 woff2 @font-face rules so the digest renders the
# Fraunces / Newsreader / JetBrains Mono families fully offline. The file is built by
# ``python scripts/build_fonts.py`` (Sub-phase 0). We FAIL LOUD (Rule 12) if it is
# absent rather than silently dropping fonts to system defaults.
_FONTS_INLINE_CSS_PATH: Path = _SCRIPTS_DIR / "assets" / "fonts" / "fonts-inline.css"
_inlined_font_css_cache: str | None = None


def _load_inlined_font_css() -> str:
    """Return the base64 ``@font-face`` CSS block, read once and cached.

    Reads :data:`_FONTS_INLINE_CSS_PATH` on first call and memoizes it for the
    process (a single disk read regardless of how many pages are rendered).

    Returns:
        The full ``@font-face`` CSS string (latin-subset, base64 woff2).

    Raises:
        FileNotFoundError: If the prebuilt font CSS is missing — the message tells
            the user to run ``python scripts/build_fonts.py`` first. We never fall
            back silently to system fonts (Rule 12: fail loud, no silent degrade).
    """
    global _inlined_font_css_cache
    if _inlined_font_css_cache is None:
        if not _FONTS_INLINE_CSS_PATH.is_file():
            raise FileNotFoundError(
                f"Inlined font CSS not found at {_FONTS_INLINE_CSS_PATH}. "
                "Run `python scripts/build_fonts.py` first to vendor the base64 woff2 fonts."
            )
        _inlined_font_css_cache = _FONTS_INLINE_CSS_PATH.read_text(encoding="utf-8")
    return _inlined_font_css_cache


def wrap_page(title: str, body_html: str) -> str:
    """Swap the page-template sentinels and return the full self-contained HTML.

    Replaces ``__TITLE__`` (escaped), ``__CSS__`` (the inlined base64 ``@font-face``
    block from :func:`_load_inlined_font_css` followed by the Tiles :data:`CSS`
    classes), and ``__BODY__`` (the caller's already-built, already-escaped body
    markup). The body is inserted verbatim — :mod:`lib.render` is responsible for
    having escaped every user-controlled string inside it via the builders here.

    The base64 font alphabet (``[A-Za-z0-9+/=]``) contains no underscores, so the
    font CSS can never re-introduce a ``__TITLE__`` / ``__BODY__`` sentinel.

    Args:
        title: The page ``<title>`` text (escaped before insertion).
        body_html: The fully built body markup (carries its own outer wrappers).

    Returns:
        The complete self-contained ``<!DOCTYPE html>...`` page string, fonts inlined.

    Raises:
        FileNotFoundError: If the prebuilt font CSS is absent (see
            :func:`_load_inlined_font_css`) — fail loud, never drop fonts silently.

    Example:
        >>> page = wrap_page("Orbit", '<div class="tile">hi</div>')
        >>> page.startswith("<!DOCTYPE html>")
        True
        >>> "@font-face" in page and "</html>" in page
        True
    """
    combined_css = f"{_load_inlined_font_css()}\n{CSS}"
    return (
        HTML_TEMPLATE.replace("__TITLE__", escape(title))
        .replace("__CSS__", combined_css)
        .replace("__BODY__", body_html)
    )


# --- Tiles builder re-exports (Phase 7, Sub-phase 3) -------------------------
# The Tiles markup builders live in :mod:`lib.tiles` (file-size discipline keeps
# this module under the 1000-line cap). They are re-exported here via PEP 562
# module ``__getattr__`` so callers can keep using ``html_render.render_*`` paths.
# The import is LAZY (resolved on first attribute access, after both modules have
# finished importing), which avoids the import-time circular reference between this
# module and ``lib.tiles`` (tiles imports ``escape`` / ``safe_href`` / ``safe_img_src``
# from here at its top level).
_TILES_REEXPORTS: frozenset[str] = frozenset(
    {
        "render_masthead",
        "render_feed_masonry",
        "render_hero_tile",
        "render_standard_tile",
        "render_compact_tile",
        "render_tweet_tile",
        "render_footer",
        "ChapterRow",
        "CrossLink",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily resolve Tiles builder names from :mod:`lib.tiles` (PEP 562).

    Args:
        name: The attribute being accessed on this module.

    Returns:
        The corresponding object from :mod:`lib.tiles` when ``name`` is a known
        Tiles re-export.

    Raises:
        AttributeError: For any other name (standard module-attribute behaviour).
    """
    if name in _TILES_REEXPORTS:
        from lib import tiles

        return getattr(tiles, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
