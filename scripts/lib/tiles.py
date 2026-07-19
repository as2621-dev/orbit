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

Graceful degradation (Rule 12, no fabrication): an empty blurb / chapter list / image
omits its element entirely rather than rendering a placeholder claim.

Rule 5: no LLM here — pure deterministic string building.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

# Make ``lib`` importable whether this module is loaded as ``lib.tiles`` (via the
# scripts-dir sys.path insert) or run from the scripts dir directly. Mirrors
# html_render.py / rerank.py. Done BEFORE importing from lib.html_render.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib.html_render import escape, is_safe_link_url, safe_href, safe_img_src  # noqa: E402


# --- Layout geometry ---------------------------------------------------------
# The masonry is packed HERE, in Python, rather than left to CSS `column-count`:
# CSS column balancing plus `break-inside:avoid` leaves ragged, half-empty column
# bottoms. We greedily place each tile in the currently-shortest column (see
# :func:`pack_tiles_into_columns`), then let every tile in a column share the leftover
# slack (`.col > .tile{flex:1 1 auto}` in lib.html_render.CSS) so each column ends flush
# on the same baseline — a newspaper page with no dead space. The slack is SPREAD rather
# than dumped on the last tile, which would leave one conspicuously hollow card.
MASONRY_COLUMN_COUNT: int = 3
MASONRY_COLUMN_GAP_PX: int = 18

# Nominal rendered width of one masonry column inside the 1360px-max shell
# (1360 - 2*40 page padding - 2*18 gaps) / 3. Only used for the height ESTIMATES that
# drive column packing, never emitted as a fixed width — the columns are fluid.
COLUMN_WIDTH_PX: int = 414

# Thumbnail height at :data:`COLUMN_WIDTH_PX`. Tiles render thumbs as `aspect-ratio:16/9`
# — YouTube's native thumbnail ratio — so a thumbnail is shown WHOLE rather than
# center-cropped to an arbitrary fixed height. This constant only mirrors that ratio for
# the packing estimate.
THUMB_HEIGHT_PX: int = round(COLUMN_WIDTH_PX * 9 / 16)

# Per-element px costs for the packing estimate. Deliberately coarse: packing only needs
# tiles ranked by relative height, and the stretch rule absorbs the residual error.
_PADDING_PX: int = 32
_META_ROW_PX: int = 22
_CHAPTER_ROW_PX: int = 26
_MORE_CHAPTERS_PX: int = 24
_CROSS_LINKS_PX: int = 58
_CHIP_ROW_PX: int = 24
_BLURB_LINE_PX: int = 20
# How many characters fit on one line at COLUMN_WIDTH_PX, by role. Serif body text is
# denser per px than the big Fraunces titles, hence the split.
_BLURB_CHARS_PER_LINE: int = 52
_TITLE_CHARS_PER_LINE_AT_19PX: int = 32


class TileBlock(NamedTuple):
    """One rendered tile plus the estimated height the packer places it by.

    Every ``render_*_tile`` builder returns this instead of a bare string: the builder
    is the only place that knows what it actually emitted (thumb? how many chapter rows?
    how long is the title?), so it is the honest place to size the tile.

    Attributes:
        html: The tile's markup.
        height_px: A coarse estimate of the tile's rendered height, used ONLY to balance
            :func:`pack_tiles_into_columns`. Not a measured layout, and deliberately
            unrelated to ``lib.render.estimate_page_height`` (which sizes whole PAGES
            against a separately-tuned spill budget).
    """

    html: str
    height_px: int


def _text_line_count(text: str, chars_per_line: int) -> int:
    """Estimate how many wrapped lines ``text`` occupies at ``chars_per_line``.

    Args:
        text: The text to measure. Empty/whitespace yields 0 lines (the element is omitted).
        chars_per_line: Characters that fit on one rendered line.

    Returns:
        The estimated line count (0 for empty text).

    Example:
        >>> _text_line_count("", 30), _text_line_count("a" * 61, 30)
        (0, 3)
    """
    stripped = (text or "").strip()
    if not stripped:
        return 0
    return math.ceil(len(stripped) / chars_per_line)


def _title_height_px(title: str, title_font_size: str) -> int:
    """Estimate a tile title's rendered height at ``title_font_size``.

    Bigger type both wraps sooner (fewer chars per line) and sets taller lines, so the
    per-line char budget is scaled off the 19px reference.

    Args:
        title: The title text.
        title_font_size: The title font-size in px, no unit (e.g. ``"23"``).

    Returns:
        The estimated title block height in px.
    """
    font_size = float(title_font_size)
    chars_per_line = max(12, round(_TITLE_CHARS_PER_LINE_AT_19PX * 19 / font_size))
    return round(_text_line_count(title, chars_per_line) * font_size * 1.16)


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


