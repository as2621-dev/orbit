"""HTML one-pager orchestration for the Orbit digest (Phase 3 / Stage 7a).

This is the orchestration layer ABOVE :mod:`lib.html_render`: it consumes the
:class:`lib.density.TieredItem` list (already rank-ordered, already tiered) and
assembles ONE self-contained HTML digest per the design brief
(``references/design-brief.md``). :mod:`lib.html_render` owns all the markup/CSS
primitives and the XSS-safe link helpers; this module decides WHICH items go where
and in what order.

Layout (design brief §3, top to bottom):
  1. one-line **TL;DR** header — ``N episodes from M creators today`` (pure
     counting, Rule 5 — no LLM).
  2. **scoops strip** — M3 placeholder, renders absent in M1 (never fabricated).
  3. **creator episode cards** — Hero/Standard get full cards WITH deep-link
     chapter lists; Compact gets condensed rows.
  4. **right-rail trending** — M3 placeholder, renders absent in M1.
  5. bottom **"they also posted" Index strip** — Index-tier items as compact lines.

Every card and every chapter carries a working ``watch?v=ID&t=Ns`` deep-link
(chapters use ``chapter.deep_link`` verbatim — the headline feature); the card
itself links to the whole video at ``t=0s``.

THE CORE INVARIANT (api-contracts derank contract): rank controls density, NEVER
inclusion. Every tiered item appears SOMEWHERE on the page — Hero as a big card,
Index as a one-line "they also posted" entry — but nothing is dropped.

Rule 5: there is NO LLM here — rendering is pure deterministic string building.

Extensibility note for Sub-phase 4 (page-2 spill / Stage 7b): the per-tier grouping
is exposed via :func:`group_items_by_tier`, and body assembly is split into
``_render_main_cards_section`` (Hero+Standard+Compact) and
``_render_index_section`` (Index). Sub-phase 4 adds ``estimate_page_height`` and a
paginating wrapper that, when the height budget is crossed, routes the Compact+Index
groups to a second page — reusing these exact section builders without rewriting
:func:`render_digest_html`. See the execution report for the precise seam.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.render`` (via orbit.py's sys.path insert of the scripts dir) or run from
# the scripts dir directly. Mirrors rerank.py / density.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import html_render, log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.density import TIER_COMPACT, TIER_HERO, TIER_INDEX, TIER_STANDARD, TieredItem  # noqa: E402

# Default page <title> when the config carries no override.
DEFAULT_DIGEST_TITLE: str = "Orbit · Today"

# Tiers whose cards carry a full deep-link chapter list (the headline feature).
_FULL_CARD_TIERS: frozenset[str] = frozenset({TIER_HERO, TIER_STANDARD})

# --- Page-budget height heuristic (Sub-phase 4 / Stage 7b) ------------------
# FIRST-CUT, TUNABLE estimate-by-content table — NOT a measured layout. Orbit
# deliberately avoids a headless-browser measurement pass (stdlib-first, no extra
# dependency), so page height is APPROXIMATED by summing a per-tier estimated px
# cost across the items plus a per-chapter increment for the chapter lists that
# Hero/Standard cards carry. These constants mirror the CSS density ladder in
# html_render.CSS (hero card is the tallest, an index line the shortest) and are
# expected to be tuned by the maintainer's real-day usage — the master-plan
# riskiest-assumption test (does the one-pager actually fit one screen?).
#
# px cost per item, by density tier:
TIER_HEIGHT_PX: dict[str, int] = {
    TIER_HERO: 220,  # big card: large title + meta + chapter-list header
    TIER_STANDARD: 150,  # medium card: title + meta + chapter-list header
    TIER_COMPACT: 44,  # condensed single row
    TIER_INDEX: 30,  # one "they also posted" line
}
# Additional px per chapter <li> on a Hero/Standard card (chapter lists add height).
CHAPTER_HEIGHT_PX: int = 26
# Fixed chrome (TL;DR header + page margins) added once per page estimate.
PAGE_CHROME_PX: int = 120
# Page-1 height budget. When estimate_page_height(...) exceeds this, Compact+Index
# spill to page 2. First-cut: ~one tall screen / A4 page. Tunable.
PAGE_1_BUDGET_PX: int = 1400

# The page-2 filename Orbit writes beside page 1 (orbit.py writes the files; this
# is the default href page 1 links to). The brief names it ``today-page2.html``.
DEFAULT_PAGE_2_FILENAME: str = "today-page2.html"


def estimate_page_height(tiered_items: list[TieredItem]) -> int:
    """Estimate a digest's rendered height in px (FIRST-CUT content heuristic).

    Sums :data:`TIER_HEIGHT_PX` per item by its density tier, plus
    :data:`CHAPTER_HEIGHT_PX` for every chapter on a Hero/Standard card, plus the
    fixed :data:`PAGE_CHROME_PX` once. This is a deliberate ESTIMATE-BY-CONTENT, not
    a measured layout (no headless browser — stdlib-first), so the value is a tunable
    approximation used only to decide whether page 1 overflows its budget. Pure
    function, no I/O (Rule 5 — deterministic).

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
        if tiered_item.density_tier in _FULL_CARD_TIERS:
            chapters = getattr(tiered_item.scored_item.item, "chapters", None) or []
            total_px += CHAPTER_HEIGHT_PX * len(chapters)
    return total_px


