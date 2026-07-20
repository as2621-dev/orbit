"""Adapt the ranked pipeline batch into :mod:`lib.ledger`'s view models.

:mod:`lib.ledger` is a PURE renderer — it takes ``LedgerVideo`` / ``LedgerPost`` /
``LedgerChannel`` / ``LedgerHandle`` tuples and emits HTML, knowing nothing about
:class:`lib.density.TieredItem`, ranking, images, or sections. This module is the seam
between them: it turns a Stage-6 batch plus a section map into exactly those tuples.

It is the ledger's analogue of :mod:`lib.render`'s ``_build_*_tile`` family, kept in its
OWN module so the renderer stays free of pipeline types and the pipeline stays free of
HTML — either side can be tested without the other.

**Shared derivations, deliberately imported from :mod:`lib.render`.** The dateline, the
masthead tallies, the card deep-link and the YouTube/X split are reached through
``render._format_dateline`` / ``render._masthead_counts`` / ``render._card_deep_link`` /
``render.split_youtube_and_x`` rather than reimplemented. :mod:`lib.markdown_render`
already does exactly this, for exactly this reason: two mastheads quoting DIFFERENT
tracked/posted/item numbers for the same run would be a real bug, and a copy is how that
drift starts (Rule 11 — match the codebase's convention).

Images are fetched and base64-inlined at BUILD time through the injectable
``inline_image`` seam (default :func:`lib.images.fetch_and_inline`, which is disk-cached),
so the rendered page is self-contained and a test never touches the network.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.ledger_adapter`` (via orbit.py's sys.path insert of the scripts dir) or run from
# the scripts dir directly. Mirrors ledger.py / markdown_render.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import images, render  # noqa: E402  (import must follow the sys.path inserts above)
from lib.density import TieredItem  # noqa: E402
from lib.ledger import (  # noqa: E402
    LedgerChannel,
    LedgerCounts,
    LedgerHandle,
    LedgerPost,
    LedgerVideo,
    channel_monogram,
    shorten_channel_name,
)
from lib.sections import Section  # noqa: E402

# Cap on the X row excerpt. The web row is a single ellipsised line and the mobile row
# wraps to two, so anything past this is never seen — trimming here keeps the HTML small
# rather than shipping a full tweet body the CSS then hides.
MAX_EXCERPT_CHARS: int = 160

# The image seam: a URL in, a ``data:`` URI (or None) out. Mirrors ``render.InlineImage``.
InlineImage = Callable[[str], Optional[str]]


class LedgerView(NamedTuple):
    """Everything :mod:`lib.ledger`'s two document renderers need, derived once.

    Both ``render_web_document`` and ``render_mobile_document`` are fed from ONE of these,
    so the desktop ledger and the mobile bulletin can never disagree about the same run.

    Attributes:
        dateline: The formatted masthead date line.
        counts: The tracked / posted / item tallies.
        videos: The ranked YouTube rows, in render order.
        channels: The channel chips for the web YouTube stat bar.
        posts: The ranked X rows, in render order.
        handles: The handle chips for the web X stat bar.
    """

    dateline: str
    counts: LedgerCounts
    videos: list[LedgerVideo]
    channels: list[LedgerChannel]
    posts: list[LedgerPost]
    handles: list[LedgerHandle]


def _inline(image_url: str, inline_image: InlineImage) -> str:
    """Inline one image URL to a ``data:`` URI, normalising every failure to "".

    Args:
        image_url: The source URL (may be empty).
        inline_image: The fetch/inline seam.

    Returns:
        The ``data:`` URI, or "" so the caller renders its placeholder block.
    """
    if not image_url:
        return ""
    return inline_image(image_url) or ""


def build_excerpt(post_text: str) -> str:
    """Reduce a tweet body to its first non-empty line, capped for the row.

    Reason: the design's X row is one line. Taking the FIRST line (rather than the first
    N chars of the whole body) keeps a multi-line tweet's opening thought intact instead
    of slicing mid-thought across a line break.

    Args:
        post_text: The raw tweet body.

    Returns:
        The trimmed excerpt (<= :data:`MAX_EXCERPT_CHARS`), ellipsised when cut.

    Example:
        >>> build_excerpt("First line\\n\\nSecond line")
        'First line'
        >>> build_excerpt("   ")
        ''
    """
    for raw_line in post_text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if len(line) <= MAX_EXCERPT_CHARS:
            return line
        clipped = line[: MAX_EXCERPT_CHARS - 1]
        last_space_index = clipped.rfind(" ")
        if last_space_index > 0:
            clipped = clipped[:last_space_index]
        return clipped.rstrip(" ,;:—-") + "…"
    return ""


def post_handle(item: Any) -> str:
    """Derive an X item's ``@handle`` display string.

    Prefers ``creator_external_id`` (the stable handle rank keys on) and falls back to
    ``channel_name``. The ``@`` is added only when absent, so a stored ``@name`` never
    becomes ``@@name`` — the same guard :func:`lib.render._tweet_source_label` applies.

    Args:
        item: A :class:`lib.rerank.RankableItem` for an X post.

    Returns:
        The handle with exactly one leading ``@``, or "" when unknown.

    Example:
        >>> from types import SimpleNamespace
        >>> post_handle(SimpleNamespace(creator_external_id="sama", channel_name=""))
        '@sama'
        >>> post_handle(SimpleNamespace(creator_external_id="@sama", channel_name=""))
        '@sama'
    """
    raw_handle = (getattr(item, "creator_external_id", "") or "") or (getattr(item, "channel_name", "") or "")
    raw_handle = raw_handle.strip()
    if not raw_handle:
        return ""
    return raw_handle if raw_handle.startswith("@") else f"@{raw_handle}"


def _points_for(item: Any, sections_by_video: dict[str, list[Section]]) -> list[Any]:
    """Look up one video's rendered section lines.

    Args:
        item: The YouTube :class:`lib.rerank.RankableItem`.
        sections_by_video: The Stage-6.5 ``{video_id: [Section]}`` map.

    Returns:
        The video's :class:`lib.ledger.LedgerPoint`-shaped rows (possibly empty — the
        renderer degrades to a title-only row).
    """
    from lib.ledger import LedgerPoint

    item_external_id = str(getattr(item, "item_external_id", "") or "")
    return [
        LedgerPoint(
            timestamp_label=section.timestamp_label,
            deep_link=section.deep_link,
            summary_text=section.summary_text,
        )
        for section in sections_by_video.get(item_external_id, [])
    ]


def build_videos(
    youtube_items: Sequence[TieredItem],
    *,
    sections_by_video: dict[str, list[Section]],
    inline_image: InlineImage,
) -> list[LedgerVideo]:
    """Build the ranked YouTube rows.

    Rank is the item's 1-based position in the ALREADY rank-ordered YouTube half, so the
    ledger's ``01, 02, ...`` column is the same order Stage 6 produced.

    Args:
        youtube_items: The YouTube half, in descending rank order.
        sections_by_video: The ``{video_id: [Section]}`` map.
        inline_image: The image seam.

    Returns:
        The rows, in render order.
    """
    videos: list[LedgerVideo] = []
    for rank_position, tiered_item in enumerate(youtube_items, start=1):
        item = tiered_item.scored_item.item
        videos.append(
            LedgerVideo(
                rank=rank_position,
                title=str(getattr(item, "title", "") or ""),
                channel_name=str(getattr(item, "channel_name", "") or ""),
                card_url=render._card_deep_link(item),
                thumb_src=_inline(str(getattr(item, "image_url", "") or ""), inline_image),
                points=_points_for(item, sections_by_video),
            )
        )
    return videos


def build_posts(x_items: Sequence[TieredItem], *, inline_image: InlineImage) -> list[LedgerPost]:
    """Build the ranked X rows.

    Args:
        x_items: The X half, in descending rank order.
        inline_image: The image seam.

    Returns:
        The rows, in render order.
    """
    posts: list[LedgerPost] = []
    for rank_position, tiered_item in enumerate(x_items, start=1):
        item = tiered_item.scored_item.item
        posts.append(
            LedgerPost(
                rank=rank_position,
                handle=post_handle(item),
                excerpt=build_excerpt(str(getattr(item, "title", "") or "")),
                card_url=render._card_deep_link(item),
                avatar_src=_inline(str(getattr(item, "image_url", "") or ""), inline_image),
            )
        )
    return posts


def _group_by_creator(tiered_items: Sequence[TieredItem]) -> list[tuple[Any, int]]:
    """Group a half by creator, most-represented first.

    Keys on ``creator_external_id`` (falling back to ``channel_name``) so two spellings of
    one creator's display name still collapse into a single chip. Ties break on the
    creator's FIRST appearance, which is its best-ranked item — so the chip order tracks
    rank rather than wobbling between runs.

    Args:
        tiered_items: One platform half, in rank order.

    Returns:
        ``[(representative_item, item_count), ...]``, count-descending.
    """
    counts: dict[str, int] = {}
    representatives: dict[str, Any] = {}
    first_seen: dict[str, int] = {}
    for position, tiered_item in enumerate(tiered_items):
        item = tiered_item.scored_item.item
        creator_key = (getattr(item, "creator_external_id", "") or "") or (
            getattr(item, "channel_name", "") or ""
        )
        counts[creator_key] = counts.get(creator_key, 0) + 1
        if creator_key not in representatives:
            representatives[creator_key] = item
            first_seen[creator_key] = position
    ordered_keys = sorted(counts, key=lambda key: (-counts[key], first_seen[key]))
    return [(representatives[key], counts[key]) for key in ordered_keys]


def build_channels(youtube_items: Sequence[TieredItem]) -> list[LedgerChannel]:
    """Build the YouTube channel chips for the web stat bar.

    Args:
        youtube_items: The YouTube half, in rank order.

    Returns:
        The chips, most-represented channel first.
    """
    channels: list[LedgerChannel] = []
    for item, item_count in _group_by_creator(youtube_items):
        channel_name = str(getattr(item, "channel_name", "") or "")
        channels.append(
            LedgerChannel(
                monogram=channel_monogram(channel_name),
                display_name=shorten_channel_name(channel_name),
                item_count=item_count,
            )
        )
    return channels


def build_handles(x_items: Sequence[TieredItem], *, inline_image: InlineImage) -> list[LedgerHandle]:
    """Build the X handle chips for the web stat bar.

    Args:
        x_items: The X half, in rank order.
        inline_image: The image seam.

    Returns:
        The chips, most-represented handle first.
    """
    handles: list[LedgerHandle] = []
    for item, item_count in _group_by_creator(x_items):
        handles.append(
            LedgerHandle(
                handle=post_handle(item),
                item_count=item_count,
                avatar_src=_inline(str(getattr(item, "image_url", "") or ""), inline_image),
            )
        )
    return handles


def build_ledger_view(
    tiered_items: list[TieredItem],
    *,
    sections_by_video: Optional[dict[str, list[Section]]] = None,
    tracked_source_total: int = 0,
    inline_image: InlineImage = images.fetch_and_inline,
    reference_date: Optional[date] = None,
) -> LedgerView:
    """Adapt a Stage-6 batch into the ledger's view models.

    Splits the batch into its YouTube and X halves (preserving rank order), builds the
    rows and chips for each, and derives the dateline and tallies through
    :mod:`lib.render` so the ledger, the Tiles page and the markdown twin all report the
    same numbers for the same run.

    Args:
        tiered_items: The Stage-6 output (tiered, rank-ordered).
        sections_by_video: The Stage-6.5 ``{video_id: [Section]}`` map. None/absent ids
            render as title-only rows (graceful degradation — never a placeholder line).
        tracked_source_total: Total sources Orbit watches, for the masthead. 0 degrades
            to the posted count (see :func:`lib.render._masthead_counts`).
        inline_image: The image seam; defaults to the disk-cached real fetcher.
        reference_date: The dateline's date (defaults to today, UTC). Injectable for tests.

    Returns:
        The fully-derived :class:`LedgerView`.

    Example:
        >>> view = build_ledger_view([], inline_image=lambda url: None)
        >>> view.counts.item_count, view.videos, view.posts
        (0, [], [])
    """
    youtube_items, x_items = render.split_youtube_and_x(tiered_items)
    tracked_total, posted_count, item_count = render._masthead_counts(tiered_items, tracked_source_total)

    return LedgerView(
        dateline=render._format_dateline(reference_date),
        counts=LedgerCounts(
            tracked_total=tracked_total,
            posted_count=posted_count,
            item_count=item_count,
        ),
        videos=build_videos(
            youtube_items,
            sections_by_video=sections_by_video or {},
            inline_image=inline_image,
        ),
        channels=build_channels(youtube_items),
        posts=build_posts(x_items, inline_image=inline_image),
        handles=build_handles(x_items, inline_image=inline_image),
    )