def _render_thumb(image_url: str, placeholder_label: str) -> str:
    """Render a tile thumbnail at YouTube's native 16:9, or the ``.ph`` block.

    The thumbnail is sized by ASPECT RATIO, not a fixed px height: every source
    thumbnail Orbit inlines is a 16:9 YouTube still, so ``aspect-ratio:16/9`` shows it
    whole at whatever width the column happens to be, instead of center-cropping it to
    an arbitrary height. ``object-fit:cover`` stays as the guard for the odd
    off-ratio source (an avatar-shaped fallback image), which is cropped rather than
    allowed to distort the tile.

    Routes ``image_url`` through :func:`safe_img_src`; on an empty/unsafe src it
    NEVER emits a broken ``<img>`` — it falls back to the hatched ``.ph`` placeholder
    carrying ``placeholder_label`` (escaped), at the same 16:9 box so the layout does
    not shift between a thumbnailed and a thumbnail-less tile.

    Args:
        image_url: A base64 ``data:image/...`` URI or http(s) URL (or empty).
        placeholder_label: The ``.ph`` caption shown when no image is available.

    Returns:
        An ``<img>`` or ``<div class="ph">`` markup string.
    """
    box = "width:100%;aspect-ratio:16/9;"
    safe_src = safe_img_src(image_url)
    if safe_src:
        return f'<img src="{safe_src}" alt="" style="{box}object-fit:cover;display:block;">'
    return f'<div class="ph" style="{box}"><span>{escape(placeholder_label)}</span></div>'


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


# --- Masthead ----------------------------------------------------------------