def _render_continued_link(page_2_href: str) -> str:
    """Render the "continued on page 2 ->" link placed at the bottom of page 1.

    Built via :func:`html_render.render_link` so the href is allowlist-checked +
    escaped like every other link. ``page_2_href`` is a trusted constructed filename
    (e.g. ``today-page2.html``), so it passes the allowlist intact.

    Args:
        page_2_href: The href page 1 links to for the spilled content.

    Returns:
        The continued-link section markup.
    """
    link = html_render.render_link(page_2_href, "Continued on page 2 →", css_class="page-link")
    return f'<nav class="page-nav page-nav-next">{link}</nav>'


def _card_deep_link(item: Any) -> str:
    """Build the card-title link: an item-supplied ``card_url`` if present, else YouTube.

    Source-aware (Phase 4 / M2): when the item carries a non-empty ``card_url`` (an X
    item sets its ``https://x.com/{handle}/status/{tweet_id}`` permalink), that link is
    used verbatim so the unified digest renders a real x.com card. When ``card_url`` is
    empty — every YouTube item — it falls back to the whole-video
    ``watch?v=ID&t=0s`` deep-link, so the YouTube path is byte-for-byte unchanged. Both
    forms are trusted constructed ``https`` URLs that pass the html_render allowlist.

    Args:
        item: A :class:`lib.rerank.RankableItem` (read for ``card_url`` then
            ``item_external_id``).

    Returns:
        The item's ``card_url`` if set, else ``https://www.youtube.com/watch?v={id}&t=0s``.
    """
    card_url = getattr(item, "card_url", "") or ""
    if card_url:
        return card_url
    item_external_id = getattr(item, "item_external_id", "") or ""
    return f"https://www.youtube.com/watch?v={item_external_id}&t=0s"


