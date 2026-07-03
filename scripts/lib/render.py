"""Tiles-layout render orchestration for the Orbit digest (Phase 7 / Sub-phase 4).

This is the orchestration layer ABOVE :mod:`lib.tiles`: it consumes the
:class:`lib.density.TieredItem` list (already rank-ordered, already tiered) plus the
M3 ``clusters`` / ``trending_items`` / ``scoops`` and the LLM ``verdict`` / per-item
``summaries``, and assembles ONE self-contained newspaper "Tiles" HTML digest
(``out/orbit-tiles-reference.html`` is the visual target). :mod:`lib.tiles` owns all
the markup/CSS primitives and the XSS-safe link/image helpers; this module decides
WHICH items go where and in what order.

Layout (top to bottom, matching the design):
  1. **masthead** — the "Orbit" wordmark + the day's counts (sources / accounted /
     scoops / dormant / clusters), pure counting (Rule 5).
  2. **verdict** — the ONE LLM editorial sentence (passed in; empty -> omitted).
  3. **"Ahead of the curve" trio** — the top scoop tile, the "Trending now" rows, and
     the top-velocity "Hidden gem". All-empty (a quiet day) -> the section is omitted.
  4. **"From your feed · ranked" masonry** — a 3-column tile grid, one tile per item,
     by density tier: Hero (biggest) / Standard / Compact, plus tweet tiles for X
     items. Thumbnails are base64-inlined at render time via an INJECTABLE
     ``inline_image`` seam (so tests stub it — no network).
  5. **footer** — the accounted-for line + an optional "Full archive · page 2 →" link.

THE CORE INVARIANT (api-contracts derank contract): rank controls DENSITY, never
INCLUSION. Every tiered item appears SOMEWHERE on the page — a Hero big tile down to a
Compact/tweet tile — nothing is dropped. The 2-page spill (low tiers to page 2) is
preserved: Hero + Standard stay on page 1 (which gains the footer page-2 link);
Compact + Index spill to page 2.

Rule 5: there is NO LLM here — rendering is pure deterministic string building. The
verdict + summaries are computed UPSTREAM (orbit.py) and passed in; image inlining
goes through the injectable ``inline_image`` seam.
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

from lib import images, log, tiles  # noqa: E402  (import must follow the sys.path inserts above)
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
# Fixed chrome (masthead + verdict + trio + margins) added once per page estimate.
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
    """Build the whole-item deep-link: an item-supplied ``card_url`` if present, else YouTube.

    Source-aware: an X item carries its ``https://x.com/{handle}/status/{tweet_id}``
    permalink in ``card_url``; a YouTube item leaves it empty and falls back to the
    whole-video ``watch?v=ID&t=0s`` deep-link. Both are trusted constructed ``https``
    URLs that pass the :mod:`lib.tiles` allowlist.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        The item's ``card_url`` if set, else ``https://www.youtube.com/watch?v={id}&t=0s``.
    """
    card_url = getattr(item, "card_url", "") or ""
    if card_url:
        return card_url
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


def _trending_deep_link(trending_item: Any, items_by_id: dict[str, Any]) -> str:
    """Resolve a trending/scoop entry's deep-link (reuse the card link, source-aware).

    Prefers the resolved item's :func:`_card_deep_link`; falls back to the entry's own
    ``card_url``, then a YouTube ``watch?v=ID&t=0s``, then ``"#"`` (never invented).

    Args:
        trending_item: A :class:`lib.trending.TrendingItem`.
        items_by_id: ``item_external_id`` -> :class:`lib.rerank.RankableItem` map.

    Returns:
        The deep-link URL string.
    """
    item_external_id = str(getattr(trending_item, "item_external_id", "") or "")
    resolved = items_by_id.get(item_external_id)
    if resolved is not None:
        return _card_deep_link(resolved)
    card_url = str(getattr(trending_item, "card_url", "") or "")
    if card_url:
        return card_url
    if item_external_id:
        return f"https://www.youtube.com/watch?v={item_external_id}&t=0s"
    return "#"


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
) -> str:
    """Build one Hero/Standard feature tile (thumbnail + summary + chapters + cross-links).

    Args:
        tiered_item: A Hero- or Standard-tier :class:`TieredItem`.
        summaries: The ``item_external_id`` -> blurb map (Hero/Standard only).
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        flag: An optional loud right flag (e.g. ``"▲ TOP SIGNAL"``), or ``""``.

    Returns:
        A feature ``.tile`` block.
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


