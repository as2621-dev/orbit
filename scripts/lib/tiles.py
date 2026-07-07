"""Tiles-layout markup builders for the Orbit digest (Phase 7, Sub-phase 3).

This module ports the newspaper "Tiles" design
(``scripts/assets/orbit-tiles.dc.html`` → ``out/orbit-tiles-reference.html``) into a
set of PURE, escaped, allowlist-safe builder functions. :mod:`lib.render`
(Sub-phase 4) calls these to assemble the digest body; :mod:`lib.html_render` owns
the page shell, the Tiles class CSS, and inlining the base64 fonts in ``wrap_page``.

Design fidelity: the reference is mostly *inline-styled per element* (only ``.ph`` /
``.tile`` / ``.chip`` / ``.kp`` are classes), so these builders emit the same inline
styles verbatim. Whitespace differences vs. the reference are cosmetic.

Security (design brief §5, non-negotiable):
  * Every user-controlled string (title, channel meta, blurb, chapter text, tweet
    text, cross-link label) is routed through :func:`lib.html_render.escape` so a
    ``<script>`` renders as inert text, never markup.
  * Every ``<a href>`` goes through :func:`lib.html_render.safe_href` (scheme
    allowlist) and every ``<img src>`` through :func:`lib.html_render.safe_img_src`
    (base64-image / http(s) allowlist). An empty/unsafe image src falls back to the
    hatched ``.ph`` placeholder (feature tiles) or is simply omitted (tweet avatars)
    — never a broken or unsafe ``<img>``.

Graceful degradation (Rule 12, no fabrication): empty verdict/blurb omit their
element entirely; hidden-gem subscriber counts and per-item clock times are NOT
captured upstream, so they are never rendered (no made-up "3.1k subs" / "14:20").

Rule 5: no LLM here — pure deterministic string building.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

# Make ``lib`` importable whether this module is loaded as ``lib.tiles`` (via the
# scripts-dir sys.path insert) or run from the scripts dir directly. Mirrors
# html_render.py / rerank.py. Done BEFORE importing from lib.html_render.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib.html_render import escape, is_safe_link_url, safe_href, safe_img_src  # noqa: E402

# --- Trending-now marker categories (the "ahead of the curve" middle tile) ----
# The trending list marks each row by WHERE the signal comes from: a creator who
# broke a long silence (dormant), a topic N of YOUR follows landed on, or a topic
# trending OUTSIDE your network (external). The marker glyph + colour encode it.
CATEGORY_DORMANT: str = "dormant"
CATEGORY_YOURS: str = "yours"
CATEGORY_EXTERNAL: str = "external"

# Per-category render spec: (marker glyph, marker colour, right-label colour). The
# right-label TEXT for CATEGORY_YOURS is computed from the count ("N of yours").
_TRENDING_MARKER_SPEC: dict[str, tuple[str, str, str]] = {
    CATEGORY_DORMANT: ("◆", "#B7472A", "#8a7f6c"),
    CATEGORY_YOURS: ("↗", "#B7472A", "#B7472A"),
    CATEGORY_EXTERNAL: ("○", "#8a7f6c", "#8a7f6c"),
}

# @handle token in a verdict sentence — italicised for the masthead accent. The
# regex runs on the ALREADY-escaped string; ``@`` and ``[A-Za-z0-9_]`` are not
# touched by html.escape, so matching post-escape is safe.
_VERDICT_HANDLE_PATTERN: re.Pattern[str] = re.compile(r"(@[A-Za-z0-9_]+)")


class TrendingRow(NamedTuple):
    """One row of the "Trending now" tile.

    Attributes:
        title: The trending topic / headline text (escaped on render).
        category: One of :data:`CATEGORY_DORMANT` / :data:`CATEGORY_YOURS` /
            :data:`CATEGORY_EXTERNAL` — selects the marker glyph + colour.
        your_count: For :data:`CATEGORY_YOURS`, how many of the user's follows
            landed on this (renders ``"N of yours"``). Ignored otherwise.
        link_url: Optional href for the row.
    """

    title: str
    category: str
    your_count: int = 0
    link_url: str = ""


class ChapterRow(NamedTuple):
    """One chapter key-point row inside a feature tile.

    Attributes:
        chip: The timestamp chip text (e.g. ``"04:20"`` / ``"1:31:00"``).
        text: The chapter title / key-point text (escaped on render).
        url: The ``watch?v=ID&t=Ns`` deep-link the chip points at. A non-empty,
            allowlist-safe url makes the chip a clickable ``<a>``; empty or unsafe
            degrades to an inert ``<span>`` (Rule 12: degrade, don't break).
    """

    chip: str
    text: str
    url: str = ""


class CrossLink(NamedTuple):
    """One "same story, also covered" cross-link inside a feature tile.

    Attributes:
        label: The visible label (e.g. ``"Dwarkesh · 1:12:30"``), escaped on render.
        url: Optional href for the cross-link.
    """

    label: str
    url: str = ""


# --- Small shared element helpers --------------------------------------------


def _render_thumb(image_url: str, placeholder_label: str, thumb_height: int) -> str:
    """Render a tile thumbnail: an inlined ``<img>`` if safe, else the ``.ph`` block.

    Routes ``image_url`` through :func:`safe_img_src`; on an empty/unsafe src it
    NEVER emits a broken ``<img>`` — it falls back to the hatched ``.ph`` placeholder
    carrying ``placeholder_label`` (escaped). This is the design's degradation path
    when a thumbnail could not be inlined.

    Args:
        image_url: A base64 ``data:image/...`` URI or http(s) URL (or empty).
        placeholder_label: The ``.ph`` caption shown when no image is available.
        thumb_height: The thumbnail height in px.

    Returns:
        An ``<img>`` or ``<div class="ph">`` markup string.
    """
    safe_src = safe_img_src(image_url)
    if safe_src:
        return f'<img src="{safe_src}" alt="" style="height:{thumb_height}px;width:100%;object-fit:cover;display:block;">'
    return f'<div class="ph" style="height:{thumb_height}px;width:100%;"><span>{escape(placeholder_label)}</span></div>'


def _render_blurb(summary: str, *, font_size: str, margin_bottom: str, color: str = "#3a342b") -> str:
    """Render the one-line per-item blurb, or ``""`` when empty (omit the element).

    Args:
        summary: The blurb text (escaped). An empty/whitespace value yields ``""``.
        font_size: CSS font-size (e.g. ``"14"`` → ``14px``).
        margin_bottom: CSS margin-bottom value (e.g. ``"12px"`` / ``"auto"``).
        color: CSS text colour.

    Returns:
        A blurb ``<div>`` string, or ``""`` if there is nothing to show.
    """
    if not (summary or "").strip():
        return ""
    return (
        f'<div style="font-size:{font_size}px;line-height:1.45;color:{color};'
        f'margin-bottom:{margin_bottom};">{escape(summary)}</div>'
    )


def _render_kp_rows(chapters: tuple[ChapterRow, ...] | list[ChapterRow]) -> str:
    """Render the chapter key-point rows (``.kp`` list), or ``""`` if none.

    Args:
        chapters: The :class:`ChapterRow` items to render.

    Returns:
        A ``<div>`` wrapping one ``.kp`` row per chapter, or ``""`` if empty.
    """
    if not chapters:
        return ""
    rows = "".join(
        f'<div class="kp">{_render_kp_chip(chapter)}<span class="t">{escape(chapter.text)}</span></div>'
        for chapter in chapters
    )
    return f"<div>{rows}</div>"


def _render_kp_chip(chapter: ChapterRow) -> str:
    """Render one chapter's timestamp chip: a clickable ``<a>`` when the deep-link is safe.

    A non-empty, allowlist-safe ``chapter.url`` renders the chip as
    ``<a class="chip" href="...">`` so the reader clicks straight into the moment; an
    empty or unsafe url (e.g. ``javascript:``) degrades to today's inert
    ``<span class="chip">`` (Rule 12: degrade, don't break). Either way the visual
    ``.chip`` class is kept, so the two forms look identical.

    Args:
        chapter: The :class:`ChapterRow` (read for ``chip`` text + ``url``).

    Returns:
        An ``<a class="chip">`` or ``<span class="chip">`` markup string.
    """
    chip_text = escape(chapter.chip)
    url = getattr(chapter, "url", "") or ""
    if url and is_safe_link_url(url):
        return f'<a class="chip" href="{safe_href(url)}">{chip_text}</a>'
    return f'<span class="chip">{chip_text}</span>'


def _render_more_chapters(more_chapters_count: int, card_url: str) -> str:
    """Render the "+ N more chapters" link, or ``""`` when ``more_chapters_count`` <= 0.

    Args:
        more_chapters_count: How many chapters were NOT shown as ``.kp`` rows.
        card_url: The whole-item deep-link the link points to.

    Returns:
        An ``<a>`` string, or ``""``.
    """
    if more_chapters_count <= 0:
        return ""
    return (
        f'<a href="{safe_href(card_url)}" style="font-family:\'JetBrains Mono\',monospace;'
        f'font-size:11px;color:#8a7f6c;display:inline-block;margin-top:10px;">'
        f"+ {int(more_chapters_count)} more chapters</a>"
    )


def _render_cross_links(cross_links: tuple[CrossLink, ...] | list[CrossLink]) -> str:
    """Render the "Same story, also covered" cross-link block, or ``""`` if none.

    Args:
        cross_links: The :class:`CrossLink` items (other coverage of the same story).

    Returns:
        A bordered cross-link ``<div>`` block, or ``""`` if empty.
    """
    if not cross_links:
        return ""
    links = "".join(
        f'<a href="{safe_href(cross_link.url)}">{escape(cross_link.label)}</a>' for cross_link in cross_links
    )
    return (
        '<div style="margin-top:12px;border-top:1px solid rgba(31,27,22,.14);padding-top:10px;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;letter-spacing:.1em;'
        'color:#8a7f6c;text-transform:uppercase;margin-bottom:6px;">Same story, also covered</div>'
        '<div style="display:flex;flex-wrap:wrap;gap:6px 12px;font-family:\'JetBrains Mono\',monospace;'
        f'font-size:11px;color:#B7472A;">{links}</div></div>'
    )


def _render_meta_row(meta_label: str, flag: str) -> str:
    """Render the tile's mono meta line, optionally paired right with a ``flag``.

    Args:
        meta_label: The ``CHANNEL · DURATION · PLATFORM`` mono line (escaped).
        flag: An optional right-aligned flag (e.g. ``"▲ TOP SIGNAL"`` /
            ``"▲ BEST ON NB2"``). Empty → meta line alone.

    Returns:
        A meta-row ``<div>`` string.
    """
    meta = (
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;'
        f'color:#8a7f6c;">{escape(meta_label)}</div>'
    )
    if not flag:
        return (
            f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;'
            f'color:#8a7f6c;margin-bottom:6px;">{escape(meta_label)}</div>'
        )
    flag_html = (
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:9px;'
        f'color:#B7472A;letter-spacing:.08em;">{escape(flag)}</div>'
    )
    return (
        '<div style="display:flex;justify-content:space-between;align-items:center;'
        f'margin-bottom:7px;">{meta}{flag_html}</div>'
    )


# --- Masthead / verdict ------------------------------------------------------


def render_masthead(
    date_str: str,
    source_total: int,
    accounted: int,
    scoop_count: int,
    dormant_count: int,
    cluster_count: int,
) -> str:
    """Render the newspaper masthead (the "Orbit" wordmark + the day's counts).

    Args:
        date_str: The pre-formatted dateline (e.g. ``"TUESDAY · 17 JUN 2026 · 06:14"``),
            built by the orchestrator; escaped here.
        source_total: Total sources scanned.
        accounted: How many of those are accounted for in the digest.
        scoop_count: Number of scoops surfaced.
        dormant_count: Number of dormant-creator breaks.
        cluster_count: Number of multi-source story clusters.

    Returns:
        The masthead ``<div>`` block.

    Example:
        >>> "Orbit" in render_masthead("MON · 1 JAN 2026", 26, 26, 1, 1, 2)
        True
    """
    return (
        '<div style="display:flex;align-items:flex-end;justify-content:space-between;'
        'padding:34px 0 16px;border-bottom:3px double #1F1B16;">'
        "<div>"
        '<div style="font-family:\'Fraunces\',serif;font-weight:900;font-size:52px;'
        'line-height:.9;letter-spacing:-.02em;">Orbit</div>'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:11px;letter-spacing:.18em;'
        'color:#8a7f6c;text-transform:uppercase;margin-top:7px;">Your people, today — ranked</div>'
        "</div>"
        '<div style="text-align:right;font-family:\'JetBrains Mono\',monospace;font-size:11px;'
        'letter-spacing:.07em;color:#5a5240;line-height:1.7;">'
        f"<div>{escape(date_str)}</div>"
        f'<div><b style="color:#1F1B16;">{int(source_total)} sources · {int(accounted)} accounted for</b></div>'
        f'<div style="color:#B7472A;">{int(scoop_count)} scoop · {int(dormant_count)} dormant · '
        f"{int(cluster_count)} clusters</div>"
        "</div>"
        "</div>"
    )


def render_verdict(verdict_text: str) -> str:
    """Render the LLM editorial verdict sentence, or ``""`` when empty (omit element).

    The text is escaped first, then ``@handle`` tokens are wrapped in italic accent
    spans (matching the design's italicised mentions). The italic transform runs on
    the escaped string and only touches ``@[A-Za-z0-9_]+`` runs, so it can never
    re-introduce active markup. An empty/whitespace verdict returns ``""`` so the
    masthead is followed by no empty container (Rule 12: degrade, don't fake).

    Args:
        verdict_text: The plain-text verdict sentence from the LLM (or empty when
            the LLM was unavailable).

    Returns:
        A verdict ``<div>`` block, or ``""`` if there is no verdict.

    Example:
        >>> render_verdict("")
        ''
        >>> "font-style:italic" in render_verdict("A scoop from @swyx today")
        True
    """
    text = (verdict_text or "").strip()
    if not text:
        return ""
    accented = _VERDICT_HANDLE_PATTERN.sub(r'<span style="font-style:italic;">\1</span>', escape(text))
    return (
        '<div style="padding:26px 0 24px;">'
        '<div style="font-family:\'Fraunces\',serif;font-weight:500;font-size:30px;'
        'line-height:1.3;letter-spacing:-.01em;max-width:1060px;">'
        f"{accented}</div></div>"
    )


# --- Ahead-of-the-curve trio (scoop / trending / hidden gem) -----------------


def render_scoop_tile(
    attribution: str,
    title: str,
    blurb: str,
    link_url: str,
    *,
    link_label: str = "→ read the thread",
) -> str:
    """Render the loud red "scoop" tile (left of the ahead-of-the-curve trio).

    Args:
        attribution: The source attribution after the "The scoop ·" prefix
            (e.g. ``"@swyx · X"``), escaped.
        title: The scoop headline, escaped.
        blurb: The one-line why-it-matters blurb (escaped). Empty → omitted.
        link_url: The href for the read link.
        link_label: The read-link label.

    Returns:
        A red scoop ``.tile`` block.
    """
    has_blurb = bool((blurb or "").strip())
    title_margin = "auto" if not has_blurb else "11px"
    blurb_html = (
        f'<div style="font-size:14.5px;line-height:1.45;opacity:.92;margin-bottom:auto;">{escape(blurb)}</div>'
        if has_blurb
        else ""
    )
    return (
        '<div class="tile" style="background:#B7472A;border-color:#B7472A;padding:22px 24px;'
        'color:#F7F3EA;display:flex;flex-direction:column;margin-bottom:0;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;letter-spacing:.12em;'
        f'text-transform:uppercase;opacity:.85;margin-bottom:12px;">◆ The scoop · {escape(attribution)}</div>'
        '<div style="font-family:\'Fraunces\',serif;font-weight:600;font-size:24px;line-height:1.16;'
        f'margin-bottom:{title_margin};">{escape(title)}</div>'
        f"{blurb_html}"
        f'<a href="{safe_href(link_url)}" style="font-family:\'JetBrains Mono\',monospace;font-size:11px;'
        f'margin-top:16px;border-top:1px solid rgba(247,243,234,.35);padding-top:11px;">{escape(link_label)}</a>'
        "</div>"
    )


def render_trending_now(rows: tuple[TrendingRow, ...] | list[TrendingRow]) -> str:
    """Render the "Trending now" middle tile of the trio, or ``""`` if no rows.

    Each row's marker encodes its category: ``◆`` dormant-creator break, ``↗``
    "N of yours" (topic N of your follows landed on), ``○`` external (trending
    outside your network).

    Args:
        rows: The :class:`TrendingRow` items in display order.

    Returns:
        A trending ``.tile`` block, or ``""`` if there are no rows.
    """
    if not rows:
        return ""
    row_html_parts: list[str] = []
    last_index = len(rows) - 1
    for index, row in enumerate(rows):
        marker, marker_color, label_color = _TRENDING_MARKER_SPEC.get(
            row.category, _TRENDING_MARKER_SPEC[CATEGORY_EXTERNAL]
        )
        if row.category == CATEGORY_YOURS:
            right_label = f"{int(row.your_count)} of yours"
        elif row.category == CATEGORY_DORMANT:
            right_label = "dormant"
        else:
            right_label = "external"
        border = "" if index == last_index else "border-bottom:1px solid rgba(31,27,22,.1);"
        row_html_parts.append(
            f'<a href="{safe_href(row.link_url)}" style="display:flex;align-items:baseline;gap:10px;'
            f'padding:9px 0;{border}">'
            f'<span style="color:{marker_color};font-family:\'JetBrains Mono\',monospace;font-size:12px;'
            f'width:12px;flex:none;">{marker}</span>'
            f'<span style="font-size:15px;line-height:1.25;flex:1;">{escape(row.title)}</span>'
            f'<span style="font-family:\'JetBrains Mono\',monospace;font-size:9.5px;color:{label_color};'
            f'white-space:nowrap;">{escape(right_label)}</span></a>'
        )
    return (
        '<div class="tile" style="padding:22px 24px;display:flex;flex-direction:column;margin-bottom:0;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;letter-spacing:.12em;'
        'text-transform:uppercase;color:#5a5240;margin-bottom:14px;">↗ Trending now</div>'
        '<div style="display:flex;flex-direction:column;gap:0;">' + "".join(row_html_parts) + "</div></div>"
    )


def render_hidden_gem(
    channel_label: str,
    velocity_pct: int,
    title: str,
    blurb: str,
    *,
    chip_time: str = "",
    chip_label: str = "",
    link_url: str = "",
) -> str:
    """Render the "Hidden gem" right tile of the trio (velocity %, NO subscriber count).

    Per the locked decisions, subscriber counts and per-item clock times are NOT
    captured upstream, so the meta line carries the channel ONLY — never a fabricated
    "3.1k subs" / "14:20".

    Args:
        channel_label: The channel meta label (escaped; pass pre-cased if uppercase
            is desired).
        velocity_pct: The view-velocity percentage (renders ``"+N% today"``).
        title: The episode title, escaped.
        blurb: The one-line blurb (escaped). Empty → omitted.
        chip_time: Optional key-moment timestamp chip (e.g. ``"02:50"``). Empty →
            the key-moment link is omitted.
        chip_label: The key-moment label paired with ``chip_time``.
        link_url: The href for the key-moment link.

    Returns:
        A hidden-gem ``.tile`` block.
    """
    has_blurb = bool((blurb or "").strip())
    blurb_html = (
        f'<div style="font-size:14px;line-height:1.45;color:#3a342b;margin-bottom:auto;">{escape(blurb)}</div>'
        if has_blurb
        else ""
    )
    chip_html = ""
    if (chip_time or "").strip():
        chip_html = (
            f'<a href="{safe_href(link_url)}" style="display:flex;gap:7px;align-items:baseline;margin-top:14px;'
            'border-top:1px solid rgba(31,27,22,.12);padding-top:11px;">'
            f'<span class="chip">{escape(chip_time)}</span>'
            f'<span style="font-size:13px;color:#2a251e;">{escape(chip_label)}</span></a>'
        )
    return (
        '<div class="tile" style="padding:0;display:flex;flex-direction:column;margin-bottom:0;overflow:hidden;">'
        '<div style="background:#2a251e;color:#e8a594;font-family:\'JetBrains Mono\',monospace;font-size:10px;'
        'letter-spacing:.12em;text-transform:uppercase;padding:9px 18px;display:flex;'
        'justify-content:space-between;align-items:center;">'
        f'<span>✦ Hidden gem</span><span style="color:#F7F3EA;">+{int(velocity_pct)}% today</span></div>'
        '<div style="padding:18px 20px;display:flex;flex-direction:column;flex:1;">'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#8a7f6c;'
        f'margin-bottom:8px;">{escape(channel_label)}</div>'
        '<div style="font-family:\'Fraunces\',serif;font-weight:600;font-size:20px;line-height:1.16;'
        f'margin-bottom:9px;">{escape(title)}</div>'
        f"{blurb_html}{chip_html}</div></div>"
    )


def render_ahead_trio(scoop_tile: str, trending_tile: str, gem_tile: str) -> str:
    """Wrap the three pre-rendered trio tiles in the "Ahead of the curve" grid.

    Returns ``""`` if all three tiles are empty (a quiet day with nothing ahead of
    the curve gets no empty section).

    Args:
        scoop_tile: HTML from :func:`render_scoop_tile` (or ``""``).
        trending_tile: HTML from :func:`render_trending_now` (or ``""``).
        gem_tile: HTML from :func:`render_hidden_gem` (or ``""``).

    Returns:
        The "Ahead of the curve" section ``<div>``, or ``""`` if all tiles empty.
    """
    if not (scoop_tile or trending_tile or gem_tile):
        return ""
    return (
        '<div style="margin-bottom:32px;">'
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:11px;letter-spacing:.18em;'
        'color:#B7472A;text-transform:uppercase;">Ahead of the curve</div>'
        '<div style="flex:1;height:1px;background:rgba(183,71,42,.3);"></div></div>'
        '<div style="display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:16px;align-items:stretch;">'
        f"{scoop_tile}{trending_tile}{gem_tile}</div></div>"
    )


# --- Ranked masonry: per-tile builders + container ---------------------------


def _render_feature_tile(
    *,
    image_url: str,
    placeholder_label: str,
    meta_label: str,
    flag: str,
    title: str,
    summary: str,
    chapters: tuple[ChapterRow, ...] | list[ChapterRow],
    more_chapters_count: int,
    cross_links: tuple[CrossLink, ...] | list[CrossLink],
    card_url: str,
    thumb_height: int,
    body_padding: str,
    title_font_size: str,
    blurb_font_size: str,
) -> str:
    """Shared builder for the Hero/Standard feature tiles (thumb + chapters + links).

    Hero and Standard differ only by size knobs; both share this body. See
    :func:`render_hero_tile` / :func:`render_standard_tile` for the public entry
    points and argument docs.

    Returns:
        A feature ``.tile`` block.
    """
    return (
        '<div class="tile">'
        f"{_render_thumb(image_url, placeholder_label, thumb_height)}"
        f'<div style="padding:{body_padding};">'
        f"{_render_meta_row(meta_label, flag)}"
        '<div style="font-family:\'Fraunces\',serif;font-weight:600;'
        f'font-size:{title_font_size}px;line-height:1.16;margin-bottom:9px;">'
        f'<a href="{safe_href(card_url)}">{escape(title)}</a></div>'
        f"{_render_blurb(summary, font_size=blurb_font_size, margin_bottom='12px')}"
        f"{_render_kp_rows(chapters)}"
        f"{_render_more_chapters(more_chapters_count, card_url)}"
        f"{_render_cross_links(cross_links)}"
        "</div></div>"
    )


def render_hero_tile(
    *,
    image_url: str = "",
    placeholder_label: str = "",
    meta_label: str,
    title: str,
    flag: str = "",
    summary: str = "",
    chapters: tuple[ChapterRow, ...] | list[ChapterRow] = (),
    more_chapters_count: int = 0,
    cross_links: tuple[CrossLink, ...] | list[CrossLink] = (),
    card_url: str = "",
    thumb_height: int = 188,
) -> str:
    """Render the Hero tile — the loudest masonry card (tall thumb, biggest title).

    Args:
        image_url: Inlined thumbnail src (base64/http(s)); empty → ``.ph`` fallback.
        placeholder_label: The ``.ph`` caption when no thumbnail is available.
        meta_label: The ``CHANNEL · DURATION · PLATFORM`` mono line (escaped).
        title: The episode title, escaped.
        flag: Optional right flag (e.g. ``"▲ TOP SIGNAL"``).
        summary: One-line blurb (escaped); empty → omitted.
        chapters: Chapter ``.kp`` rows.
        more_chapters_count: Count for the "+ N more chapters" link (0 → omitted).
        cross_links: "Same story, also covered" cross-links.
        card_url: The whole-item deep-link (used by the more-chapters link).
        thumb_height: Thumbnail height in px.

    Returns:
        A Hero ``.tile`` block.

    Example:
        >>> html = render_hero_tile(meta_label="DWARKESH · 1:52:14 · YouTube", title="<x>")
        >>> "class=\\"tile\\"" in html and "&lt;x&gt;" in html
        True
    """
    return _render_feature_tile(
        image_url=image_url,
        placeholder_label=placeholder_label,
        meta_label=meta_label,
        flag=flag,
        title=title,
        summary=summary,
        chapters=chapters,
        more_chapters_count=more_chapters_count,
        cross_links=cross_links,
        card_url=card_url,
        thumb_height=thumb_height,
        body_padding="16px 18px",
        title_font_size="23",
        blurb_font_size="14",
    )


def render_standard_tile(
    *,
    image_url: str = "",
    placeholder_label: str = "",
    meta_label: str,
    title: str,
    flag: str = "",
    summary: str = "",
    chapters: tuple[ChapterRow, ...] | list[ChapterRow] = (),
    more_chapters_count: int = 0,
    cross_links: tuple[CrossLink, ...] | list[CrossLink] = (),
    card_url: str = "",
    thumb_height: int = 120,
    title_font_size: str = "19",
) -> str:
    """Render a Standard tile — a mid-weight masonry card (smaller than Hero).

    Same shape as :func:`render_hero_tile` with smaller defaults. ``title_font_size``
    is exposed so the orchestrator can match the design's per-tile sizing
    (20px for the primary pick, 18-19px for the rest).

    Args:
        image_url: Inlined thumbnail src; empty → ``.ph`` fallback.
        placeholder_label: The ``.ph`` caption when no thumbnail is available.
        meta_label: The mono meta line (escaped).
        title: The episode title, escaped.
        flag: Optional right flag (e.g. ``"▲ BEST ON NB2"``).
        summary: One-line blurb (escaped); empty → omitted.
        chapters: Chapter ``.kp`` rows.
        more_chapters_count: Count for the "+ N more chapters" link (0 → omitted).
        cross_links: "Same story, also covered" cross-links.
        card_url: The whole-item deep-link.
        thumb_height: Thumbnail height in px.
        title_font_size: Title font-size in px (no unit).

    Returns:
        A Standard ``.tile`` block.
    """
    return _render_feature_tile(
        image_url=image_url,
        placeholder_label=placeholder_label,
        meta_label=meta_label,
        flag=flag,
        title=title,
        summary=summary,
        chapters=chapters,
        more_chapters_count=more_chapters_count,
        cross_links=cross_links,
        card_url=card_url,
        thumb_height=thumb_height,
        body_padding="15px 17px",
        title_font_size=title_font_size,
        blurb_font_size="13.5",
    )


def render_compact_tile(
    *,
    meta_label: str,
    title: str,
    summary: str = "",
    chip_time: str = "",
    chip_label: str = "",
    link_url: str = "",
    title_font_size: str = "18",
) -> str:
    """Render a Compact tile — thumbnail-less, one optional key-moment chip-link.

    Covers the design's short/condensed YouTube tiles (Theo, Fireship): meta line,
    title, optional one-line blurb, and a single chip deep-link instead of a full
    ``.kp`` chapter list.

    Args:
        meta_label: The mono meta line (escaped).
        title: The episode title, escaped.
        summary: Optional one-line blurb (escaped); empty → omitted.
        chip_time: Optional single key-moment timestamp (e.g. ``"18:20"``). Empty →
            the chip-link is omitted.
        chip_label: The key-moment label paired with ``chip_time``.
        link_url: The href for the chip-link.
        title_font_size: Title font-size in px (no unit).

    Returns:
        A Compact ``.tile`` block.
    """
    chip_html = ""
    if (chip_time or "").strip():
        chip_html = (
            f'<a href="{safe_href(link_url)}" style="display:flex;gap:7px;align-items:baseline;">'
            f'<span class="chip">{escape(chip_time)}</span>'
            f'<span style="font-size:12.5px;color:#2a251e;">{escape(chip_label)}</span></a>'
        )
    return (
        '<div class="tile"><div style="padding:14px 16px;">'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#8a7f6c;'
        f'margin-bottom:6px;">{escape(meta_label)}</div>'
        '<div style="font-family:\'Fraunces\',serif;font-weight:600;'
        f'font-size:{title_font_size}px;line-height:1.16;margin-bottom:8px;">'
        f'<a href="{safe_href(link_url)}">{escape(title)}</a></div>'
        f"{_render_blurb(summary, font_size='13.5', margin_bottom='10px')}"
        f"{chip_html}</div></div>"
    )


def render_tweet_tile(
    *,
    source_label: str,
    text: str,
    link_url: str = "",
    link_label: str = "→ post",
    avatar_url: str = "",
) -> str:
    """Render a tweet (X) tile — text-first, with the deliberate unavatar extension.

    The design's tweet tiles are text-only; the user opted to ADD a profile avatar
    (an ``unavatar.io`` image) inlined here. The avatar goes through
    :func:`safe_img_src`; an empty/unsafe src simply omits the ``<img>`` (no ``.ph``
    fallback — tweets are text-first), never a broken image.

    Args:
        source_label: The handle/source line (e.g. ``"@levelsio · X"``), escaped.
        text: The tweet/summary text, escaped.
        link_url: The href for the read link.
        link_label: The read-link label (e.g. ``"→ post"`` / ``"→ repo"``).
        avatar_url: Optional inlined avatar src (base64/http(s)); empty/unsafe →
            no avatar rendered.

    Returns:
        A tweet ``.tile`` block (cream ``#F2ECE0`` background).
    """
    safe_avatar = safe_img_src(avatar_url)
    avatar_html = (
        f'<img src="{safe_avatar}" alt="" style="width:22px;height:22px;border-radius:50%;'
        'object-fit:cover;flex:none;">'
        if safe_avatar
        else ""
    )
    return (
        '<div class="tile" style="background:#F2ECE0;"><div style="padding:13px 15px;">'
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">'
        f"{avatar_html}"
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:9.5px;color:#8a7f6c;">'
        f"{escape(source_label)}</div></div>"
        f'<div style="font-size:13.5px;line-height:1.4;color:#2a251e;">{escape(text)}</div>'
        f'<a href="{safe_href(link_url)}" style="font-family:\'JetBrains Mono\',monospace;font-size:10.5px;'
        f'color:#B7472A;margin-top:9px;display:inline-block;">{escape(link_label)}</a>'
        "</div></div>"
    )


def render_feed_masonry(tiles_html: str) -> str:
    """Wrap the ranked tiles in the "From your feed · ranked" 3-column masonry.

    Args:
        tiles_html: The concatenated per-tile HTML (Hero/Standard/Compact/Tweet),
            already built + escaped, in DOM order ≈ rank.

    Returns:
        The masonry section ``<div>`` (heading + ``column-count:3`` container).

    Example:
        >>> "From your feed" in render_feed_masonry("<div class=\\"tile\\"></div>")
        True
    """
    return (
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:11px;letter-spacing:.18em;'
        'color:#5a5240;text-transform:uppercase;">From your feed · ranked</div>'
        '<div style="flex:1;height:1px;background:rgba(31,27,22,.18);"></div>'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#8a7f6c;">'
        "bigger tile = higher signal</div></div>"
        '<div style="column-count:3;column-gap:18px;">'
        f"{tiles_html}</div>"
    )


def render_footer(accounted_str: str, page_2_href: str) -> str:
    """Render the double-rule footer (accounted-for line + optional page-2 link).

    Args:
        accounted_str: The mono summary line (e.g. ``"26 OF 26 SOURCES ACCOUNTED FOR"``),
            escaped.
        page_2_href: The href to the overflow page. Empty → no page-2 link is shown
            (single-page digest).

    Returns:
        The footer ``<div>`` block.
    """
    page_2_html = (
        f'<a href="{safe_href(page_2_href)}" style="color:#B7472A;">Full archive · page 2 →</a>'
        if (page_2_href or "").strip()
        else ""
    )
    return (
        '<div style="margin-top:26px;padding-top:16px;border-top:3px double #1F1B16;display:flex;'
        'justify-content:space-between;font-family:\'JetBrains Mono\',monospace;font-size:11px;'
        'letter-spacing:.06em;color:#8a7f6c;">'
        f"<span>{escape(accounted_str)}</span>{page_2_html}</div>"
    )
