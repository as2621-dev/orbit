"""Self-contained ``digest.md`` markdown twin of the Tiles HTML digest (issue #6).

The render stage writes this ONE markdown file beside the rendered HTML. It carries the
SAME content the Tiles pages carry — tier grouping, the LLM verdict, per-item blurbs, the
"ahead of the curve" trio, and working deep-links — so a fresh Claude session that
receives ONLY this file's text has the full digest (that is exactly what #7 does with it).

Two deliberate differences from :mod:`lib.render`, both because the consumer is a text
file an LLM reads, not a browser page:

  * **Self-contained means NO local references.** The markdown is text-first: it carries
    no thumbnails/avatars (no images at all), only ``https``/``http`` deep-links. A
    ``file://`` url, an inlined image, or a relative ``./`` path would point at something
    a fresh session cannot see — see :func:`_md_link`, which linkifies ONLY web URLs.
  * **The 2-page HTML spill is a LAYOUT concern, not a content boundary.** A digest that
    spills to two HTML pages produces ONE ``digest.md`` with every item — the markdown
    simply groups all tiered items by density tier.

PARITY BY REUSE (not by reimplementation): to guarantee the twin never drifts from the
HTML, this module reuses :mod:`lib.render`'s OWN content-selection helpers — tier
grouping, the card/chapter/trending deep-link resolvers, the trio selection inputs, the
masthead counts. Those are module-private (underscore) helpers; importing them is a
deliberate coupling so a change to how the HTML selects content flows to the markdown for
free. This module owns only the markdown PRESENTATION.

Rule 5: no LLM here — the verdict + summaries are computed upstream and passed in; this is
pure deterministic string building.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

# Make ``lib`` importable whether this module is imported as ``lib.markdown_render`` (via
# orbit.py's sys.path insert of the scripts dir) or run from the scripts dir directly.
# Mirrors render.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log, render, tiles  # noqa: E402  (import must follow the sys.path inserts above)
from lib.density import TIER_COMPACT, TIER_HERO, TIER_INDEX, TIER_STANDARD  # noqa: E402
from lib.html_render import _format_timestamp  # noqa: E402

# The filename the render stage writes beside page 1. This is the CONTRACT with #7, whose
# ONLY input is this file's path — kept as one explicit constant (mirrors
# render.DEFAULT_PAGE_2_FILENAME).
DIGEST_MD_FILENAME: str = "digest.md"

# Tier order + human headings for the "From your feed · ranked" section. Mirrors the HTML
# density ladder (Hero loudest -> Index quietest). Any item whose density_tier falls
# OUTSIDE these four constants is still rendered (see _build_feed's leftover-group fold),
# so the twin can never show fewer items than the HTML masonry.
_TIER_HEADINGS: tuple[tuple[str, str], ...] = (
    (TIER_HERO, "Hero"),
    (TIER_STANDARD, "Standard"),
    (TIER_COMPACT, "Compact"),
    (TIER_INDEX, "Index"),
)

# Heading for the leftover-group fold: an out-of-band density_tier (not one of the four
# constants above) renders here so no item is dropped relative to the HTML.
_OTHER_TIER_HEADING: str = "Other"


def resolve_digest_md_path(html_path: Path) -> Path:
    """Resolve the ``digest.md`` path beside a rendered page-1 HTML path (the #7 contract).

    #7's only input is this path, and the render stage writes it here — both derive it the
    SAME way (``digest.md`` in page 1's directory), centralized here so the join lives in
    exactly one place rather than being re-implemented at each call site.

    Args:
        html_path: The page-1 HTML output path.

    Returns:
        The ``digest.md`` path in the same directory as ``html_path``.

    Example:
        >>> resolve_digest_md_path(Path("/out/today.html")).name
        'digest.md'
    """
    return Path(html_path).parent / DIGEST_MD_FILENAME


# --- Text + link primitives --------------------------------------------------


def _oneline(text: str) -> str:
    """Collapse any run of whitespace (incl. newlines) to single spaces, stripped.

    Titles, blurbs and tweet bodies flow onto one line so a stray newline can never break
    the surrounding markdown structure (a heading, a list item, a link label).
    """
    return " ".join((text or "").split())


def _is_web_url(url: str) -> bool:
    """True only for an ``http``/``https`` URL — the only scheme the markdown ever links.

    This is the self-contained guard: a ``file://`` path, a ``javascript:`` payload, a
    bare ``#`` anchor or a relative path all return False and are rendered as plain text
    rather than a link a fresh session could not (or should not) follow.
    """
    return (url or "").strip().startswith(("https://", "http://"))


def _md_link(text: str, url: str) -> str:
    """Render ``[text](url)`` for a real web URL; otherwise the plain (one-lined) text.

    Linkifying ONLY web URLs is what keeps the digest self-contained — see
    :func:`_is_web_url`. Deep-links (``...&t=<s>s``) pass through the URL VERBATIM (markdown
    does not HTML-escape), which is the round-trip the whole deep-link feature depends on.
    """
    label = _oneline(text)
    target = (url or "").strip()
    if _is_web_url(target):
        return f"[{label}]({target})"
    return label


def _trend_marker(category: str, your_count: int) -> str:
    """Map a "Trending now" category to its markdown marker (mirrors the HTML glyphs)."""
    if category == tiles.CATEGORY_YOURS:
        return f"↗ {your_count} of yours"
    if category == tiles.CATEGORY_DORMANT:
        return "◆ dormant"
    return "○ external"


# --- Section builders --------------------------------------------------------


def _build_header(
    page_title: str,
    reference_date: Optional[date],
    tiered_items: list[Any],
    scoops: list[Any],
    clusters: list[Any],
) -> str:
    """Build the masthead: the title, the dateline, and the day's counts line.

    The counts mirror render.py's masthead fields exactly (distinct-creator sources,
    accounted-for total, scoops, dormant, clusters) so the twin reports the same tallies.
    """
    dateline = render._format_dateline(reference_date)
    source_total, accounted, scoop_count, dormant_count, cluster_count = render._masthead_counts(
        tiered_items, scoops, clusters
    )
    counts_line = (
        f"**{accounted} of {source_total} sources accounted for** · "
        f"{scoop_count} scoops · {dormant_count} dormant · {cluster_count} clusters"
    )
    return f"# {page_title}\n\n{dateline}\n\n{counts_line}"


def _build_verdict(verdict: str) -> str:
    """Build the verdict blockquote, or ``""`` when there is no verdict (no fabrication)."""
    text = (verdict or "").strip()
    return f"> {_oneline(text)}" if text else ""


def _build_trio(
    scoops: list[Any],
    trending_items: list[Any],
    items_by_id: dict[str, Any],
    summaries: dict[str, str],
) -> str:
    """Build the "Ahead of the curve" section (top scoop / trending rows / hidden gem).

    Reproduces render._build_ahead_trio's SELECTION (which scoop, the top-N trending rows,
    the top-velocity gem) but emits markdown. The whole section is omitted on a quiet day
    (no scoops and no trending items), matching the HTML's omit-when-empty behavior.
    """
    if not scoops and not trending_items:
        return ""

    blocks: list[str] = ["## Ahead of the curve"]

    if scoops:
        top_scoop = scoops[0]
        scoop_id = str(getattr(top_scoop, "item_external_id", "") or "")
        resolved = items_by_id.get(scoop_id)
        attribution = (
            (getattr(resolved, "channel_name", "") or "")
            if resolved is not None
            else (getattr(top_scoop, "creator_external_id", "") or "")
        ) or "your network"
        link = render._trending_deep_link(top_scoop, items_by_id)
        lines = [f"### Scoop — {_oneline(attribution)}", "", _md_link(getattr(top_scoop, "title", "") or "", link)]
        blurb = summaries.get(scoop_id, "")
        if blurb.strip():
            lines += ["", _oneline(blurb)]
        blocks.append("\n".join(lines))

    if trending_items:
        rows = ["### Trending now", ""]
        for trending_item in trending_items[: render._TRENDING_MAX_ROWS]:
            category, your_count = render._trending_row_category(trending_item)
            link = render._trending_deep_link(trending_item, items_by_id)
            rows.append(
                f"- {_trend_marker(category, your_count)} — {_md_link(getattr(trending_item, 'title', '') or '', link)}"
            )
        blocks.append("\n".join(rows))

        gem = trending_items[0]
        gem_id = str(getattr(gem, "item_external_id", "") or "")
        ratio = float(getattr(gem, "baseline_relative_ratio", 0.0) or 0.0)
        creator = (getattr(gem, "creator_external_id", "") or getattr(gem, "title", "") or "").upper()
        pct = max(0, int(round(ratio * 100)))
        gem_lines = [
            f"### Hidden gem — {_oneline(creator)} · +{pct}% vs baseline",
            "",
            _oneline(getattr(gem, "title", "") or ""),
        ]
        gem_blurb = summaries.get(gem_id, "")
        if gem_blurb.strip():
            gem_lines += ["", _oneline(gem_blurb)]
        blocks.append("\n".join(gem_lines))

    return "\n\n".join(blocks)


def _build_feature_entry(item: Any, *, summaries: dict[str, str], cross_links_by_id: dict[str, list[Any]]) -> str:
    """Build a Hero/Standard feature entry: title deep-link, meta, blurb, chapters, cross-links.

    Chapters reuse render._chapter_rows (same visible cap + "+ N more" overflow the HTML
    uses), so each chapter's ``...&t=<s>s`` deep-link surfaces exactly as the HTML surfaces it.
    """
    item_id = str(getattr(item, "item_external_id", "") or "")
    lines = [
        f"#### {_md_link(getattr(item, 'title', '') or '', render._card_deep_link(item))}",
        "",
        render._meta_label(item),
    ]
    blurb = summaries.get(item_id, "")
    if blurb.strip():
        lines += ["", _oneline(blurb)]

    chapter_rows, more_count = render._chapter_rows(item)
    if chapter_rows:
        lines.append("")
        lines += [f"- `{row.chip}` {_md_link(row.text, row.url)}" for row in chapter_rows]
        if more_count > 0:
            lines.append(f"- _+ {more_count} more chapters_")

    cross_links = cross_links_by_id.get(item_id, [])
    if cross_links:
        lines += ["", "_Also covered:_ " + " · ".join(_md_link(link.label, link.url) for link in cross_links)]

    return "\n".join(lines)


def _build_compact_entry(item: Any) -> str:
    """Build a Compact/Index entry: a one-line title deep-link + meta + optional key moment."""
    line = f"- {_md_link(getattr(item, 'title', '') or '', render._card_deep_link(item))} — {render._meta_label(item)}"
    chapters = getattr(item, "chapters", None) or []
    if chapters:
        first = chapters[0]
        chip = _format_timestamp(getattr(first, "start_seconds", 0.0))
        chip_label = _oneline(getattr(first, "title", "") or "")
        line += f" · `{chip}` {chip_label}".rstrip()
    return line


def _build_tweet_entry(item: Any) -> str:
    """Build an X-tweet entry: the source handle, the tweet body, its x.com permalink.

    Dispatched for an X item at ANY tier (mirrors render._build_masonry_tiles, which
    renders a tweet tile before consulting the density tier). The source label reuses
    render._tweet_source_label so the HTML tile and this entry never drift.
    """
    lines = [f"#### {render._tweet_source_label(item)}", "", _oneline(getattr(item, "title", "") or "")]
    card_url = render._card_deep_link(item)
    if _is_web_url(card_url):
        lines += ["", _md_link("Open on X", card_url)]
    return "\n".join(lines)


def _render_tier_entries(
    tier_items: list[Any], *, summaries: dict[str, str], cross_links_by_id: dict[str, list[Any]]
) -> list[str]:
    """Render one tier group's items to markdown entries, dispatching per item.

    Dispatch mirrors render._build_masonry_tiles exactly: an X item -> tweet entry (checked
    FIRST, before the tier); a Hero/Standard item -> feature entry; everything lower ->
    compact entry. An out-of-band tier lands in the ``else`` (compact), matching the HTML
    non-spilled masonry's fallback for an unknown tier.
    """
    entries: list[str] = []
    for tiered_item in tier_items:
        item = tiered_item.scored_item.item
        if render._is_tweet(item):
            entries.append(_build_tweet_entry(item))
        elif tiered_item.density_tier in render._FEATURE_TIERS:
            entries.append(_build_feature_entry(item, summaries=summaries, cross_links_by_id=cross_links_by_id))
        else:
            entries.append(_build_compact_entry(item))
    return entries


def _build_feed(tiered_items: list[Any], *, summaries: dict[str, str], cross_links_by_id: dict[str, list[Any]]) -> str:
    """Build the "From your feed · ranked" section, grouping every tiered item by tier.

    Renders the four known density tiers in ladder order, then folds any leftover group (an
    out-of-band density_tier) under a generic heading — so the union of what is rendered is
    ALWAYS every tiered item, never fewer than the HTML masonry shows (the parity invariant
    #7 depends on). An empty batch yields a coherent quiet-day line, not a bare heading.
    """
    grouped = render.group_items_by_tier(tiered_items)
    sections = ["## From your feed · ranked"]
    rendered_any = False
    covered: set[str] = set()

    for tier, heading in _TIER_HEADINGS:
        covered.add(tier)
        tier_items = grouped.get(tier, [])
        if not tier_items:
            continue
        rendered_any = True
        entries = _render_tier_entries(tier_items, summaries=summaries, cross_links_by_id=cross_links_by_id)
        sections.append("\n\n".join([f"### {heading}"] + entries))

    # Fold any out-of-band tier group (a density_tier outside the four constants) so the
    # twin can NEVER show fewer items than the HTML masonry, which renders every item
    # regardless of tier. Unreachable with today's assign_density_tiers, but drop-proof.
    for tier, tier_items in grouped.items():
        if tier in covered or not tier_items:
            continue
        rendered_any = True
        entries = _render_tier_entries(tier_items, summaries=summaries, cross_links_by_id=cross_links_by_id)
        sections.append("\n\n".join([f"### {_OTHER_TIER_HEADING}"] + entries))

    if not rendered_any:
        sections.append("_No qualifying items in your feed today._")

    return "\n\n".join(sections)


def render_digest_markdown(
    tiered_items: list[Any],
    config: Any = None,
    *,
    clusters: list[Any] | None = None,
    trending_items: list[Any] | None = None,
    scoops: list[Any] | None = None,
    verdict: str = "",
    summaries: dict[str, str] | None = None,
    reference_date: Optional[date] = None,
) -> str:
    """Render the tiered items into ONE self-contained ``digest.md`` markdown string.

    Assembles the same content the HTML digest carries — masthead, verdict, the "ahead of
    the curve" trio, and the tier-grouped feed — into a single markdown document. Unlike
    :func:`lib.render.render_digest_pages`, the 2-page spill is collapsed: every tiered
    item appears once, grouped by density tier. Text-first: no images, only web deep-links.

    Args:
        tiered_items: The :class:`lib.density.TieredItem` list (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` — read for ``digest_title``.
        clusters: OPTIONAL clusters (supply the feed cross-links + the masthead count).
        trending_items: OPTIONAL trending list (the trio's rows + hidden gem).
        scoops: OPTIONAL detected scoops (the trio's top scoop + the dormant count).
        verdict: The pre-computed LLM verdict sentence (``""`` -> omitted).
        summaries: The pre-computed ``item_external_id`` -> blurb map (None -> ``{}``).
        reference_date: The masthead dateline date (defaults to today; injectable).

    Returns:
        The complete ``digest.md`` markdown string (always non-empty and coherent).

    Example:
        >>> md = render_digest_markdown([])
        >>> md.startswith("# ")
        True
    """
    clusters = clusters or []
    trending_items = trending_items or []
    scoops = scoops or []
    summaries = summaries or {}

    page_title = getattr(config, "digest_title", None) or render.DEFAULT_DIGEST_TITLE
    items_by_id = render._items_by_id(tiered_items)
    cross_links_by_id = render._cross_links_by_id(clusters)

    sections = [
        _build_header(page_title, reference_date, tiered_items, scoops, clusters),
        _build_verdict(verdict),
        _build_trio(scoops, trending_items, items_by_id, summaries),
        _build_feed(tiered_items, summaries=summaries, cross_links_by_id=cross_links_by_id),
    ]
    markdown = "\n\n".join(section for section in sections if section) + "\n"

    log.log_info(
        "markdown_render_completed",
        item_count=len(tiered_items),
        has_verdict=bool(verdict.strip()),
        trending_count=len(trending_items),
        scoop_count=len(scoops),
        char_count=len(markdown),
    )
    return markdown