def render_masthead(date_str: str, tracked_total: int, posted_count: int, item_count: int) -> str:
    """Render the newspaper masthead (the "Orbit" wordmark + the day's coverage counts).

    The counts line answers ONE question — "is Orbit watching everything I follow?" —
    so it reports coverage, not editorial signal: how many channels/accounts are
    tracked in total, how many of those posted today, and how many items that produced.

    Args:
        date_str: The pre-formatted dateline (e.g. ``"TUESDAY · 17 JUN 2026"``), built
            by the orchestrator; escaped here.
        tracked_total: Every source Orbit watches (all YouTube channels + X accounts),
            whether or not they posted today.
        posted_count: How many of those tracked sources appear in today's digest.
        item_count: How many items the digest carries.

    Returns:
        The masthead ``<div>`` block.

    Example:
        >>> "142 TRACKED · 9 POSTED · 14 ITEMS" in render_masthead("MON · 1 JAN 2026", 142, 9, 14)
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
        f'<div><b style="color:#1F1B16;">{int(tracked_total)} TRACKED · {int(posted_count)} POSTED · '
        f"{int(item_count)} ITEMS</b></div>"
        "</div>"
        "</div>"
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
    body_padding: str,
    title_font_size: str,
    blurb_font_size: str,
) -> TileBlock:
    """Shared builder for the Hero/Standard feature tiles (thumb + chapters + links).

    Hero and Standard differ only by size knobs; both share this body. See
    :func:`render_hero_tile` / :func:`render_standard_tile` for the public entry
    points and argument docs.

    Returns:
        A feature ``.tile`` :class:`TileBlock`.
    """
    height_px = (
        THUMB_HEIGHT_PX
        + _PADDING_PX
        + _META_ROW_PX
        + _title_height_px(title, title_font_size)
        + _text_line_count(summary, _BLURB_CHARS_PER_LINE) * _BLURB_LINE_PX
        + len(chapters) * _CHAPTER_ROW_PX
        + (_MORE_CHAPTERS_PX if more_chapters_count > 0 else 0)
        + (_CROSS_LINKS_PX if cross_links else 0)
    )
    html = (
        '<div class="tile">'
        f"{_render_thumb(image_url, placeholder_label)}"
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
    return TileBlock(html=html, height_px=height_px)


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
) -> TileBlock:
    """Render the Hero tile — the loudest masonry card (biggest title).

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

    Returns:
        A Hero ``.tile`` :class:`TileBlock`.

    Example:
        >>> tile = render_hero_tile(meta_label="DWARKESH · 1:52:14 · YouTube", title="<x>")
        >>> "class=\\"tile\\"" in tile.html and "&lt;x&gt;" in tile.html
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
    title_font_size: str = "19",
) -> TileBlock:
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
        title_font_size: Title font-size in px (no unit).

    Returns:
        A Standard ``.tile`` :class:`TileBlock`.
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
) -> TileBlock:
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
        A Compact ``.tile`` :class:`TileBlock`.
    """
    chip_html = ""
    if (chip_time or "").strip():
        chip_html = (
            f'<a href="{safe_href(link_url)}" style="display:flex;gap:7px;align-items:baseline;">'
            f'<span class="chip">{escape(chip_time)}</span>'
            f'<span style="font-size:12.5px;color:#2a251e;">{escape(chip_label)}</span></a>'
        )
    height_px = (
        _PADDING_PX
        + _META_ROW_PX
        + _title_height_px(title, title_font_size)
        + _text_line_count(summary, _BLURB_CHARS_PER_LINE) * _BLURB_LINE_PX
        + (_CHIP_ROW_PX if chip_html else 0)
    )
    html = (
        '<div class="tile"><div style="padding:14px 16px;">'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#8a7f6c;'
        f'margin-bottom:6px;">{escape(meta_label)}</div>'
        '<div style="font-family:\'Fraunces\',serif;font-weight:600;'
        f'font-size:{title_font_size}px;line-height:1.16;margin-bottom:8px;">'
        f'<a href="{safe_href(link_url)}">{escape(title)}</a></div>'
        f"{_render_blurb(summary, font_size='13.5', margin_bottom='10px')}"
        f"{chip_html}</div></div>"
    )
    return TileBlock(html=html, height_px=height_px)


def render_tweet_tile(
    *,
    source_label: str,
    text: str,
    link_url: str = "",
    link_label: str = "→ post",
    avatar_url: str = "",
) -> TileBlock:
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
        A tweet ``.tile`` :class:`TileBlock` (cream ``#F2ECE0`` background).
    """
    safe_avatar = safe_img_src(avatar_url)
    avatar_html = (
        f'<img src="{safe_avatar}" alt="" style="width:22px;height:22px;border-radius:50%;'
        'object-fit:cover;flex:none;">'
        if safe_avatar
        else ""
    )
    height_px = _PADDING_PX + _META_ROW_PX + _text_line_count(text, _BLURB_CHARS_PER_LINE) * 19 + _CHIP_ROW_PX
    html = (
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
    return TileBlock(html=html, height_px=height_px)


def pack_tiles_into_columns(tile_blocks: Sequence[TileBlock], column_count: int) -> list[list[str]]:
    """Greedily distribute tiles across ``column_count`` columns, shortest column first.

    This is what replaces CSS ``column-count`` balancing: each tile in rank order goes
    to whichever column is currently shortest, so the columns end within roughly one
    tile of each other instead of one dying half a page early. Ties break to the
    left-most column, which keeps the highest-ranked tiles reading left-to-right.

    Pure function over the estimated heights — no markup is parsed or measured (Rule 5).

    Args:
        tile_blocks: The tiles in rank order.
        column_count: How many columns to fill (>= 1).

    Returns:
        One list of tile HTML strings per column, in placement order.

    Example:
        >>> blocks = [TileBlock("a", 100), TileBlock("b", 10), TileBlock("c", 10)]
        >>> pack_tiles_into_columns(blocks, 2)
        [['a'], ['b', 'c']]
    """
    columns: list[list[str]] = [[] for _ in range(column_count)]
    column_heights: list[int] = [0] * column_count
    for tile_block in tile_blocks:
        shortest_index = column_heights.index(min(column_heights))
        columns[shortest_index].append(tile_block.html)
        column_heights[shortest_index] += tile_block.height_px
    return columns


def render_feed_masonry(tile_blocks: Sequence[TileBlock], *, heading: str, note: str = "") -> str:
    """Wrap a run of tiles in a headed, gap-free masonry section.

    The page uses this twice — once for the YouTube tiles, once for the X posts
    below them — so the heading (and its optional right-hand note) is a parameter
    rather than a hardcoded string.

    Two things keep the grid newspaper-tight rather than ragged:
      * the columns are packed HERE (:func:`pack_tiles_into_columns`) instead of by CSS
        column balancing, so no column runs out of tiles early; and
      * a short run uses FEWER columns (``min(3, len(tile_blocks))``) rather than leaving
        an empty one — two X posts fill the row as two half-width tiles instead of
        huddling on the left with a dead third column.
    The tiles in each column then share whatever slack remains (the ``.col > .tile``
    flex rule), so the section ends flush on one baseline.

    Args:
        tile_blocks: The per-tile :class:`TileBlock` items (Hero/Standard/Compact/Tweet),
            already built + escaped, in rank order.
        heading: The section heading (e.g. ``"From YouTube · ranked"``), escaped.
        note: An optional right-aligned note (e.g. ``"bigger tile = higher signal"``);
            empty → the note element is omitted.

    Returns:
        The masonry section ``<div>`` (heading + packed flex-column container).

    Example:
        >>> "From YouTube" in render_feed_masonry([TileBlock("<div></div>", 10)],
        ...                                       heading="From YouTube · ranked")
        True
    """
    note_html = (
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:10px;color:#8a7f6c;">{escape(note)}</div>'
        if (note or "").strip()
        else ""
    )
    blocks = list(tile_blocks)
    column_count = max(1, min(MASONRY_COLUMN_COUNT, len(blocks)))
    columns_html = "".join(
        f'<div class="col">{"".join(column)}</div>' for column in pack_tiles_into_columns(blocks, column_count)
    )
    return (
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;margin-top:28px;">'
        '<div style="font-family:\'JetBrains Mono\',monospace;font-size:11px;letter-spacing:.18em;'
        f'color:#5a5240;text-transform:uppercase;">{escape(heading)}</div>'
        '<div style="flex:1;height:1px;background:rgba(31,27,22,.18);"></div>'
        f"{note_html}</div>"
        f'<div style="display:flex;align-items:stretch;gap:{MASONRY_COLUMN_GAP_PX}px;">'
        f"{columns_html}</div>"
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