def _build_compact_tile(item: Any) -> str:
    """Build one Compact/Index tile — thumbnail-less, one optional key-moment chip-link.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        A Compact ``.tile`` block.
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


def _build_tweet_tile(item: Any, *, inline_image: InlineImage) -> str:
    """Build one tweet (X) tile — text-first, with the inlined unavatar profile pic.

    Args:
        item: A :class:`lib.rerank.RankableItem` (an X tweet — ``title`` is the body).
        inline_image: The injectable image-inline seam (avatar fetch).

    Returns:
        A tweet ``.tile`` block.
    """
    channel_name = (getattr(item, "channel_name", "") or "").strip()
    source_label = f"@{channel_name} · X" if channel_name and not channel_name.startswith("@") else f"{channel_name} · X"
    return tiles.render_tweet_tile(
        source_label=source_label,
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
) -> str:
    """Build the concatenated per-tile HTML for the masonry, one tile per item (rank order).

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
        The concatenated tile HTML (``""`` for an empty input).
    """
    parts: list[str] = []
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
    return "".join(parts)


# --- "Ahead of the curve" trio -----------------------------------------------


def _trending_row_category(trending_item: Any) -> tuple[str, int]:
    """Map a trending item to its "Trending now" marker category + your-count.

    A scoop (dormant break) -> ``◆`` dormant; a multi-creator convergence -> ``↗`` "N of
    yours"; an externally-corroborated/other topic -> ``○`` external.

    Args:
        trending_item: A :class:`lib.trending.TrendingItem`.

    Returns:
        ``(category, your_count)``.
    """
    if bool(getattr(trending_item, "is_scoop", False)):
        return tiles.CATEGORY_DORMANT, 0
    convergence = int(getattr(trending_item, "convergence_count", 0) or 0)
    if convergence >= 2:
        return tiles.CATEGORY_YOURS, convergence
    return tiles.CATEGORY_EXTERNAL, 0


def _build_ahead_trio(
    scoops: list[Any],
    trending_items: list[Any],
    items_by_id: dict[str, Any],
    summaries: dict[str, str],
) -> str:
    """Assemble the "Ahead of the curve" trio (top scoop / trending rows / hidden gem).

    Each sub-tile degrades to ``""`` when its source data is absent, and the whole
    section is omitted when all three are empty (a quiet day, the M1/M2 path). No LLM
    here — the prose is the pre-computed ``summaries``/verdict only.

    Args:
        scoops: The :class:`lib.trending.TrendingItem` scoops (loudest first).
        trending_items: The velocity-ranked :class:`lib.trending.TrendingItem` list.
        items_by_id: ``item_external_id`` -> :class:`lib.rerank.RankableItem` map.
        summaries: The ``item_external_id`` -> blurb map.

    Returns:
        The trio section HTML, or ``""`` when there is nothing ahead of the curve.
    """
    scoop_tile = ""
    if scoops:
        top_scoop = scoops[0]
        scoop_id = str(getattr(top_scoop, "item_external_id", "") or "")
        resolved = items_by_id.get(scoop_id)
        attribution = (
            (getattr(resolved, "channel_name", "") or "")
            if resolved is not None
            else (getattr(top_scoop, "creator_external_id", "") or "")
        ) or "your network"
        scoop_tile = tiles.render_scoop_tile(
            attribution,
            getattr(top_scoop, "title", "") or "",
            summaries.get(scoop_id, ""),
            _trending_deep_link(top_scoop, items_by_id),
        )

    trending_tile = ""
    if trending_items:
        rows: list[tiles.TrendingRow] = []
        for trending_item in trending_items:
            category, your_count = _trending_row_category(trending_item)
            rows.append(
                tiles.TrendingRow(
                    title=getattr(trending_item, "title", "") or "",
                    category=category,
                    your_count=your_count,
                    link_url=_trending_deep_link(trending_item, items_by_id),
                )
            )
        trending_tile = tiles.render_trending_now(rows)

    gem_tile = ""
    if trending_items:
        gem = trending_items[0]
        gem_id = str(getattr(gem, "item_external_id", "") or "")
        ratio = float(getattr(gem, "baseline_relative_ratio", 0.0) or 0.0)
        gem_tile = tiles.render_hidden_gem(
            (getattr(gem, "creator_external_id", "") or getattr(gem, "title", "") or "").upper(),
            max(0, int(round(ratio * 100))),
            getattr(gem, "title", "") or "",
            summaries.get(gem_id, ""),
        )

    return tiles.render_ahead_trio(scoop_tile, trending_tile, gem_tile)


# --- Page assembly -----------------------------------------------------------


def _wrap_body_container(inner_html: str) -> str:
    """Wrap a page body in the design's outer page container (background + max-width).

    Args:
        inner_html: The assembled masthead/verdict/trio/masonry/footer markup.

    Returns:
        The body markup with the design's two outer wrapper divs.
    """
    return (
        '<div style="min-height:100vh;background:#EDE7DA;color:#1F1B16;'
        "font-family:'Newsreader',serif;padding:0 0 60px;\">"
        '<div style="max-width:1360px;margin:0 auto;padding:0 40px;">'
        f"{inner_html}</div></div>"
    )


def _build_page1_body(
    page_items: list[TieredItem],
    *,
    masthead_html: str,
    verdict_html: str,
    trio_html: str,
    summaries: dict[str, str],
    cross_links_by_id: dict[str, list[tiles.CrossLink]],
    inline_image: InlineImage,
    accounted_str: str,
    page_2_href: str,
) -> str:
    """Assemble page 1's body: masthead -> verdict -> trio -> masonry -> footer.

    Args:
        page_items: The tiered items rendered on page 1 (all items, or Hero+Standard on spill).
        masthead_html: The pre-rendered masthead.
        verdict_html: The pre-rendered verdict (``""`` when absent).
        trio_html: The pre-rendered "Ahead of the curve" trio (``""`` when absent).
        summaries: The ``item_external_id`` -> blurb map.
        cross_links_by_id: The representative-id -> cross-links map.
        inline_image: The injectable image-inline seam.
        accounted_str: The footer accounted-for line.
        page_2_href: The footer page-2 link href (``""`` -> no link, single-page digest).

    Returns:
        The page-1 body markup (absent sections omitted).
    """
    masonry_tiles = _build_masonry_tiles(
        page_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=True,
    )
    masonry_html = tiles.render_feed_masonry(masonry_tiles) if masonry_tiles else ""
    footer_html = tiles.render_footer(accounted_str, page_2_href)
    inner = "\n".join(
        part for part in (masthead_html, verdict_html, trio_html, masonry_html, footer_html) if part
    )
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
        accounted_str: The footer accounted-for line.

    Returns:
        The page-2 body markup.
    """
    masonry_tiles = _build_masonry_tiles(
        page_items,
        summaries=summaries,
        cross_links_by_id=cross_links_by_id,
        inline_image=inline_image,
        flag_top_signal=False,
    )
    masonry_html = tiles.render_feed_masonry(masonry_tiles) if masonry_tiles else ""
    footer_html = tiles.render_footer(accounted_str, "")
    inner = "\n".join(part for part in (masthead_html, masonry_html, footer_html) if part)
    return _wrap_body_container(inner)