def group_items_by_tier(tiered_items: list[TieredItem]) -> dict[str, list[TieredItem]]:
    """Group tiered items by density tier, preserving descending rank order within each.

    The input is already rank-ordered (``derank_items`` -> ``assign_density_tiers``
    preserve order), so iterating once and appending keeps each tier's items in their
    original rank order. Every one of the four tier keys is always present (possibly
    with an empty list) so callers never key-error.

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


def _render_full_card(tiered_item: TieredItem) -> str:
    """Render one Hero/Standard full card (with its deep-link chapter list).

    Args:
        tiered_item: A Hero- or Standard-tier :class:`TieredItem`.

    Returns:
        The card markup string.
    """
    item = tiered_item.scored_item.item
    return html_render.render_card(
        item,
        _card_deep_link(item),
        tiered_item.density_tier,
        with_chapters=True,
    )


def _render_main_cards_section(grouped: dict[str, list[TieredItem]]) -> str:
    """Render the main column: Hero + Standard full cards, then Compact rows.

    This is one of the two section builders Sub-phase 4 reuses for pagination (the
    Compact group is the lowest of the three here, so it is the natural spill
    boundary alongside Index). Returns ``""`` when all three tiers are empty so the
    section is absent.

    Args:
        grouped: The output of :func:`group_items_by_tier`.

    Returns:
        The main-cards section markup, or ``""`` if there are no Hero/Standard/Compact items.
    """
    hero_and_standard_html = "".join(
        _render_full_card(tiered_item) for tiered_item in grouped[TIER_HERO] + grouped[TIER_STANDARD]
    )
    compact_html = "".join(
        html_render.render_compact_row(tiered_item.scored_item.item, _card_deep_link(tiered_item.scored_item.item))
        for tiered_item in grouped[TIER_COMPACT]
    )
    if not hero_and_standard_html and not compact_html:
        return ""
    return f'<section class="cards">{hero_and_standard_html}{compact_html}</section>'


def _render_index_section(grouped: dict[str, list[TieredItem]]) -> str:
    """Render the bottom "they also posted" Index strip.

    The other section builder Sub-phase 4 reuses (Index spills to page 2 with
    Compact). Returns ``""`` when there are no Index items (section absent).

    Args:
        grouped: The output of :func:`group_items_by_tier`.

    Returns:
        The index-strip markup, or ``""`` if there are no Index items.
    """
    index_lines = [
        html_render.render_index_line(tiered_item.scored_item.item, _card_deep_link(tiered_item.scored_item.item))
        for tiered_item in grouped[TIER_INDEX]
    ]
    return html_render.render_index_strip(index_lines)


def _count_distinct_creators(tiered_items: list[TieredItem]) -> int:
    """Count distinct creators across the batch (for the TL;DR header).

    Uses ``creator_external_id`` when present, falling back to ``channel_name`` so
    an item with no external id still counts toward a distinct creator rather than
    collapsing every blank-id item into one. Pure counting (Rule 5).

    Args:
        tiered_items: The full tiered batch.

    Returns:
        The number of distinct creators.
    """
    creator_keys: set[str] = set()
    for tiered_item in tiered_items:
        item = tiered_item.scored_item.item
        creator_key = (getattr(item, "creator_external_id", "") or "") or (getattr(item, "channel_name", "") or "")
        creator_keys.add(creator_key)
    return len(creator_keys)


def _split_grouped_for_spill(
    grouped: dict[str, list[TieredItem]],
) -> tuple[dict[str, list[TieredItem]], dict[str, list[TieredItem]]]:
    """Split the tier groups into a page-1 and a page-2 group dict (spill-the-low-tiers).

    Page 1 keeps Hero + Standard (the cards worth a full screen); Compact + Index
    spill to page 2. This is NOT an arbitrary mid-list cut — it spills by tier, so a
    Hero never lands on page 2. Each side is a complete four-key ``grouped`` dict (the
    section builders key all four tiers) with the moved tiers emptied on the side they
    left.

    Args:
        grouped: The output of :func:`group_items_by_tier`.

    Returns:
        ``(page1_grouped, page2_grouped)``.
    """
    page1_grouped: dict[str, list[TieredItem]] = {
        TIER_HERO: grouped[TIER_HERO],
        TIER_STANDARD: grouped[TIER_STANDARD],
        TIER_COMPACT: [],
        TIER_INDEX: [],
    }
    page2_grouped: dict[str, list[TieredItem]] = {
        TIER_HERO: [],
        TIER_STANDARD: [],
        TIER_COMPACT: grouped[TIER_COMPACT],
        TIER_INDEX: grouped[TIER_INDEX],
    }
    return page1_grouped, page2_grouped


def _trending_deep_link(trending_item: Any, items_by_id: dict[str, Any]) -> str:
    """Resolve the deep-link for a trending/scoop entry (source-aware, reuse the card link).

    Prefers the rankable item resolved by ``item_external_id`` (so an X item links to its
    x.com card and a YouTube item to its ``watch?v=ID&t=0s`` fallback via
    :func:`_card_deep_link` — the SAME deep-link logic the cards use). Falls back to the
    trending entry's own ``card_url`` (the right-rail hook), then to a YouTube
    ``watch?v=ID&t=0s`` built from the id. Empty id and no card_url -> ``"#"`` (the
    renderer never invents a link).

    Args:
        trending_item: A :class:`lib.trending.TrendingItem` (``item_external_id`` /
            ``card_url``).
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem` so
            the trending entry can reuse the card's exact deep-link.

    Returns:
        The deep-link URL string (allowlist-checked + escaped at render time).
    """
    item_external_id = str(getattr(trending_item, "item_external_id", "") or "")
    resolved_item = items_by_id.get(item_external_id)
    if resolved_item is not None:
        return _card_deep_link(resolved_item)
    card_url = str(getattr(trending_item, "card_url", "") or "")
    if card_url:
        return card_url
    if item_external_id:
        return f"https://www.youtube.com/watch?v={item_external_id}&t=0s"
    return "#"


def _render_overlap_block(clusters: list[Any], items_by_id: dict[str, Any]) -> str:
    """Render the "Everyone's talking about" overlap block (Sub-phase 1 clusters).

    Renders ONLY clusters that actually represent an overlap worth surfacing — those
    with a merged short body (>= 2 short members) OR at least one long-form cross-link
    (a short reaction matched against an episode). Singleton clusters (one lone item,
    no cross-links) are skipped so the block is the genuine "everyone's talking about"
    set, not every item again. Each cluster shows its representative headline (linked
    to the representative item's deep-link) plus, for every long-form episode on the
    topic, a chapter deep-link into the relevant moment (the never-shred cross-link).
    Returns ``""`` when no cluster qualifies (section absent — no empty container).

    Args:
        clusters: Sub-phase 1's :class:`lib.cluster.Cluster` list.
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem`
            (for the representative headline + its deep-link).

    Returns:
        A ``<section class="overlap-block">...</section>`` string, or ``""`` if empty.
    """
    blocks: list[str] = []
    for cluster in clusters:
        member_item_ids = list(getattr(cluster, "member_item_ids", []) or [])
        cross_links = list(getattr(cluster, "cross_links", []) or [])
        # Reason: only surface a genuine overlap — a merged body (2+ short members) or a
        # short<->long cross-link. A lone singleton is already on the page as a card.
        if len(member_item_ids) < 2 and not cross_links:
            continue

        representative_id = str(getattr(cluster, "representative_item_id", "") or "")
        representative_item = items_by_id.get(representative_id)
        headline = (getattr(representative_item, "title", "") or representative_id) if representative_item else representative_id
        headline_link = html_render.render_link(
            _card_deep_link(representative_item) if representative_item else f"https://www.youtube.com/watch?v={representative_id}&t=0s",
            headline,
            css_class="overlap-headline",
        )

        member_count = len(member_item_ids)
        convergence = int(getattr(cluster, "source_diversity", 0) or 0)
        meta = f"{member_count} posts · {convergence} creators" if member_count else f"{convergence} creators"

        cross_link_html = ""
        if cross_links:
            cross_link_items = [
                "<li>"
                + html_render.render_link(
                    str(getattr(cross_link, "chapter_deep_link", "") or ""),
                    f"{html_render._format_timestamp(float(getattr(cross_link, 'chapter_start_seconds', 0.0) or 0.0))} "
                    f"{getattr(cross_link, 'chapter_title', '') or 'episode'}",
                    css_class="overlap-crosslink",
                )
                + "</li>"
                for cross_link in cross_links
            ]
            cross_link_html = '<ul class="overlap-crosslinks">' + "".join(cross_link_items) + "</ul>"

        blocks.append(
            '<article class="overlap-cluster">'
            f'<div class="overlap-topic">{headline_link}</div>'
            f'<div class="overlap-meta">{html_render.escape(meta)}</div>'
            f"{cross_link_html}"
            "</article>"
        )

    if not blocks:
        return ""
    heading = '<h2 class="section-heading">Everyone\'s talking about</h2>'
    return f'<section class="overlap-block">{heading}{"".join(blocks)}</section>'


def _render_trending_rail(trending_items: list[Any], items_by_id: dict[str, Any]) -> str:
    """Render the right-rail internal-trending list, tagged corroborated-vs-scoop.

    Each entry is the trending headline linked to its item/chapter deep-link, with a
    tag badge: ``scoop`` (your network first), ``corroborated`` (also big outside), or
    none (untagged). The list is assumed already velocity-descending (as
    :func:`lib.trending.compute_internal_trending` returns it). Returns ``""`` when the
    list is empty (section absent).

    Args:
        trending_items: The :class:`lib.trending.TrendingItem` list.
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem`
            for the deep-link.

    Returns:
        An ``<aside class="trending-rail">...</aside>`` string, or ``""`` if empty.
    """
    rows: list[str] = []
    for trending_item in trending_items:
        headline = getattr(trending_item, "title", "") or getattr(trending_item, "item_external_id", "") or "Trending"
        link = html_render.render_link(
            _trending_deep_link(trending_item, items_by_id),
            headline,
            css_class="trending-link",
        )
        tag = str(getattr(trending_item, "corroboration_tag", "") or "")
        is_scoop = bool(getattr(trending_item, "is_scoop", False))
        # Reason: a scoop badge is louder than the corroboration tag — show scoop when set.
        if is_scoop:
            badge = '<span class="trending-tag tag-scoop">scoop</span>'
        elif tag:
            badge = f'<span class="trending-tag tag-{html_render.escape(tag)}">{html_render.escape(tag)}</span>'
        else:
            badge = ""
        rows.append(f'<li class="trending-row">{link}{badge}</li>')

    if not rows:
        return ""
    heading = '<h2 class="section-heading">Trending in your network</h2>'
    return f'<aside class="trending-rail">{heading}<ul class="trending-list">' + "".join(rows) + "</ul></aside>"


def _render_scoops_strip(scoops: list[Any], items_by_id: dict[str, Any]) -> str:
    """Render the LOUD scoops strip — dormant-account acceleration, flagged prominently.

    The brief's highest-value signal gets its own top-of-body strip. Each scoop shows a
    loud flag plus the headline linked to its item/chapter deep-link. Returns ``""``
    when there are no scoops (section absent — never fabricated).

    Args:
        scoops: The :class:`lib.trending.TrendingItem` list flagged by
            :func:`lib.external_trending.detect_scoops`.
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem`
            for the deep-link.

    Returns:
        A ``<section class="scoops-strip">...</section>`` string, or ``""`` if empty.
    """
    rows: list[str] = []
    for scoop in scoops:
        headline = getattr(scoop, "title", "") or getattr(scoop, "item_external_id", "") or "Scoop"
        link = html_render.render_link(
            _trending_deep_link(scoop, items_by_id),
            headline,
            css_class="scoop-link",
        )
        rows.append(f'<li class="scoop-row"><span class="scoop-flag">SCOOP</span> {link}</li>')

    if not rows:
        return ""
    heading = '<h2 class="section-heading scoops-heading">⚡ Scoops — your network first</h2>'
    return f'<section class="scoops-strip">{heading}<ul class="scoops-list">' + "".join(rows) + "</ul></section>"


def _build_body(
    grouped: dict[str, list[TieredItem]],
    *,
    item_count: int,
    creator_count: int,
    overlap_html: str = "",
    trending_html: str = "",
    scoops_html: str = "",
) -> str:
    """Assemble one page's body markup from a grouped dict (TL;DR + M3 sections + cards + index).

    Layout order (design brief §3): TL;DR, then the LOUD scoops strip (top, the
    highest-value signal), then the "Everyone's talking about" overlap block, then the
    main cards, the right-rail trending list, and finally the "they also posted" index
    strip. The three M3 sections (``scoops_html`` / ``overlap_html`` / ``trending_html``)
    are pre-rendered by the caller and passed in; each is ``""`` (and so omitted) when
    its source data was not supplied — so the M1/M2 path (no clusters/trending/scoops)
    renders EXACTLY as before (DoD #4 regression).

    Args:
        grouped: A (possibly spill-split) :func:`group_items_by_tier`-shaped dict.
        item_count: Episode count for THIS page's TL;DR header.
        creator_count: Distinct-creator count for THIS page's TL;DR header.
        overlap_html: Pre-rendered overlap block (``""`` when absent — M1 path).
        trending_html: Pre-rendered right-rail trending list (``""`` when absent).
        scoops_html: Pre-rendered scoops strip (``""`` when absent).

    Returns:
        The ``<main>`` body markup (absent sections omitted).
    """
    tldr_html = html_render.render_tldr(episode_count=item_count, creator_count=creator_count)
    main_cards_html = _render_main_cards_section(grouped)
    index_html = _render_index_section(grouped)
    return "\n".join(
        part
        for part in (tldr_html, scoops_html, overlap_html, main_cards_html, trending_html, index_html)
        if part
    )


def _items_by_id_from_tiered(tiered_items: list[TieredItem]) -> dict[str, Any]:
    """Index the tiered batch by ``item_external_id`` for the M3 deep-link resolution.

    The M3 sections (overlap / trending / scoops) reference items by id; this lets them
    reuse the SAME :func:`_card_deep_link` the cards use (source-aware: x.com card_url
    else the YouTube fallback). Pure indexing, no I/O.

    Args:
        tiered_items: The tiered batch.

    Returns:
        A ``item_external_id`` -> :class:`lib.rerank.RankableItem` map.
    """
    items_by_id: dict[str, Any] = {}
    for tiered_item in tiered_items:
        item = tiered_item.scored_item.item
        item_external_id = str(getattr(item, "item_external_id", "") or "")
        if item_external_id:
            items_by_id[item_external_id] = item
    return items_by_id


def render_digest_pages(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    page_2_href: str = DEFAULT_PAGE_2_FILENAME,
    clusters: list[Any] | None = None,
    trending_items: list[Any] | None = None,
    scoops: list[Any] | None = None,
) -> list[str]:
    """Render the digest into one or two self-contained HTML pages (Stage 7b).

    When :func:`estimate_page_height` is within :data:`PAGE_1_BUDGET_PX`, returns a
    single-page list ``[page1]`` with NO page-2 link. When it exceeds the budget,
    spills the Compact + Index tiers to a second page and returns ``[page1, page2]``:
    Hero + Standard STAY on page 1 (which gains a "Continued on page 2 →" link to
    ``page_2_href``); Compact + Index render on page 2. HARD CAP of 2 pages — every
    spilled item goes to page 2 even if page 2 itself would overflow the budget
    (Orbit never produces a page 3).

    The three M3 sections render on PAGE 1 (the screen the user opens first), built from
    the OPTIONAL ``clusters`` / ``trending_items`` / ``scoops`` args. When all three are
    None/empty — the M1/M2 path — every section is ``""`` (omitted), so page 1 is
    byte-for-byte the M1 page (DoD #4 regression). Their deep-links reuse the cards'
    source-aware :func:`_card_deep_link`, resolved against the page's items.

    Rank controls density, never inclusion: nothing is dropped on either path.

    Args:
        tiered_items: The :class:`TieredItem` list from
            :func:`lib.density.assign_density_tiers` (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` — read for an optional
            ``digest_title`` page-title override.
        page_2_href: The href page 1 links to for the spilled content. orbit.py writes
            page 2 to a file of this name beside page 1.
        clusters: OPTIONAL Sub-phase 1 :class:`lib.cluster.Cluster` list for the
            "Everyone's talking about" overlap block. None/empty -> block absent.
        trending_items: OPTIONAL :class:`lib.trending.TrendingItem` list (velocity
            ranked, tagged) for the right-rail. None/empty -> rail absent.
        scoops: OPTIONAL :class:`lib.trending.TrendingItem` list from
            :func:`lib.external_trending.detect_scoops` for the loud scoops strip. None/empty ->
            strip absent.

    Returns:
        ``[page1_html]`` (fits) or ``[page1_html, page2_html]`` (spilled).
    """
    grouped = group_items_by_tier(tiered_items)
    page_title = getattr(config, "digest_title", None) or DEFAULT_DIGEST_TITLE
    estimated_height_px = estimate_page_height(tiered_items)
    spilled = estimated_height_px > PAGE_1_BUDGET_PX

    # Reason: build the three M3 sections ONCE; each is "" when its source is absent
    # (the M1/M2 path), so the body composition is unchanged for the existing tests.
    items_by_id = _items_by_id_from_tiered(tiered_items)
    overlap_html = _render_overlap_block(clusters or [], items_by_id)
    trending_html = _render_trending_rail(trending_items or [], items_by_id)
    scoops_html = _render_scoops_strip(scoops or [], items_by_id)

    if not spilled:
        body_html = _build_body(
            grouped,
            item_count=len(tiered_items),
            creator_count=_count_distinct_creators(tiered_items),
            overlap_html=overlap_html,
            trending_html=trending_html,
            scoops_html=scoops_html,
        )
        pages = [html_render.wrap_page(page_title, body_html)]
    else:
        page1_grouped, page2_grouped = _split_grouped_for_spill(grouped)
        page1_items = page1_grouped[TIER_HERO] + page1_grouped[TIER_STANDARD]
        page2_items = page2_grouped[TIER_COMPACT] + page2_grouped[TIER_INDEX]

        page1_body = _build_body(
            page1_grouped,
            item_count=len(page1_items),
            creator_count=_count_distinct_creators(page1_items),
            overlap_html=overlap_html,
            trending_html=trending_html,
            scoops_html=scoops_html,
        )
        # Reason: the spill link goes AFTER the page-1 cards so the reader hits it
        # once they've exhausted the Hero/Standard band.
        page1_body = f"{page1_body}\n{_render_continued_link(page_2_href)}"
        # Reason: page 2 is the low-tier overflow only — the M3 sections live on page 1.
        page2_body = _build_body(
            page2_grouped,
            item_count=len(page2_items),
            creator_count=_count_distinct_creators(page2_items),
        )
        pages = [
            html_render.wrap_page(page_title, page1_body),
            html_render.wrap_page(page_title, page2_body),
        ]

    # Reason: single render_completed call with additive page_count / spilled kwargs.
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
    )
    return pages


def render_digest_html(
    tiered_items: list[TieredItem],
    config: Any = None,
    *,
    clusters: list[Any] | None = None,
    trending_items: list[Any] | None = None,
    scoops: list[Any] | None = None,
) -> str:
    """Render the tiered items into ONE self-contained HTML digest page (Stage 7a/b).

    Backwards-compatible single-string entry point: returns PAGE 1 of
    :func:`render_digest_pages`. When the digest fits :data:`PAGE_1_BUDGET_PX`, that
    is the whole digest (no page-2 link). When it overflows, this returns page 1 WITH
    the "Continued on page 2 →" link — but only page 1's string (the caller that needs
    the page-2 file uses :func:`render_digest_pages`, which orbit.py wires).

    Assembles the design-brief layout: TL;DR header, the (M3) scoops strip + overlap
    block, Hero/Standard full cards WITH deep-link chapter lists, Compact rows, the (M3)
    right-rail trending list, and the bottom "they also posted" Index strip. The three
    M3 sections render only when their optional source data is supplied — with none
    supplied (the M1/M2 path) the page is byte-for-byte the M1 page. Every card links to
    its whole-video ``watch?v=ID&t=0s`` and every chapter to its ``chapter.deep_link``;
    every user-controlled string is escaped and every href allowlist-checked inside
    :mod:`lib.html_render`.

    Rank controls density, never inclusion: every tiered item appears somewhere
    (Hero as a big card down to Index as a one-line entry); nothing is dropped. An
    empty ``tiered_items`` still produces a valid page (TL;DR ``0 episodes``).

    Args:
        tiered_items: The :class:`TieredItem` list from
            :func:`lib.density.assign_density_tiers` (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` — read for an optional
            ``digest_title`` page-title override. None / missing -> the default title.
        clusters: OPTIONAL Sub-phase 1 clusters for the overlap block (None -> absent).
        trending_items: OPTIONAL trending list for the right-rail (None -> absent).
        scoops: OPTIONAL detected scoops for the scoops strip (None -> absent).

    Returns:
        The complete ``<!DOCTYPE html>...`` page-1 string of the digest.

    Example:
        >>> render_digest_html([]).startswith("<!DOCTYPE html>")  # doctest: +SKIP
        True
    """
    return render_digest_pages(
        tiered_items,
        config,
        clusters=clusters,
        trending_items=trending_items,
        scoops=scoops,
    )[0]
