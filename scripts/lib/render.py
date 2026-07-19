"""Tiles-layout render orchestration for the Orbit digest (Phase 7 / Sub-phase 4).

This is the orchestration layer ABOVE :mod:`lib.tiles`: it consumes the
:class:`lib.density.TieredItem` list (already rank-ordered, already tiered) plus the
M3 ``clusters`` and the per-item LLM ``summaries``, and assembles ONE self-contained
newspaper "Tiles" HTML digest. :mod:`lib.tiles` owns all the markup/CSS primitives and
the XSS-safe link/image helpers; this module decides WHICH items go where and in what
order.

Layout (top to bottom) — deliberately SHORT, three sections and a rule:
  1. **masthead** — the "Orbit" wordmark, the dateline, and the coverage counts
     (``N TRACKED · M POSTED · K ITEMS``), pure counting (Rule 5).
  2. **"From YouTube · ranked" masonry** — a 3-column tile grid, one tile per video,
     by density tier: Hero (biggest) / Standard / Compact. Each feature tile is a
     16:9 thumbnail (YouTube's native ratio), a title, the one-line blurb, and the
     timestamped chapter bullets. Thumbnails are base64-inlined at render time via an
     INJECTABLE ``inline_image`` seam (so tests stub it — no network). The columns are
     packed by :func:`lib.tiles.pack_tiles_into_columns` so none of them runs dry
     early — see that module for why the layout is not left to CSS ``column-count``.
  3. **"From X" masonry** — the X posts, BELOW the videos, in their own section.
  4. **footer** — the coverage line + an optional "Full archive · page 2 →" link.

There is deliberately NO editorial layer above the feed — no LLM verdict headline and
no "ahead of the curve" scoop/trending/hidden-gem trio. The digest opens on the videos
themselves; the masthead counts are the only claim the page makes about the day, and
they are coverage facts, not judgments.

THE CORE INVARIANT (api-contracts derank contract): rank controls DENSITY, never
INCLUSION. Every tiered item appears SOMEWHERE on the page — a Hero big tile down to a
Compact/tweet tile — nothing is dropped. The 2-page spill (low tiers to page 2) is
preserved: Hero + Standard stay on page 1 (which gains the footer page-2 link);
Compact + Index spill to page 2.

Rule 5: there is NO LLM here — rendering is pure deterministic string building. The
summaries are computed UPSTREAM (orbit.py) and passed in; image inlining goes through
the injectable ``inline_image`` seam.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.render`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors rerank.py / density.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import chat_bridge, images, log, tiles  # noqa: E402  (import must follow the sys.path inserts above)
from lib.html_render import _format_timestamp, wrap_page  # noqa: E402
from lib.density import TIER_COMPACT, TIER_HERO, TIER_INDEX, TIER_STANDARD, TieredItem  # noqa: E402

# Default page <title> when the config carries no override.
DEFAULT_DIGEST_TITLE: str = "Orbit · Today"

# Tiers whose tiles carry the full chapter-list + summary (the loud feature tiles).
_FEATURE_TIERS: frozenset[str] = frozenset({TIER_HERO, TIER_STANDARD})

# The injectable image-inline seam: a remote image URL in, a base64 ``data:`` URI (or
# None) out. Defaults to the real build-time fetch; tests stub it so NO network fires.
InlineImage = Callable[[str], Optional[str]]

# How many chapter ``.kp`` rows a feature tile shows before the "+ N more chapters"
# link. Matches the design (3-4 visible rows, the rest behind the more-chapters link).
_MAX_VISIBLE_CHAPTERS: int = 4

# Section headings for the two masonry runs (YouTube first, X below it).
_YOUTUBE_SECTION_HEADING: str = "From YouTube · ranked"
_YOUTUBE_SECTION_NOTE: str = "bigger tile = higher signal"
_X_SECTION_HEADING: str = "From X"

# --- Page-budget height heuristic (2-page spill) -----------------------------
# FIRST-CUT, TUNABLE estimate-by-content table — NOT a measured layout (stdlib-first,
# no headless browser). Page height is APPROXIMATED by summing a per-tier estimated px
# cost across the items plus a per-chapter increment. Mirrors the CSS density ladder.
TIER_HEIGHT_PX: dict[str, int] = {
    TIER_HERO: 220,
    TIER_STANDARD: 150,
    TIER_COMPACT: 44,
    TIER_INDEX: 30,
}
# Additional px per chapter row on a feature tile (chapter lists add height).
CHAPTER_HEIGHT_PX: int = 26
# Fixed chrome (masthead + section headings + margins) added once per page estimate.
PAGE_CHROME_PX: int = 120
# Page-1 height budget. When estimate_page_height(...) exceeds this, Compact+Index
# spill to page 2. First-cut: ~one tall screen / A4 page. Tunable.
PAGE_1_BUDGET_PX: int = 1400

# The page-2 filename Orbit writes beside page 1 (orbit.py writes the files; this is the
# default href page 1 links to in the footer).
DEFAULT_PAGE_2_FILENAME: str = "today-page2.html"


def estimate_page_height(tiered_items: list[TieredItem]) -> int:
    """Estimate a digest's rendered height in px (FIRST-CUT content heuristic).

    Sums :data:`TIER_HEIGHT_PX` per item by its density tier, plus
    :data:`CHAPTER_HEIGHT_PX` for every chapter on a feature (Hero/Standard) tile, plus
    the fixed :data:`PAGE_CHROME_PX` once. A deliberate ESTIMATE-BY-CONTENT, not a
    measured layout — used only to decide whether page 1 overflows its budget. Pure
    function, no I/O (Rule 5).

    Args:
        tiered_items: The :class:`TieredItem` list (already tiered).

    Returns:
        The estimated total height in px.

    Example:
        >>> estimate_page_height([])
        120
    """
    total_px = PAGE_CHROME_PX
    for tiered_item in tiered_items:
        total_px += TIER_HEIGHT_PX.get(tiered_item.density_tier, TIER_HEIGHT_PX[TIER_INDEX])
        if tiered_item.density_tier in _FEATURE_TIERS:
            chapters = getattr(tiered_item.scored_item.item, "chapters", None) or []
            total_px += CHAPTER_HEIGHT_PX * len(chapters)
    return total_px


def group_items_by_tier(tiered_items: list[TieredItem]) -> dict[str, list[TieredItem]]:
    """Group tiered items by density tier, preserving descending rank order within each.

    The input is already rank-ordered, so iterating once and appending keeps each tier's
    items in their original rank order. All four tier keys are always present (possibly
    empty) so callers never key-error.

    Args:
        tiered_items: The :class:`TieredItem` list from ``assign_density_tiers``.

    Returns:
        A dict mapping each tier name to its items, in rank order.
    """
    grouped: dict[str, list[TieredItem]] = {
        TIER_HERO: [],
        TIER_STANDARD: [],
        TIER_COMPACT: [],
        TIER_INDEX: [],
    }
    for tiered_item in tiered_items:
        grouped.setdefault(tiered_item.density_tier, []).append(tiered_item)
    return grouped


# --- Small item helpers ------------------------------------------------------


def _card_deep_link(item: Any) -> str:
    """Build the whole-item deep-link: ``card_url``, else first chapter offset, else video start.

    Source-aware: an X item carries its ``https://x.com/{handle}/status/{tweet_id}``
    permalink in ``card_url``; a YouTube item leaves it empty. For a chaptered YouTube
    item the link lands on the FIRST chapter's ``deep_link`` (``watch?v=ID&t=Ns``) so a
    click drops the reader where the content starts; a chapterless item falls back to the
    whole-video ``watch?v=ID&t=0s``. All are trusted constructed URLs that the
    :mod:`lib.tiles` allowlist re-validates at render time.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        The item's ``card_url`` if set; else the first chapter's ``deep_link`` when the
        item has chapters; else ``https://www.youtube.com/watch?v={id}&t=0s``.
    """
    card_url = getattr(item, "card_url", "") or ""
    if card_url:
        return card_url
    chapters = getattr(item, "chapters", None) or []
    if chapters:
        first_chapter_link = getattr(chapters[0], "deep_link", "") or ""
        if first_chapter_link:
            return first_chapter_link
    item_external_id = getattr(item, "item_external_id", "") or ""
    return f"https://www.youtube.com/watch?v={item_external_id}&t=0s"


def _is_tweet(item: Any) -> bool:
    """True when the item is an X tweet (renders a tweet tile, not a feature/compact tile).

    Detected by its ``card_url`` x.com permalink (set only by
    :meth:`lib.rerank.RankableItem.from_tweet`). A YouTube item's ``card_url`` is empty.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        True if the item is an X tweet.
    """
    return "x.com/" in (getattr(item, "card_url", "") or "")


def _meta_label(item: Any) -> str:
    """Build the mono meta line for a feature/compact tile (``CHANNEL · PLATFORM``).

    Per the locked decisions, per-item duration is NOT captured upstream, so the meta
    line carries the (upper-cased) channel name + the platform only — never a fabricated
    runtime.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        The meta-label string.
    """
    channel_name = (getattr(item, "channel_name", "") or "").strip()
    label = channel_name.upper() if channel_name else "UNKNOWN"
    return f"{label} · YouTube"


def _placeholder_label(item: Any) -> str:
    """Build the ``.ph`` placeholder caption shown when a thumbnail could not be inlined.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        A ``"thumbnail · {channel}"`` caption (lower-cased channel, design style).
    """
    channel_name = (getattr(item, "channel_name", "") or "").strip().lower()
    return f"thumbnail · {channel_name}" if channel_name else "thumbnail"


def _inline(image_url: str, inline_image: InlineImage) -> str:
    """Resolve a remote image URL to a base64 ``data:`` URI via the seam, or ``""``.

    Empty ``image_url`` -> ``""`` (no fetch). A seam returning None (fetch failed /
    fail-soft) -> ``""`` so the tile builder falls back to its ``.ph`` placeholder
    (feature tiles) or omits the avatar (tweet tiles). NEVER a broken ``<img>``.

    Args:
        image_url: The remote source image URL (or empty).
        inline_image: The injectable fetch seam.

    Returns:
        A ``data:`` URI, or ``""``.
    """
    if not (image_url or "").strip():
        return ""
    return inline_image(image_url) or ""


def _chapter_rows(item: Any) -> tuple[list[tiles.ChapterRow], int]:
    """Build the visible chapter ``.kp`` rows + the "+ N more chapters" overflow count.

    Shows up to :data:`_MAX_VISIBLE_CHAPTERS` chapters as ``ChapterRow(chip, text)``;
    any beyond that become the overflow count for the more-chapters link (which points
    at the whole-item ``card_url`` — the deep-link surfacing in the Tiles layout).

    Args:
        item: A :class:`lib.rerank.RankableItem` (read for ``chapters``).

    Returns:
        ``(visible_rows, more_chapters_count)``.
    """
    chapters = getattr(item, "chapters", None) or []
    visible = chapters[:_MAX_VISIBLE_CHAPTERS]
    rows = [
        tiles.ChapterRow(
            chip=_format_timestamp(getattr(chapter, "start_seconds", 0.0)),
            text=getattr(chapter, "title", "") or "",
            # The chapter's own ``watch?v=ID&t=Ns`` deep-link makes the chip clickable
            # straight into the moment; tiles.py neutralizes an empty/unsafe url.
            url=getattr(chapter, "deep_link", "") or "",
        )
        for chapter in visible
    ]
    return rows, max(0, len(chapters) - len(visible))


def _cross_links_by_id(clusters: list[Any]) -> dict[str, list[tiles.CrossLink]]:
    """Map each cluster representative's item id -> its "same story, also covered" links.

    A cluster references other coverage of one topic through its ``cross_links`` (each a
    :class:`lib.fusion.CrossLink` into a long-form episode's chapter). Those are attached
    to the cluster's representative item's tile so the reader sees who else covered the
    story, with a deep-link into the relevant moment.

    Args:
        clusters: The :class:`lib.cluster.Cluster` list (may be empty).

    Returns:
        A ``item_external_id`` -> ``list[CrossLink]`` map (only representatives present).
    """
    cross_by_id: dict[str, list[tiles.CrossLink]] = {}
    for cluster in clusters or []:
        representative_id = str(getattr(cluster, "representative_item_id", "") or "")
        if not representative_id:
            continue
        built: list[tiles.CrossLink] = []
        for cross_link in getattr(cluster, "cross_links", []) or []:
            chapter_title = str(getattr(cross_link, "chapter_title", "") or "").strip()
            timestamp = _format_timestamp(float(getattr(cross_link, "chapter_start_seconds", 0.0) or 0.0))
            label = f"{chapter_title} · {timestamp}" if chapter_title else timestamp
            built.append(
                tiles.CrossLink(label=label, url=str(getattr(cross_link, "chapter_deep_link", "") or ""))
            )
        if built:
            cross_by_id.setdefault(representative_id, []).extend(built)
    return cross_by_id


def _items_by_id(tiered_items: list[TieredItem]) -> dict[str, Any]:
    """Index the tiered batch by ``item_external_id`` for the trio's deep-link resolution.

    Args:
        tiered_items: The tiered batch.

    Returns:
        A ``item_external_id`` -> :class:`lib.rerank.RankableItem` map.
    """
    by_id: dict[str, Any] = {}
    for tiered_item in tiered_items:
        item = tiered_item.scored_item.item
        item_external_id = str(getattr(item, "item_external_id", "") or "")
        if item_external_id:
            by_id[item_external_id] = item
    return by_id


def _count_distinct_creators(tiered_items: list[TieredItem]) -> int:
    """Count distinct creators across the batch (a masthead "sources" proxy).

    Args:
        tiered_items: The tiered batch.

    Returns:
        The number of distinct creators.
    """
    creator_keys: set[str] = set()
    for tiered_item in tiered_items:
        item = tiered_item.scored_item.item
        creator_key = (getattr(item, "creator_external_id", "") or "") or (getattr(item, "channel_name", "") or "")
        creator_keys.add(creator_key)
    return len(creator_keys)


def _masthead_counts(tiered_items: list[TieredItem], tracked_source_total: int) -> tuple[int, int, int]:
    """Compute the masthead coverage tallies (pure counting, Rule 5).

    These three numbers answer "is Orbit watching everything I follow?", which is the
    only claim the masthead makes:

      * ``tracked_total`` — every source Orbit watches, posted-today or not. This is NOT
        derivable from ``tiered_items`` (a channel that stayed quiet has no item), so it
        is passed in from the sources table by the caller.
      * ``posted_count`` — distinct creators appearing in today's digest.
      * ``item_count`` — items in the digest. Nothing is ever dropped, so this is the
        full batch size.

    A ``tracked_source_total`` of 0 (an uninformed caller) degrades to ``posted_count``
    rather than reporting a tracked total of zero alongside real items — an
    obviously-false claim. Shared with :mod:`lib.markdown_render` so the HTML and
    markdown mastheads report identical tallies (no drift).

    Args:
        tiered_items: The tiered batch.
        tracked_source_total: Total rows in the sources table (all platforms). 0 when
            the caller does not know, which degrades to ``posted_count``.

    Returns:
        ``(tracked_total, posted_count, item_count)``.
    """
    posted_count = _count_distinct_creators(tiered_items)
    item_count = len(tiered_items)
    tracked_total = tracked_source_total if tracked_source_total > 0 else posted_count
    return tracked_total, posted_count, item_count


def _format_dateline(reference_date: Optional[date] = None) -> str:
    """Format the masthead dateline (e.g. ``"MONDAY · 30 JUN 2026"``).

    The run date is the digest's "today"; per the locked decisions a per-item clock time
    is never available, but the masthead dateline is the run's own date (honest).

    Args:
        reference_date: The date to format (defaults to today, UTC). Injectable for tests.

    Returns:
        The upper-cased dateline string.
    """
    reference = reference_date or datetime.now(timezone.utc).date()
    return reference.strftime("%A · %-d %b %Y").upper()


# --- Per-tile builders -------------------------------------------------------


def _build_feature_tile(
    tiered_item: TieredItem,
    *,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    flag: str,
) -> tiles.TileBlock:
    """Build one Hero/Standard feature tile (thumbnail + summary + chapters + cross-links).

    Args:
        tiered_item: A Hero- or Standard-tier :class:`TieredItem`.
        summaries: The ``item_external_id`` -> blurb map (Hero/Standard only).
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        flag: An optional loud right flag (e.g. ``"▲ TOP SIGNAL"``), or ``""``.

    Returns:
        A feature ``.tile`` :class:`lib.tiles.TileBlock`.
    """
    item = tiered_item.scored_item.item
    item_external_id = str(getattr(item, "item_external_id", "") or "")
    rows, more_count = _chapter_rows(item)
    builder = tiles.render_hero_tile if tiered_item.density_tier == TIER_HERO else tiles.render_standard_tile
    return builder(
        image_url=_inline(getattr(item, "image_url", "") or "", inline_image),
        placeholder_label=_placeholder_label(item),
        meta_label=_meta_label(item),
        title=getattr(item, "title", "") or "",
        flag=flag,
        summary=summaries.get(item_external_id, ""),
        chapters=rows,
        more_chapters_count=more_count,
        cross_links=cross_links_by_id.get(item_external_id, []),
        card_url=_card_deep_link(item),
    )


def _build_compact_tile(item: Any) -> tiles.TileBlock:
    """Build one Compact/Index tile — thumbnail-less, one optional key-moment chip-link.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        A Compact ``.tile`` :class:`lib.tiles.TileBlock`.
    """
    chip_time = ""
    chip_label = ""
    chapters = getattr(item, "chapters", None) or []
    if chapters:
        first = chapters[0]
        chip_time = _format_timestamp(getattr(first, "start_seconds", 0.0))
        chip_label = getattr(first, "title", "") or ""
    # The whole-item deep-link is always the chip-link target AND (via the builder) the
    # title link, so a chapter-less compact tile is still clickable to its source.
    return tiles.render_compact_tile(
        meta_label=_meta_label(item),
        title=getattr(item, "title", "") or "",
        chip_time=chip_time,
        chip_label=chip_label,
        link_url=_card_deep_link(item),
    )


def _tweet_source_label(item: Any) -> str:
    """Build a tweet's ``@handle · X`` source label (shared with :mod:`lib.markdown_render`).

    An already-``@``-prefixed handle is not double-prefixed. Kept as one helper so the HTML
    tweet tile and the markdown tweet entry derive the label identically (no drift).

    Args:
        item: A :class:`lib.rerank.RankableItem` (an X tweet).

    Returns:
        The tweet source label (``"@handle · X"`` or ``"handle · X"``).
    """
    channel_name = (getattr(item, "channel_name", "") or "").strip()
    return f"@{channel_name} · X" if channel_name and not channel_name.startswith("@") else f"{channel_name} · X"


def _build_tweet_tile(item: Any, *, inline_image: InlineImage) -> tiles.TileBlock:
    """Build one tweet (X) tile — text-first, with the inlined unavatar profile pic.

    Args:
        item: A :class:`lib.rerank.RankableItem` (an X tweet — ``title`` is the body).
        inline_image: The injectable image-inline seam (avatar fetch).

    Returns:
        A tweet ``.tile`` :class:`lib.tiles.TileBlock`.
    """
    return tiles.render_tweet_tile(
        source_label=_tweet_source_label(item),
        text=getattr(item, "title", "") or "",
        link_url=_card_deep_link(item),
        avatar_url=_inline(getattr(item, "image_url", "") or "", inline_image),
    )


def _build_masonry_tiles(
    tiered_items: list[TieredItem],
    *,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    flag_top_signal: bool,
) -> list[tiles.TileBlock]:
    """Build the per-tile blocks for the masonry, one tile per item (rank order).

    Dispatches by item kind + density tier: an X item -> tweet tile; a Hero/Standard
    YouTube item -> feature tile; everything lower -> compact tile. The very first
    Hero item (when ``flag_top_signal``) gets the loud ``▲ TOP SIGNAL`` flag.

    Rank controls density, never inclusion — every input item yields exactly one tile.

    Args:
        tiered_items: The (possibly spill-split) tiered items in rank order.
        summaries: The ``item_external_id`` -> blurb map.
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        flag_top_signal: When True, the first Hero tile is flagged ``▲ TOP SIGNAL``.

    Returns:
        The tile blocks in rank order (``[]`` for an empty input). The masonry packs
        them into columns by their estimated heights — see
        :func:`lib.tiles.render_feed_masonry`.
    """
    parts: list[tiles.TileBlock] = []
    top_flag_used = False
    for tiered_item in tiered_items:
        item = tiered_item.scored_item.item
        if _is_tweet(item):
            parts.append(_build_tweet_tile(item, inline_image=inline_image))
            continue
        if tiered_item.density_tier in _FEATURE_TIERS:
            flag = ""
            if flag_top_signal and not top_flag_used and tiered_item.density_tier == TIER_HERO:
                flag = "▲ TOP SIGNAL"
                top_flag_used = True
            parts.append(
                _build_feature_tile(
                    tiered_item,
                    summaries=summaries,
                    cross_links_by_id=cross_links_by_id,
                    inline_image=inline_image,
                    flag=flag,
                )
            )
            continue
        parts.append(_build_compact_tile(item))
    return parts


# --- Page assembly -----------------------------------------------------------


def _wrap_body_container(inner_html: str) -> str:
    """Wrap a page body in the design's outer page container (background + max-width).

    Args:
        inner_html: The assembled masthead/masonry/footer markup.

    Returns:
        The body markup with the design's two outer wrapper divs.
    """
    return (
        '<div style="min-height:100vh;background:#EDE7DA;color:#1F1B16;'
        "font-family:'Newsreader',serif;padding:0 0 60px;\">"
        '<div style="max-width:1360px;margin:0 auto;padding:0 40px;">'
        f"{inner_html}</div></div>"
    )


def split_youtube_and_x(tiered_items: list[TieredItem]) -> tuple[list[TieredItem], list[TieredItem]]:
    """Partition a tiered batch into its YouTube half and its X half, preserving rank order.

    The page renders videos first and X posts below them, so the two runs are separated
    here rather than interleaved by rank. A single pass keeps each half in its original
    descending-rank order, and the two halves partition the input exactly — every item
    lands in one of them, so the never-drop invariant survives the split.

    Args:
        tiered_items: The rank-ordered tiered batch.

    Returns:
        ``(youtube_items, x_items)``.
    """
    youtube_items: list[TieredItem] = []
    x_items: list[TieredItem] = []
    for tiered_item in tiered_items:
        target = x_items if _is_tweet(tiered_item.scored_item.item) else youtube_items
        target.append(tiered_item)
    return youtube_items, x_items


def _build_feed_sections(
    page_items: list[TieredItem],
    *,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    flag_top_signal: bool,
) -> str:
    """Build the page's feed body: the YouTube masonry, then the X masonry below it.

    Each section is omitted entirely when its half is empty, so a YouTube-only day gets
    no dangling "From X" heading (and vice-versa).

    Args:
        page_items: The tiered items for this page, in rank order.
        summaries: The ``item_external_id`` -> blurb map.
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        flag_top_signal: When True, the first Hero video tile is flagged ``▲ TOP SIGNAL``.

    Returns:
        The concatenated section markup (``""`` when the page has no items).
    """
    youtube_items, x_items = split_youtube_and_x(page_items)
    sections: list[str] = []

    youtube_tiles = _build_masonry_tiles(
        youtube_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=flag_top_signal,
    )
    if youtube_tiles:
        sections.append(
            tiles.render_feed_masonry(
                youtube_tiles, heading=_YOUTUBE_SECTION_HEADING, note=_YOUTUBE_SECTION_NOTE
            )
        )

    x_tiles = _build_masonry_tiles(
        x_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=False,
    )
    if x_tiles:
        sections.append(tiles.render_feed_masonry(x_tiles, heading=_X_SECTION_HEADING))

    return "\n".join(sections)


def _build_page1_body(
    page_items: list[TieredItem],
    *,
    masthead_html: str,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    accounted_str: str,
    page_2_href: str,
) -> str:
    """Assemble page 1's body: masthead -> YouTube masonry -> X masonry -> footer.

    Args:
        page_items: The tiered items rendered on page 1 (all items, or Hero+Standard on spill).
        masthead_html: The pre-rendered masthead.
        summaries: The ``item_external_id`` -> blurb map.
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        accounted_str: The footer coverage line.
        page_2_href: The footer page-2 link href (``""`` -> no link, single-page digest).

    Returns:
        The page-1 body markup (absent sections omitted).
    """
    feed_html = _build_feed_sections(
        page_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=True,
    )
    # Page 1 carries the chat-bridge entry point (issue #7) — the SAME encoded link
    # as the email body, built by lib.chat_bridge (never hand-concatenated, spike #5 AC4).
    footer_html = tiles.render_footer(accounted_str, page_2_href, chat_href=chat_bridge.build_chat_link())
    inner = "\n".join(part for part in (masthead_html, feed_html, footer_html) if part)
    return _wrap_body_container(inner)


def _build_page2_body(
    page_items: list[TieredItem],
    *,
    masthead_html: str,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    accounted_str: str,
) -> str:
    """Assemble page 2 (the archive): masthead -> low-tier masonry -> footer (no page-3 link).

    Args:
        page_items: The spilled Compact + Index tiered items.
        masthead_html: The pre-rendered masthead (repeated so page 2 stands alone).
        summaries: The ``item_external_id`` -> blurb map.
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        accounted_str: The footer coverage line.

    Returns:
        The page-2 body markup.
    """
    feed_html = _build_feed_sections(
        page_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=False,
    )
    footer_html = tiles.render_footer(accounted_str, "")
    inner = "\n".join(part for part in (masthead_html, feed_html, footer_html) if part)
    return _wrap_body_container(inner)


def render_digest_pages(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    page_2_href: str = DEFAULT_PAGE_2_FILENAME,
    clusters: list[Any] | None = None,
    tracked_source_total: int = 0,
    summaries: dict[str, str] | None = None,
    inline_image: InlineImage = images.fetch_and_inline,
    reference_date: Optional[date] = None,
) -> list[str]:
    """Render the digest into one or two self-contained Tiles HTML pages.

    Assembles the layout — masthead, the YouTube tile masonry, the X masonry below it,
    and the footer — from the tiered items + the M3 ``clusters`` (cross-links) + the
    pre-computed ``summaries``. Thumbnails/avatars are base64-inlined at render time via
    the injectable ``inline_image`` seam (tests stub it — no network).

    When :func:`estimate_page_height` fits :data:`PAGE_1_BUDGET_PX`, returns
    ``[page1]`` (single page, footer carries no page-2 link). When it overflows, the
    Compact + Index tiers spill to a second page and this returns ``[page1, page2]``:
    Hero + Standard STAY on page 1 (whose footer links to ``page_2_href``); Compact +
    Index render on page 2. HARD CAP of 2 pages (Orbit never produces a page 3).

    Rank controls density, never inclusion: every item appears as exactly one tile.

    Args:
        tiered_items: The :class:`TieredItem` list (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` — read for ``digest_title``.
        page_2_href: The footer href page 1 links to for the spilled content.
        clusters: OPTIONAL :class:`lib.cluster.Cluster` list (the tile cross-links).
        tracked_source_total: Total sources Orbit watches, for the masthead coverage
            count. NOT derivable from ``tiered_items`` (a quiet channel has no item), so
            the caller reads it from the sources table; 0 degrades to the posted count.
        summaries: The pre-computed ``item_external_id`` -> blurb map (None -> ``{}``).
        inline_image: The injectable image-inline seam (defaults to the real fetch).
        reference_date: The masthead dateline date (defaults to today; injectable).

    Returns:
        ``[page1_html]`` (fits) or ``[page1_html, page2_html]`` (spilled).
    """
    clusters = clusters or []
    summaries = summaries or {}

    page_title = getattr(config, "digest_title", None) or DEFAULT_DIGEST_TITLE
    grouped = group_items_by_tier(tiered_items)
    cross_links_by_id = _cross_links_by_id(clusters)

    # Masthead coverage counts (pure counting, Rule 5): how many sources are watched,
    # how many of them posted today, and how many items that produced.
    tracked_total, posted_count, item_count = _masthead_counts(tiered_items, tracked_source_total)
    accounted_str = f"{item_count} ITEMS FROM {posted_count} OF {tracked_total} TRACKED CHANNELS"

    masthead_html = tiles.render_masthead(
        _format_dateline(reference_date), tracked_total, posted_count, item_count
    )

    estimated_height_px = estimate_page_height(tiered_items)
    spilled = estimated_height_px > PAGE_1_BUDGET_PX

    if not spilled:
        page1_body = _build_page1_body(
            tiered_items,
            masthead_html=masthead_html,
            summaries=summaries,
            cross_links_by_id=cross_links_by_id,
            inline_image=inline_image,
            accounted_str=accounted_str,
            page_2_href="",
        )
        pages = [wrap_page(page_title, page1_body)]
    else:
        page1_items = grouped[TIER_HERO] + grouped[TIER_STANDARD]
        page2_items = grouped[TIER_COMPACT] + grouped[TIER_INDEX]
        page1_body = _build_page1_body(
            page1_items,
            masthead_html=masthead_html,
            summaries=summaries,
            cross_links_by_id=cross_links_by_id,
            inline_image=inline_image,
            accounted_str=accounted_str,
            page_2_href=page_2_href,
        )
        page2_body = _build_page2_body(
            page2_items,
            masthead_html=masthead_html,
            summaries=summaries,
            cross_links_by_id=cross_links_by_id,
            inline_image=inline_image,
            accounted_str=accounted_str,
        )
        pages = [wrap_page(page_title, page1_body), wrap_page(page_title, page2_body)]

    log.log_info(
        "render_completed",
        item_count=len(tiered_items),
        hero_count=len(grouped[TIER_HERO]),
        standard_count=len(grouped[TIER_STANDARD]),
        compact_count=len(grouped[TIER_COMPACT]),
        index_count=len(grouped[TIER_INDEX]),
        estimated_height_px=estimated_height_px,
        page_count=len(pages),
        spilled=spilled,
        tracked_source_total=tracked_total,
        posted_source_count=posted_count,
        summary_count=len(summaries),
    )
    return pages


def render_digest_html(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    clusters: list[Any] | None = None,
    tracked_source_total: int = 0,
    summaries: dict[str, str] | None = None,
    inline_image: InlineImage = images.fetch_and_inline,
    reference_date: Optional[date] = None,
) -> str:
    """Render the tiered items into ONE self-contained Tiles HTML page (page 1).

    Backwards-compatible single-string entry point: returns PAGE 1 of
    :func:`render_digest_pages`. When the digest fits, that is the whole digest; when it
    overflows, page 1 carries the footer "Full archive · page 2 →" link (the caller that
    needs the page-2 file uses :func:`render_digest_pages`, which orbit.py wires).

    Args:
        tiered_items: The :class:`TieredItem` list (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` (``digest_title``).
        clusters: OPTIONAL clusters (the tile cross-links).
        tracked_source_total: Total sources watched, for the masthead coverage count.
        summaries: The pre-computed ``item_external_id`` -> blurb map.
        inline_image: The injectable image-inline seam (defaults to the real fetch).
        reference_date: The masthead dateline date (defaults to today; injectable).

    Returns:
        The complete ``<!DOCTYPE html>...`` page-1 string of the digest.
    """
    return render_digest_pages(
        tiered_items,
        config,
        clusters=clusters,
        tracked_source_total=tracked_source_total,
        summaries=summaries,
        inline_image=inline_image,
        reference_date=reference_date,
    )[0]
