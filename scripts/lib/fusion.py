"""Orbit-specific overlap fusion: short-merge vs long-cross-link (design decision 7).

This is the rule that makes Orbit's clustering DIFFERENT from the reference's uniform
clustering (last30days/cluster.py merges everything into one body). Orbit's brief
(design decision 7, "never shred a long-form episode") forbids absorbing a long-form
episode into a cluster body. So fusion splits an overlapping topic group into:

  * **Short members** (tweets, short videos) — MERGED into the cluster body. They
    become the "Everyone's talking about" block.
  * **Long-form members** (duration > 1200s) — NEVER merged. Each stays its own
    separate item and is attached to the cluster as a :class:`CrossLink` that CARRIES
    the episode's ``item_external_id`` plus its most-relevant chapter's deep-link +
    timestamp, so the never-shred deep-link SURVIVES into the cluster.

The relevant-chapter pick is deterministic (Rule 5): the chapter whose title is most
lexically similar to the cluster's representative text (via
:func:`lib.dedupe.hybrid_similarity`), falling back to the episode's FIRST chapter
when nothing matches — so a long-form episode with chapters always contributes a real
deep-link, never an invented one.

:mod:`lib.cluster` owns the grouping (greedy + entity second pass); this module owns
only the short/long split + cross-link construction. No network, no LLM (Rule 5).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Make ``lib`` importable whether imported as the package member ``lib.fusion`` (via
# orbit.py's sys.path insert of the scripts dir) or run from the scripts dir directly.
# Mirrors rerank.py / chapterize.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import dedupe, log  # noqa: E402  (import must follow the sys.path inserts above)


@dataclass
class CrossLink:
    """A cluster's never-shred link INTO one long-form episode's relevant chapter.

    A long-form episode (duration > 1200s) is never absorbed into a cluster body
    (design decision 7). Instead the cluster references it through this cross-link,
    which carries the deep-link to the chapter most relevant to the cluster's topic —
    so a viewer of the "Everyone's talking about" block can jump straight to the
    moment in the episode that the short reactions are about.

    Attributes:
        episode_item_id: The long-form episode's ``RankableItem.item_external_id``
            (the stable id — the episode REMAINS its own separate item in the stream).
        chapter_title: The relevant chapter's title (for display next to the link).
        chapter_start_seconds: The chapter's start offset in seconds — the timestamp
            the deep-link drops into. Traces back to a real :class:`lib.chapterize.Chapter`.
        chapter_deep_link: The ``watch?v=ID&t=Ns`` (or source) deep-link straight to
            the moment. This is the deep-link that MUST survive (never-shred intent).
    """

    episode_item_id: str
    chapter_title: str
    chapter_start_seconds: float
    chapter_deep_link: str


@dataclass
class TopicGroup:
    """A pre-fusion group of items the clusterer judged to share a topic.

    The clustering pass (:mod:`lib.cluster`) emits these; fusion then splits each into
    short members (merged into the body) and long-form members (cross-linked).

    Attributes:
        member_items: The :class:`lib.rerank.RankableItem`s grouped together.
        representative_text: The text used to pick each long-form episode's most
            relevant chapter (normally the representative item's title).
    """

    member_items: list[Any]
    representative_text: str = ""


def _pick_relevant_chapter(representative_text: str, chapters: list[Any]) -> Any | None:
    """Pick the chapter most lexically relevant to the cluster topic (deterministic).

    Scores each chapter's ``title`` against ``representative_text`` with
    :func:`lib.dedupe.hybrid_similarity` and returns the best. When no chapter title
    shares any lexical signal (all-zero), falls back to the FIRST chapter so a
    long-form episode with chapters always yields a real deep-link (never invented).

    Args:
        representative_text: The cluster's representative text (a title / tweet body).
        chapters: The episode's :class:`lib.chapterize.Chapter` list (non-empty here).

    Returns:
        The most relevant chapter, the first chapter on an all-zero tie, or None when
        ``chapters`` is empty.

    Example:
        >>> from types import SimpleNamespace
        >>> ch_a = SimpleNamespace(title="Intro", start_seconds=0.0, deep_link="u0")
        >>> ch_b = SimpleNamespace(title="Apple M5 chip review", start_seconds=90.0, deep_link="u90")
        >>> _pick_relevant_chapter("apple m5 chip", [ch_a, ch_b]).start_seconds
        90.0
    """
    if not chapters:
        return None
    best_chapter = chapters[0]
    best_similarity = -1.0
    for chapter in chapters:
        chapter_title = str(getattr(chapter, "title", "") or "")
        similarity = dedupe.hybrid_similarity(representative_text, chapter_title)
        if similarity > best_similarity:
            best_similarity = similarity
            best_chapter = chapter
    return best_chapter


def _cross_link_for_episode(episode_item: Any, representative_text: str) -> CrossLink | None:
    """Build the :class:`CrossLink` into a long-form episode's relevant chapter.

    Args:
        episode_item: The long-form :class:`lib.rerank.RankableItem` (has ``chapters``).
        representative_text: The cluster's representative text for chapter relevance.

    Returns:
        A cross-link carrying the chapter deep-link, or None when the episode has no
        chapters at all (then there is no real deep-link to carry — we never invent one).
    """
    chapters = list(getattr(episode_item, "chapters", []) or [])
    chapter = _pick_relevant_chapter(representative_text, chapters)
    if chapter is None:
        log.log_warning(
            "fusion_long_form_without_chapters",
            episode_item_id=str(getattr(episode_item, "item_external_id", "")),
            fix_suggestion=(
                "long-form episode reached fusion with no chapters; it stays a separate "
                "item but contributes no cross-link deep-link. Confirm Phase-2 chapterized it."
            ),
        )
        return None
    return CrossLink(
        episode_item_id=str(getattr(episode_item, "item_external_id", "")),
        chapter_title=str(getattr(chapter, "title", "") or ""),
        chapter_start_seconds=float(getattr(chapter, "start_seconds", 0.0) or 0.0),
        chapter_deep_link=str(getattr(chapter, "deep_link", "") or ""),
    )


def fuse_topic_group(
    group: TopicGroup,
    is_long_form: Callable[[Any], bool],
) -> tuple[list[Any], list[CrossLink]]:
    """Split a topic group into merged short members and cross-linked long-form members.

    The Orbit rule (design decision 7): short items merge into the cluster body; each
    long-form item stays separate and is attached as a :class:`CrossLink` carrying its
    relevant chapter deep-link.

    Args:
        group: The pre-fusion :class:`TopicGroup` from the clustering pass.
        is_long_form: Predicate deciding whether an item is a long-form episode (and
            so must NOT be merged). Injected so the long/short distinction is explicit
            and testable.

    Returns:
        A ``(short_members, cross_links)`` tuple — the short items that form the
        cluster body, and the cross-links into each long-form episode (one per
        episode that carries chapters).

    Example:
        >>> from types import SimpleNamespace
        >>> short = SimpleNamespace(item_external_id="t1", title="apple m5 is wild", chapters=[])
        >>> ch = SimpleNamespace(title="M5 chip", start_seconds=30.0, deep_link="u30")
        >>> ep = SimpleNamespace(item_external_id="v1", title="Apple M5 deep dive", chapters=[ch])
        >>> grp = TopicGroup(member_items=[short, ep], representative_text="apple m5")
        >>> members, links = fuse_topic_group(grp, is_long_form=lambda i: bool(i.chapters))
        >>> [m.item_external_id for m in members]
        ['t1']
        >>> links[0].episode_item_id
        'v1'
    """
    short_members: list[Any] = []
    cross_links: list[CrossLink] = []
    for item in group.member_items:
        if is_long_form(item):
            cross_link = _cross_link_for_episode(item, group.representative_text)
            if cross_link is not None:
                cross_links.append(cross_link)
        else:
            short_members.append(item)
    return short_members, cross_links