def render_digest_pages(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    page_2_href: str = DEFAULT_PAGE_2_FILENAME,
    clusters: list[Any] | None = None,
    trending_items: list[Any] | None = None,
    scoops: list[Any] | None = None,
    verdict: str = "",
    summaries: dict[str, str] | None = None,
    inline_image: InlineImage = images.fetch_and_inline,
    reference_date: Optional[date] = None,
) -> list[str]:
    """Render the digest into one or two self-contained Tiles HTML pages.

    Assembles the newspaper layout — masthead, the LLM verdict, the "Ahead of the curve"
    trio, the "From your feed · ranked" tile masonry, and the footer — from the tiered
    items + the M3 ``clusters`` / ``trending_items`` / ``scoops`` + the pre-computed
    ``verdict`` / ``summaries``. Thumbnails/avatars are base64-inlined at render time via
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
        clusters: OPTIONAL :class:`lib.cluster.Cluster` list (cross-links + the count).
        trending_items: OPTIONAL :class:`lib.trending.TrendingItem` list (the trio).
        scoops: OPTIONAL detected :class:`lib.trending.TrendingItem` scoops (the trio).
        verdict: The pre-computed LLM verdict sentence (``""`` -> omitted).
        summaries: The pre-computed ``item_external_id`` -> blurb map (None -> ``{}``).
        inline_image: The injectable image-inline seam (defaults to the real fetch).
        reference_date: The masthead dateline date (defaults to today; injectable).

    Returns:
        ``[page1_html]`` (fits) or ``[page1_html, page2_html]`` (spilled).
    """
    clusters = clusters or []
    trending_items = trending_items or []
    scoops = scoops or []
    summaries = summaries or {}

    page_title = getattr(config, "digest_title", None) or DEFAULT_DIGEST_TITLE
    grouped = group_items_by_tier(tiered_items)
    items_by_id = _items_by_id(tiered_items)
    cross_links_by_id = _cross_links_by_id(clusters)

    # Masthead counts (pure counting, Rule 5). Nothing is dropped, so every item is
    # "accounted for"; "sources" is the distinct-creator count (the feeds scanned).
    source_total = _count_distinct_creators(tiered_items)
    accounted = len(tiered_items)
    scoop_count = len(scoops)
    dormant_count = sum(1 for scoop in scoops if bool(getattr(scoop, "is_scoop", False))) or scoop_count
    cluster_count = len(clusters)
    accounted_str = f"{accounted} OF {source_total} SOURCES ACCOUNTED FOR"

    masthead_html = tiles.render_masthead(
        _format_dateline(reference_date),
        source_total,
        accounted,
        scoop_count,
        dormant_count,
        cluster_count,
    )
    verdict_html = tiles.render_verdict(verdict)
    trio_html = _build_ahead_trio(scoops, trending_items, items_by_id, summaries)

    estimated_height_px = estimate_page_height(tiered_items)
    spilled = estimated_height_px > PAGE_1_BUDGET_PX

    if not spilled:
        page1_body = _build_page1_body(
            tiered_items,
            masthead_html=masthead_html,
            verdict_html=verdict_html,
            trio_html=trio_html,
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
            verdict_html=verdict_html,
            trio_html=trio_html,
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
        has_verdict=bool(verdict.strip()),
        summary_count=len(summaries),
    )
    return pages


def render_digest_html(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    clusters: list[Any] | None = None,
    trending_items: list[Any] | None = None,
    scoops: list[Any] | None = None,
    verdict: str = "",
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
        clusters: OPTIONAL clusters (cross-links + the masthead count).
        trending_items: OPTIONAL trending list (the trio).
        scoops: OPTIONAL detected scoops (the trio).
        verdict: The pre-computed LLM verdict sentence (``""`` -> omitted).
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
        trending_items=trending_items,
        scoops=scoops,
        verdict=verdict,
        summaries=summaries,
        inline_image=inline_image,
        reference_date=reference_date,
    )[0]
