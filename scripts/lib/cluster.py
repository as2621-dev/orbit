"""Overlap clustering for the unified YouTube+X stream (Phase 5 / Stage 4).

Adapts last30days/cluster.py's LEXICAL clustering (the algorithm, NOT the imports —
the reference's ``from . import dedupe, schema`` is replaced: similarity primitives
live in :mod:`lib.dedupe`, the item model is :class:`lib.rerank.RankableItem`) and
layers Orbit's design-decision-7 fusion (:mod:`lib.fusion`) on top.

The pipeline (Rule 5 — 100% deterministic lexical math, NO embedding model, NO
network, NO LLM):

  1. **Greedy single-leader pass** — each item joins the first existing group whose
     leader it is similar enough to (:func:`lib.dedupe.prepared_similarity` >=
     :data:`SIMILARITY_THRESHOLD`), else it founds a new group. Items are processed in
     a STABLE order (by ``item_external_id``) so grouping is reproducible run-to-run.
  2. **Entity-overlap second pass** — small groups whose extracted entities overlap
     enough (overlap coefficient ``|A ∩ B| / min(|A|,|B|)`` >=
     :data:`ENTITY_OVERLAP_THRESHOLD`) are merged. This is exactly the tool that
     matches a SHORT tweet reaction ("M5 is insane") against a LONG video title
     ("Apple M5 chip — full technical deep dive"): the tweet shares few total words
     with the title, but its few entities are a subset of the title's, so the overlap
     coefficient (normalized by the SMALLER set) is high.
  3. **Fusion** — each merged topic group is split by :func:`lib.fusion.fuse_topic_group`
     into short members (the cluster body, the "Everyone's talking about" block) and
     cross-links into each long-form episode's relevant chapter (never-shred).

:func:`cluster_overlaps` is the single entry point. Sub-phase 2 consumes its
``list[Cluster]`` to compute internal trending.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Make ``lib`` importable whether imported as the package member ``lib.cluster`` (via
# orbit.py's sys.path insert of the scripts dir) or run from the scripts dir directly.
# Mirrors rerank.py / chapterize.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import dedupe, fusion, log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.fusion import CrossLink, TopicGroup  # noqa: E402

# --- Tunable thresholds (the clustering surface; first-cut values) -----------
# Greedy-pass similarity: an item joins a group when its prepared_similarity to the
# group leader meets this. Matches the reference's non-breaking-news default (0.48).
# Orbit does not branch on intent (the reference's breaking_news 0.42), so one value.
SIMILARITY_THRESHOLD: float = 0.48

# Entity-overlap second-pass threshold (overlap coefficient |A∩B|/min). Lifted from
# the reference's 0.45 — the value tuned for short-vs-long cross-source matching.
ENTITY_OVERLAP_THRESHOLD: float = 0.45

# Only groups with at most this many members are eligible for the entity second-pass
# merge (don't fold an already-large group into another on entity overlap alone).
ENTITY_MERGE_MAX_GROUP_SIZE: int = 3

# Words too common to signal a shared topic between groups. Lifted from the
# reference's _ENTITY_STOPWORDS (URL fragments kept — tweets carry links).
_ENTITY_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "for",
        "how",
        "is",
        "in",
        "of",
        "on",
        "and",
        "with",
        "from",
        "by",
        "at",
        "this",
        "that",
        "it",
        "what",
        "are",
        "do",
        "can",
        "his",
        "her",
        "he",
        "she",
        "its",
        "was",
        "has",
        "new",
        "just",
        "says",
        "said",
        "will",
        "about",
        "after",
        "now",
        "all",
        "been",
        "here",
        "not",
        "out",
        "up",
        "more",
        "also",
        "but",
        "who",
        "year",
        "first",
        "make",
        "being",
        "making",
        "over",
        "into",
        "than",
        "they",
        "their",
        "would",
        "could",
        "get",
        "got",
        "some",
        "like",
        "back",
        "going",
        "breaking",
        "https",
        "http",
        "www",
        "com",
    }
)


@dataclass
class Cluster:
    """One overlap cluster over the unified YouTube+X stream — the M3 unit.

    Short items on a shared topic merge into the ``member_item_ids`` body (the
    "Everyone's talking about" block); long-form episodes on the same topic are NEVER
    merged — they stay separate items and are referenced through ``cross_links``,
    each carrying a chapter deep-link (design decision 7, never-shred).

    Sub-phase 2 consumes a ``list[Cluster]`` to compute internal trending: cluster
    SIZE (``len(member_item_ids)``) and ``source_diversity`` are its velocity proxies.

    Attributes:
        cluster_id: Stable cluster id (``"cluster-N"``, N in deterministic order).
        member_item_ids: The SHORT members' ``item_external_id``s that merged into
            this cluster body (may be empty for a long-form-only topic — then the
            cluster exists purely to carry cross-links to separate episodes).
        representative_item_id: The ``item_external_id`` chosen to represent the
            cluster (the highest-priority short member, else the first cross-linked
            episode). Drives the block's headline/link.
        cross_links: Links into each long-form episode on this topic, each carrying
            the episode id + its relevant chapter's deep-link + timestamp (a
            :class:`lib.fusion.CrossLink`). The never-shred deep-links survive HERE.
        source_diversity: The count of DISTINCT ``creator_external_id`` across all
            members (short members + cross-linked episodes). A proxy for "how many
            different people in your network are converging on this" — Sub-phase 2's
            convergence/velocity signal (3 creators on one topic > 1 creator).
    """

    cluster_id: str
    member_item_ids: list[str]
    representative_item_id: str
    cross_links: list[CrossLink] = field(default_factory=list)
    source_diversity: int = 0
    # ALL members of the topic group (short + long-form), by item_external_id, in stable
    # order. Unlike ``member_item_ids`` (short-merged body only), this is the complete
    # membership the crown-winners stage ranks to pick ONE winner per topic and footnote
    # the rest — independent of the short/long fusion split, so it is correct even when
    # long-form videos reach clustering without chapters (the post-reorder pipeline).
    all_member_item_ids: list[str] = field(default_factory=list)
    # Set by the crown-winners stage (empty until then): the crowned member's id, and the
    # non-winner members folded under it as footnote links.
    winner_item_id: str = ""
    footnote_item_ids: list[str] = field(default_factory=list)


def _item_text(item: Any) -> str:
    """Cluster text for an item: its title plus any cheap chapter titles.

    For an X tweet ``title`` IS the tweet body; for a YouTube item it is the video
    title. Chapter titles (when present on a long-form item) are appended cheaply so
    the entity second pass has more surface to match a short reaction against — the
    title remains the core lexical signal.

    Args:
        item: A :class:`lib.rerank.RankableItem` (read for ``title`` / ``chapters``).

    Returns:
        The concatenated cluster text, stripped.
    """
    parts: list[str] = [str(getattr(item, "title", "") or "")]
    for chapter in getattr(item, "chapters", None) or []:
        chapter_title = str(getattr(chapter, "title", "") or "")
        if chapter_title:
            parts.append(chapter_title)
    return " ".join(part for part in parts if part).strip()


def _extract_entities(text: str) -> set[str]:
    """Extract significant words (capitalized, ALL-CAPS, digit-bearing, or 4+ chars).

    Lifted from the reference: the cross-source matcher's vocabulary. Stopwords and
    words of length <= 2 are dropped. Used by the entity second pass to match a short
    tweet reaction against a long video title even when total phrasing differs.

    Args:
        text: Raw cluster text.

    Returns:
        The lower-cased set of significant entity words.

    Example:
        >>> sorted(_extract_entities("Apple chip benchmark review 2026"))
        ['2026', 'apple', 'benchmark', 'chip', 'review']
    """
    words = re.sub(r"[^\w\s]", " ", text).split()
    entities: set[str] = set()
    for word in words:
        lower = word.lower()
        if lower in _ENTITY_STOPWORDS or len(word) <= 2:
            continue
        if word[0].isupper() or word.isupper() or any(character.isdigit() for character in word) or len(word) >= 4:
            entities.add(lower)
    return entities


def _entity_overlap(entities_a: set[str], entities_b: set[str]) -> float:
    """Overlap coefficient ``|A ∩ B| / min(|A|, |B|)`` of two entity sets.

    NOT Jaccard: normalizing by the SMALLER set is what lets a short tweet (few
    entities) score high against a long title (many entities) when the tweet's
    entities are a subset — the short-vs-long cross-source matcher.

    Args:
        entities_a: First entity set.
        entities_b: Second entity set.

    Returns:
        The overlap coefficient in ``[0.0, 1.0]``. ``0.0`` when either set is empty.

    Example:
        >>> _entity_overlap({"apple", "m5"}, {"apple", "m5", "chip", "review"})
        1.0
    """
    if not entities_a or not entities_b:
        return 0.0
    intersection = entities_a & entities_b
    smaller = min(len(entities_a), len(entities_b))
    return len(intersection) / smaller if smaller > 0 else 0.0


def _greedy_groups(items: list[Any]) -> list[list[Any]]:
    """Greedy single-leader clustering: each item joins the first similar-enough group.

    Items are compared (via cached :class:`lib.dedupe.PreparedText`) against each
    existing group's LEADER (its first member); the first whose similarity meets
    :data:`SIMILARITY_THRESHOLD` adopts the item, else the item founds a new group.

    Args:
        items: The items to group (already in a stable order).

    Returns:
        The list of groups, each a non-empty list of items.
    """
    prepared_by_id: dict[str, dedupe.PreparedText] = {
        str(item.item_external_id): dedupe.PreparedText(_item_text(item)) for item in items
    }
    groups: list[list[Any]] = []
    for item in items:
        item_prepared = prepared_by_id[str(item.item_external_id)]
        assigned = False
        for group in groups:
            leader = group[0]
            similarity = dedupe.prepared_similarity(item_prepared, prepared_by_id[str(leader.item_external_id)])
            if similarity >= SIMILARITY_THRESHOLD:
                group.append(item)
                assigned = True
                break
        if not assigned:
            groups.append([item])
    return groups


def _merge_entity_groups(groups: list[list[Any]]) -> list[list[Any]]:
    """Second pass: merge small groups whose extracted entities overlap enough.

    Catches cross-source / cross-phrasing overlaps the lexical greedy pass missed —
    notably a short tweet reaction vs a long video title. Only groups of at most
    :data:`ENTITY_MERGE_MAX_GROUP_SIZE` members are eligible (don't fold a large group
    into another on entity overlap alone). A later group is merged into the FIRST
    earlier eligible group it overlaps (overlap coefficient >=
    :data:`ENTITY_OVERLAP_THRESHOLD`).

    Args:
        groups: The greedy-pass groups (stable order).

    Returns:
        The merged groups, preserving the order of surviving leaders.
    """
    if len(groups) < 2:
        return groups

    group_entities: list[set[str]] = []
    for group in groups:
        pooled: set[str] = set()
        for item in group:
            pooled |= _extract_entities(_item_text(item))
        group_entities.append(pooled)

    merged_into: dict[int, int] = {}  # later-group index -> earlier-group target index
    for earlier_index in range(len(groups)):
        if earlier_index in merged_into or len(groups[earlier_index]) > ENTITY_MERGE_MAX_GROUP_SIZE:
            continue
        for later_index in range(earlier_index + 1, len(groups)):
            if later_index in merged_into or len(groups[later_index]) > ENTITY_MERGE_MAX_GROUP_SIZE:
                continue
            overlap = _entity_overlap(group_entities[earlier_index], group_entities[later_index])
            if overlap >= ENTITY_OVERLAP_THRESHOLD:
                merged_into[later_index] = earlier_index

    if not merged_into:
        return groups

    result: list[list[Any]] = []
    for index, group in enumerate(groups):
        if index in merged_into:
            continue
        absorbed = [later for later, target in merged_into.items() if target == index]
        combined = list(group)
        for later_index in absorbed:
            combined.extend(groups[later_index])
        result.append(combined)
    return result


def _representative_priority(item: Any, config: Any) -> float:
    """The item's creator priority_weight (for picking a cluster representative).

    Defaults to ``1.0`` (neutral) when config is None or the creator is absent, so
    representative selection never crashes on a missing weight.

    Args:
        item: A :class:`lib.rerank.RankableItem`.
        config: An :class:`lib.config.OrbitConfig`-shaped object, or None.

    Returns:
        The creator's priority weight as a float (``1.0`` neutral default).
    """
    creator_weights = getattr(config, "creator_weights", {}) if config is not None else {}
    creator_weights = creator_weights or {}
    raw_weight = creator_weights.get(str(getattr(item, "creator_external_id", "")), 1.0)
    try:
        return float(raw_weight)
    except (TypeError, ValueError):
        return 1.0


def _is_long_form_by_chapters(item: Any) -> bool:
    """Default long-form predicate: an item is long-form when it carries chapters.

    First-cut heuristic (documented for sub-phases 2/4): :class:`lib.rerank.RankableItem`
    does NOT currently carry ``duration`` / ``is_long_form``, and rerank.py is
    out-of-bounds for this sub-phase. Phase-2 chapterizes ONLY long-form videos
    (``chapterize_episode`` returns ``[]`` for anything <= 1200s), so a non-empty
    ``chapters`` list is a reliable long-form proxy: short videos and tweets have no
    chapters. A future explicit ``duration``/``is_long_form`` field on RankableItem
    would be cleaner — see the sub-phase 1 handoff note.

    Args:
        item: A :class:`lib.rerank.RankableItem`.

    Returns:
        True when the item carries at least one chapter.
    """
    return bool(getattr(item, "chapters", None))


def cluster_overlaps(
    items: list[Any],
    config: Any = None,
    *,
    is_long_form: Callable[[Any], bool] = _is_long_form_by_chapters,
) -> list[Cluster]:
    """Cluster the unified YouTube+X stream — short-merge, long-cross-link (decision 7).

    Source-agnostic (works on YouTube uploads and X tweets uniformly via
    :class:`lib.rerank.RankableItem`). Deterministic (Rule 5): no LLM, no network, no
    embedding model — pure lexical similarity + entity overlap, with items processed
    in a stable ``item_external_id`` order so the output is reproducible.

    Steps: greedy single-leader grouping (:func:`_greedy_groups`) -> entity-overlap
    second-pass merge (:func:`_merge_entity_groups`) -> per-group fusion
    (:func:`lib.fusion.fuse_topic_group`) splitting short members (merged into the
    cluster body) from long-form episodes (attached as cross-links carrying chapter
    deep-links).

    A cluster's ``representative_item_id`` is the highest-priority SHORT member, or —
    when the topic is long-form-only — the first cross-linked episode's id, so a
    long-form-only topic still has a representative. ``source_diversity`` counts
    distinct ``creator_external_id`` across short members AND cross-linked episodes.

    Args:
        items: The unified :class:`lib.rerank.RankableItem` stream to cluster.
        config: An :class:`lib.config.OrbitConfig`-shaped object (read only for
            ``creator_weights``, to pick the highest-priority representative), or None
            (then representatives are picked by stable id order).
        is_long_form: Predicate deciding whether an item is a long-form episode (and
            so must be cross-linked, not merged). Defaults to
            :func:`_is_long_form_by_chapters` (chapters present == long-form). Tests
            inject an explicit predicate for a deterministic long/short distinction.

    Returns:
        The clusters in deterministic order (by descending member count, then
        ``representative_item_id``). Empty input returns ``[]``.

    Example:
        >>> from types import SimpleNamespace
        >>> def tweet(tid, text):
        ...     return SimpleNamespace(
        ...         item_external_id=tid, title=text, creator_external_id=tid, chapters=[]
        ...     )
        >>> a = tweet("t1", "Apple M5 chip is insane")
        >>> b = tweet("t2", "Apple M5 chip is insane")
        >>> clusters = cluster_overlaps([a, b], is_long_form=lambda i: bool(i.chapters))  # doctest: +SKIP
        >>> len(clusters[0].member_item_ids)  # doctest: +SKIP
        2
    """
    if not items:
        log.log_info("cluster_overlaps_completed", cluster_count=0, item_count=0)
        return []

    # Reason: process in a STABLE order (by id) so greedy grouping is reproducible —
    # otherwise the leader of each group (and thus the grouping) would depend on input
    # order and flap run-to-run, confusing the user.
    ordered_items = sorted(items, key=lambda item: str(item.item_external_id))

    greedy = _greedy_groups(ordered_items)
    merged = _merge_entity_groups(greedy)

    clusters: list[Cluster] = []
    for group in merged:
        representative_text = _item_text(max(group, key=lambda item: _representative_priority(item, config)))
        topic_group = TopicGroup(member_items=group, representative_text=representative_text)
        short_members, cross_links = fusion.fuse_topic_group(topic_group, is_long_form)

        if short_members:
            representative = max(short_members, key=lambda item: _representative_priority(item, config))
            representative_item_id = str(representative.item_external_id)
        elif cross_links:
            # Reason: a long-form-only topic has no short body — represent it by the
            # first cross-linked episode so the cluster still has a headline/link.
            representative_item_id = cross_links[0].episode_item_id
        else:
            # Reason: a long-form member without chapters yields no cross-link and is
            # not a short member; fall back to the group's first item id so the cluster
            # is never representative-less.
            representative_item_id = str(group[0].item_external_id)

        member_item_ids = [str(item.item_external_id) for item in short_members]
        all_member_item_ids = [str(item.item_external_id) for item in group]
        distinct_creators = {str(getattr(item, "creator_external_id", "")) for item in group}
        clusters.append(
            Cluster(
                cluster_id="",  # assigned after the deterministic sort below
                member_item_ids=member_item_ids,
                representative_item_id=representative_item_id,
                cross_links=cross_links,
                source_diversity=len(distinct_creators),
                all_member_item_ids=all_member_item_ids,
            )
        )

    # Reason: order clusters by descending body size then representative id for a
    # stable, meaningful order (busiest "everyone's talking about" block first), then
    # assign cluster ids in that final order so ids are deterministic.
    clusters.sort(key=lambda cluster: (-len(cluster.member_item_ids), cluster.representative_item_id))
    for index, cluster in enumerate(clusters, start=1):
        cluster.cluster_id = f"cluster-{index}"

    log.log_info(
        "cluster_overlaps_completed",
        cluster_count=len(clusters),
        item_count=len(items),
        cross_linked_episode_count=sum(len(cluster.cross_links) for cluster in clusters),
    )
    return clusters
