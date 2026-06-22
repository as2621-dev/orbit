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


def render_summary_bullets(bullets: Iterable[Any]) -> str:
    """Render a winner's summary as a bullet list (the digest's headline content).

    Each bullet is a :class:`lib.summarize.SummaryBullet` (duck-typed: ``.text``,
    ``.start_seconds``, ``.deep_link``). A VIDEO bullet carries a ``deep_link`` and is
    rendered as a timestamped deep-link ``<li>`` (jump straight to the moment); a TWEET
    bullet has no ``deep_link`` and renders as a plain text ``<li>``. The deep-link is a
    trusted ``watch?v=ID&t=Ns`` URL, so it passes the allowlist and survives escaping.
    Returns ``""`` for an empty list (no empty container).

    Args:
        bullets: An iterable of summary bullets.

    Returns:
        A ``<ul class="summary">...</ul>`` string, or ``""`` if there are no bullets.
    """
    list_items: list[str] = []
    for bullet in bullets:
        text = escape(getattr(bullet, "text", "") or "")
        deep_link = getattr(bullet, "deep_link", None)
        if deep_link:
            timestamp = escape(_format_timestamp(getattr(bullet, "start_seconds", 0.0) or 0.0))
            list_items.append(
                f'<li><a class="summary-link" href="{safe_href(deep_link)}">'
                f'<span class="summary-time">{timestamp}</span> '
                f'<span class="summary-text">{text}</span></a></li>'
            )
        else:
            list_items.append(f'<li class="summary-point">{text}</li>')
    if not list_items:
        return ""
    return '<ul class="summary">' + "".join(list_items) + "</ul>"


def render_footnote_links(footnote_links: Iterable[tuple[str, str]]) -> str:
    """Render the "Also covered" footnote links folded under a cluster winner's card.

    The non-winner members of a topic cluster are surfaced here as links (so duplicate
    coverage of one story collapses to a single summarized card plus these footnotes).
    Each link's URL is allowlist-checked + escaped via :func:`render_link`. Returns
    ``""`` for an empty list (no empty container).

    Args:
        footnote_links: An iterable of ``(url, label)`` pairs (built by the renderer
            from each non-winner item's deep-link + title).

    Returns:
        A ``<div class="card-footnotes">...</div>`` string, or ``""`` if empty.
    """
    list_items = [
        f'<li>{render_link(url, label, css_class="footnote-link")}</li>' for url, label in footnote_links
    ]
    if not list_items:
        return ""
    return (
        '<div class="card-footnotes"><span class="footnote-label">Also covered</span>'
        '<ul class="footnote-list">' + "".join(list_items) + "</ul></div>"
    )


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


def render_card(
    item: Any,
    card_url: str,
    tier_class: str,
    *,
    with_chapters: bool,
    summary_bullets: Iterable[Any] | None = None,
    footnote_links: Iterable[tuple[str, str]] | None = None,
) -> str:
    """Render a full creator episode card (Hero / Standard tiers).

    The card title links to ``card_url`` (the whole-video ``watch?v=ID&t=0s``
    deep-link). Its body content is, in precedence order: the SUMMARY bullet list when
    ``summary_bullets`` is given (a cluster winner — the headline feature, timestamped
    for videos), ELSE the deep-link chapter list when ``with_chapters`` and the item
    carries chapters (legacy / non-summarized path). The "Also covered" footnote links
    (the folded non-winner cluster members) are appended when ``footnote_links`` is
    given. Title is escaped; every href is allowlisted. With neither ``summary_bullets``
    nor ``footnote_links`` (the existing callers / M1 path), output is unchanged.

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        card_url: The whole-video deep-link for the card title.
        tier_class: The CSS tier class (``"hero"`` / ``"standard"``).
        with_chapters: Whether to render the chapter list (Hero/Standard: True) when
            there is no summary.
        summary_bullets: OPTIONAL the winner's summary bullets; when present they
            REPLACE the chapter list as the card's body.
        footnote_links: OPTIONAL ``(url, label)`` pairs for the "Also covered" footnotes.

    Returns:
        A ``<article class="card {tier_class}">...</article>`` string.
    """
    title = getattr(item, "title", "") or ""
    title_link = f'<h2 class="card-title">{render_link(card_url, title)}</h2>'
    meta_line = render_meta_line(item)
    summary_html = render_summary_bullets(summary_bullets) if summary_bullets else ""
    if summary_html:
        body = summary_html
    else:
        body = render_chapter_list(getattr(item, "chapters", []) or []) if with_chapters else ""
    footnotes_html = render_footnote_links(footnote_links or [])
    return f'<article class="card {escape(tier_class)}">{title_link}{meta_line}{body}{footnotes_html}</article>'


