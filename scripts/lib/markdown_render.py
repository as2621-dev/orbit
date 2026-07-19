"""Self-contained ``digest.md`` markdown twin of the Tiles HTML digest (issue #6).

The render stage writes this ONE markdown file beside the rendered HTML. It carries the
SAME content the Tiles pages carry — the coverage counts, the YouTube feed grouped by
tier, the X posts below it, per-item blurbs, and working deep-links — so a fresh Claude
session that receives ONLY this file's text has the full digest (that is exactly what #7
does with it).

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
grouping, the YouTube/X split, the card/chapter deep-link resolvers, the masthead counts.
Those are module-private (underscore) helpers; importing them is a deliberate coupling so
a change to how the HTML selects content flows to the markdown for free. This module owns
only the markdown PRESENTATION.

Rule 5: no LLM here — the summaries are computed upstream and passed in; this is pure
deterministic string building.
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

from lib import log, render  # noqa: E402  (import must follow the sys.path inserts above)
from lib.density import TIER_COMPACT, TIER_HERO, TIER_INDEX, TIER_STANDARD  # noqa: E402
from lib.html_render import _format_timestamp  # noqa: E402

# The filename the render stage writes beside page 1. This is the CONTRACT with #7, whose
# ONLY input is this file's path — kept as one explicit constant (mirrors
# render.DEFAULT_PAGE_2_FILENAME).
DIGEST_MD_FILENAME: str = "digest.md"

# Tier order + human headings for the YouTube feed section. Mirrors the HTML density
# ladder (Hero loudest -> Index quietest). Any item whose density_tier falls OUTSIDE
# these four constants is still rendered (see _build_youtube_feed's leftover-group fold),
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


def read_digest_markdown(html_path: Path) -> str:
    """Read the ``digest.md`` twin beside page 1 for the email body — fail-soft, never raises.

    Lives beside :func:`resolve_digest_md_path` so the twin's path AND read contract stay
    in one module (orbit.py stays sequencing-only). A missing/unreadable/corrupt twin (its
    render is itself fail-soft, issue #6) degrades to ``""`` with a loud warning: the email
    then carries the TL;DR + chat link and points at the HTML attachment instead (PRD
    story #19).

    Args:
        html_path: The page-1 HTML output path (the twin sits beside it).

    Returns:
        The digest.md text, or "" when unavailable.
    """
    markdown_path = resolve_digest_md_path(html_path)
    try:
        return markdown_path.read_text(encoding="utf-8")
    # UnicodeDecodeError too: a crash mid-write can leave a split multibyte char at the
    # file's tail, and the email must still send (fail-soft) — not just on OSError.
    except (OSError, UnicodeDecodeError) as read_error:
        log.log_warning(
            "digest_markdown_unreadable_for_email",
            markdown_path=str(markdown_path),
            error_type=type(read_error).__name__,
            detail="The email body will carry the TL;DR + chat link without the markdown section.",
        )
        return ""


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


# --- Section builders --------------------------------------------------------


def _build_header(
    page_title: str,
    reference_date: Optional[date],
    tiered_items: list[Any],
    tracked_source_total: int,
) -> str:
    """Build the masthead: the title, the dateline, and the day's coverage counts.

    The counts come from render._masthead_counts, so the twin reports the SAME tracked /
    posted / item tallies the HTML masthead does.
    """
    dateline = render._format_dateline(reference_date)
    tracked_total, posted_count, item_count = render._masthead_counts(tiered_items, tracked_source_total)
    counts_line = f"**{tracked_total} tracked · {posted_count} posted · {item_count} items**"
    return f"# {page_title}\n\n{dateline}\n\n{counts_line}"


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
    """Render one YouTube tier group's items to markdown entries, dispatching per item.

    Dispatch mirrors render._build_masonry_tiles: a Hero/Standard item -> feature entry;
    everything lower -> compact entry. An out-of-band tier lands in the ``else``
    (compact), matching the HTML masonry's fallback for an unknown tier. X items never
    reach here — they are split out upstream and rendered in their own section.
    """
    entries: list[str] = []
    for tiered_item in tier_items:
        item = tiered_item.scored_item.item
        if tiered_item.density_tier in render._FEATURE_TIERS:
            entries.append(_build_feature_entry(item, summaries=summaries, cross_links_by_id=cross_links_by_id))
        else:
            entries.append(_build_compact_entry(item))
    return entries


def _build_youtube_feed(
    youtube_items: list[Any], *, summaries: dict[str, str], cross_links_by_id: dict[str, list[Any]]
) -> str:
    """Build the "From YouTube · ranked" section, grouping every video by density tier.

    Renders the four known density tiers in ladder order, then folds any leftover group (an
    out-of-band density_tier) under a generic heading — so the union of what is rendered is
    ALWAYS every video passed in, never fewer than the HTML masonry shows (the parity
    invariant #7 depends on). An empty batch yields a coherent quiet-day line, not a bare
    heading.
    """
    grouped = render.group_items_by_tier(youtube_items)
    sections = ["## From YouTube · ranked"]
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
        sections.append("_No new videos from your channels today._")

    return "\n\n".join(sections)


def _build_x_feed(x_items: list[Any]) -> str:
    """Build the "From X" section below the videos, or ``""`` when there are no X posts.

    Mirrors the HTML, which omits the X masonry entirely on a YouTube-only day rather
    than leaving a dangling heading.
    """
    if not x_items:
        return ""
    entries = [_build_tweet_entry(tiered_item.scored_item.item) for tiered_item in x_items]
    return "\n\n".join(["## From X"] + entries)


def render_digest_markdown(
    tiered_items: list[Any],
    config: Any = None,
    *,
    clusters: list[Any] | None = None,
    tracked_source_total: int = 0,
    summaries: dict[str, str] | None = None,
    reference_date: Optional[date] = None,
) -> str:
    """Render the tiered items into ONE self-contained ``digest.md`` markdown string.

    Assembles the same content the HTML digest carries — the masthead coverage counts, the
    tier-grouped YouTube feed, and the X posts below it — into a single markdown document.
    Unlike :func:`lib.render.render_digest_pages`, the 2-page spill is collapsed: every
    tiered item appears once. Text-first: no images, only web deep-links.

    Args:
        tiered_items: The :class:`lib.density.TieredItem` list (already rank-ordered + tiered).
        config: An optional :class:`lib.config.OrbitConfig` — read for ``digest_title``.
        clusters: OPTIONAL clusters (supply the feed cross-links).
        tracked_source_total: Total sources watched, for the masthead coverage count
            (0 degrades to the posted count — see :func:`lib.render._masthead_counts`).
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
    summaries = summaries or {}

    page_title = getattr(config, "digest_title", None) or render.DEFAULT_DIGEST_TITLE
    cross_links_by_id = render._cross_links_by_id(clusters)
    youtube_items, x_items = render.split_youtube_and_x(tiered_items)

    sections = [
        _build_header(page_title, reference_date, tiered_items, tracked_source_total),
        _build_youtube_feed(youtube_items, summaries=summaries, cross_links_by_id=cross_links_by_id),
        _build_x_feed(x_items),
    ]
    markdown = "\n\n".join(section for section in sections if section) + "\n"

    log.log_info(
        "markdown_render_completed",
        item_count=len(tiered_items),
        youtube_count=len(youtube_items),
        x_count=len(x_items),
        char_count=len(markdown),
    )
    return markdown