def render_compact_row(item: Any, card_url: str, *, footnote_links: Iterable[tuple[str, str]] | None = None) -> str:
    """Render a condensed single-line Compact-tier row (no chapter list).

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        card_url: The whole-video deep-link for the linked title.
        footnote_links: OPTIONAL "Also covered" links (a compact-tier cluster winner
            still folds its non-winner members in rather than dropping them).

    Returns:
        A ``<div class="card compact">...</div>`` row string.
    """
    title = getattr(item, "title", "") or ""
    channel_name = getattr(item, "channel_name", "") or ""
    channel_html = f' <span class="channel">· {escape(channel_name)}</span>' if channel_name else ""
    footnotes_html = render_footnote_links(footnote_links or [])
    return (
        '<div class="card compact">'
        f'<span class="compact-title">{render_link(card_url, title)}</span>{channel_html}'
        f"{footnotes_html}"
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
# external fetches: the <style> is inline, there is no <link> or <script src>.
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
<main class="digest">
__BODY__
</main>
</body>
</html>
"""

# Self-contained stylesheet. Dark-friendly neutral palette with a light fallback
# via prefers-color-scheme. System font stack (no web-font fetch). Tier classes
# .hero / .standard / .compact / .index carry the density distinction.
CSS: str = """
:root {
  /* Palette lifted from the "Aura Editorial Features" design system on
     aura.build — gold accent on near-black with zinc borders. The source's
     web fonts (Inter / Playfair Display / JetBrains Mono) are adapted to
     system serif / mono / sans stacks so the page stays self-contained
     (design brief §4: no web-font fetch). See reference/design-language.md
     for the full token map, rationale, and creator credit. */
  --bg: #000000;
  --surface: #141414;
  --surface-2: #1c1c1c;
  --text: #ffffff;
  --muted: #a1a1aa;
  --accent: #e0a94e;
  --border: #27272a;
  --radius: 8px;
  --font-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, "Times New Roman", serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #faf8f4;
    --surface: #ffffff;
    --surface-2: #f3f1ec;
    --text: #18181b;
    --muted: #52525b;
    --accent: #9a6b1e;
    --border: #e4e4e7;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-ui);
  line-height: 1.5;
}
.digest { max-width: 1040px; margin: 0 auto; padding: 24px 20px 64px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.tldr {
  font-size: 1.15rem;
  font-weight: 600;
  padding: 14px 18px;
  margin-bottom: 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.tldr-label {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  margin-right: 8px;
}
.section-heading {
  font-family: var(--font-mono);
  font-size: 0.8rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 28px 0 12px;
}
/* Tiles layout: each source section (Videos / From X) is a responsive grid.
   Tile SIZE tracks rank — a Hero tile spans the full row, Standard/Compact are
   single-column tiles — so the rank ladder stays legible across the grid. */
.tile-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
}
.tile-grid .card { margin-bottom: 0; height: 100%; }
.tile-grid .card.hero { grid-column: 1 / -1; }
@media (max-width: 640px) {
  .tile-grid { grid-template-columns: 1fr; }
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin-bottom: 14px;
}
.card-title { margin: 0 0 6px; line-height: 1.3; font-family: var(--font-serif); }
.card.hero .card-title { font-size: 1.5rem; }
.card.standard .card-title { font-size: 1.2rem; }
.card-meta { color: var(--muted); font-size: 0.9rem; }
.card-meta .channel { font-weight: 600; color: var(--text); }
.chapters { list-style: none; margin: 12px 0 0; padding: 0; border-top: 1px solid var(--border); }
.chapters li { padding: 4px 0; }
.chapter-link { display: flex; gap: 10px; }
.chapter-time {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--muted);
  min-width: 3.5em;
}
.chapter-title { color: var(--text); }
.card.compact {
  padding: 8px 14px;
  margin-bottom: 6px;
  background: var(--surface-2);
  font-size: 0.95rem;
}
.card.compact .channel { color: var(--muted); }
.index-strip { margin-top: 36px; border-top: 2px solid var(--border); padding-top: 8px; }
.index-list { list-style: none; margin: 0; padding: 0; }
.index-line { padding: 5px 0; font-size: 0.9rem; border-bottom: 1px solid var(--border); }
.index-line .channel { color: var(--muted); }
/* M3 sections (Phase 5): scoops strip, overlap block, right-rail trending. */
.scoops-strip {
  margin: 0 0 24px;
  padding: 14px 18px;
  background: var(--surface-2);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
}
.scoops-heading { margin-top: 0; color: var(--accent); }
.scoops-list { list-style: none; margin: 0; padding: 0; }
.scoop-row { padding: 6px 0; font-weight: 600; }
.scoop-flag {
  display: inline-block;
  font-size: 0.65rem;
  letter-spacing: 0.08em;
  background: var(--accent);
  color: var(--bg);
  padding: 2px 7px;
  border-radius: 4px;
  margin-right: 8px;
  vertical-align: middle;
}
.overlap-block { margin: 0 0 24px; }
.overlap-cluster {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px;
  margin-bottom: 12px;
}
.overlap-topic { font-weight: 600; font-size: 1.05rem; }
.overlap-meta { color: var(--muted); font-size: 0.85rem; margin-top: 2px; }
.overlap-crosslinks { list-style: none; margin: 8px 0 0; padding: 0; border-top: 1px solid var(--border); }
.overlap-crosslinks li { padding: 4px 0; }
.trending-rail {
  margin: 0 0 24px;
  padding: 12px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.trending-list { list-style: none; margin: 0; padding: 0; }
.trending-row { padding: 6px 0; display: flex; gap: 8px; align-items: baseline; justify-content: space-between; }
.trending-tag {
  flex: 0 0 auto;
  font-size: 0.65rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  color: var(--muted);
}
.trending-tag.tag-scoop { color: var(--accent); border-color: var(--accent); }
.trending-tag.tag-corroborated { color: var(--text); }
/* Summary bullets (the cluster-winner headline content) + "Also covered" footnotes. */
.summary { list-style: none; margin: 12px 0 0; padding: 0; border-top: 1px solid var(--border); }
.summary li { padding: 5px 0; }
.summary-link { display: flex; gap: 10px; }
.summary-time {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--accent);
  min-width: 3.5em;
}
.summary-text { color: var(--text); }
.summary-point { color: var(--text); }
.card-footnotes { margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--border); }
.footnote-label {
  font-family: var(--font-mono);
  font-size: 0.65rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
.footnote-list { list-style: none; margin: 4px 0 0; padding: 0; }
.footnote-list li { padding: 2px 0; font-size: 0.85rem; }
"""


def wrap_page(title: str, body_html: str) -> str:
    """Swap the page-template sentinels and return the full self-contained HTML.

    Replaces ``__TITLE__`` (escaped), ``__CSS__`` (the inline stylesheet), and
    ``__BODY__`` (the caller's already-built, already-escaped body markup). The
    body is inserted verbatim — :mod:`lib.render` is responsible for having escaped
    every user-controlled string inside it via the element builders here.

    Args:
        title: The page ``<title>`` text (escaped before insertion).
        body_html: The fully built ``<main>`` body markup.

    Returns:
        The complete ``<!DOCTYPE html>...`` page string.

    Example:
        >>> page = wrap_page("Orbit", "<p>hi</p>")
        >>> page.startswith("<!DOCTYPE html>")
        True
        >>> "</html>" in page
        True
    """
    return (
        HTML_TEMPLATE.replace("__TITLE__", escape(title))
        .replace("__CSS__", CSS)
        .replace("__BODY__", body_html)
    )
