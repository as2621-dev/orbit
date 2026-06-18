"""Internal-network trending — baseline-relative velocity (Phase 5 / Stage 5a).

This is BUILT on top of Sub-phase 1's clusters and the static per-item primitives in
:mod:`lib.signals` — the reference has NO time-windowed velocity / baseline logic, so
velocity is constructed here. The brief's core Stage-5 distinction is that trending is
BASELINE-RELATIVE, not raw popularity: an item spiking far above ITS creator's own
normal is "trending" even if a different item has higher raw engagement that is merely
normal-for-its-creator.

:func:`compute_internal_trending` measures network velocity two ways, combined into one
``velocity_score`` per cluster:

  1. **Convergence / cluster-size velocity** — multiple FOLLOWED creators converging on
     the same cluster. A cluster's :attr:`lib.cluster.Cluster.source_diversity` (distinct
     ``creator_external_id`` across its short members + cross-linked episodes) is the
     velocity proxy: 3 different people in your network on one topic is more "trending"
     than 1 person, at EQUAL raw engagement.
  2. **Baseline-relative spike** — a single item spiking relative to the CREATOR'S OWN
     baseline (engagement vs the creator's recent median), via :func:`lib.rerank.log1p_safe`
     compression + the per-creator reference normalization in
     :func:`lib.signals.baseline_relative_ratio`. An item at 5× its creator's own normal
     ranks as trending while an item at the creator's normal level does NOT — EVEN IF the
     normal-level item has higher RAW engagement. This is the core Stage-5 distinction.

**The TIME dimension and the engagement-baseline source (documented design + fallback):**
``store.py``'s ``seen`` table stores only ``first_seen_at`` per ``source_id`` +
``item_external_id`` — it carries NO historical engagement snapshots. So the store gives
the TIME dimension (how many prior items a creator has been seen with = their history
DEPTH, the dormancy signal Sub-phase 4 needs) but NOT historical engagement values.
Therefore the engagement baseline a spike is measured against comes from the BATCH — the
per-creator MEDIAN of :func:`lib.rerank.engagement_blend`, exactly the
:func:`lib.rerank.compute_creator_engagement_baselines` idea (a median resists a single
viral outlier skewing "normal"). A creator with one item in the batch has a baseline
equal to that item's blend (spike ratio ~1.0 — neutral, never a false spike). This is
the brief's "where the batch lacks history, fall back to the batch-median baseline" path.
When a true historical engagement store exists, the ``creator_baselines`` arg accepts a
precomputed map without touching this function.

``store_module`` is INJECTABLE (defaults to :mod:`store`): tests pass a fake/temp-DB
store so this never hits a real user DB. It is read ONLY through
:func:`lib.store.list_sources` + :func:`lib.store.get_seen_ids` to derive each creator's
``history_sample_count`` (their ``seen`` history depth). No engagement is read from it.

Sub-phases 3 & 4 build on this module's :class:`TrendingItem` from the sibling module
:mod:`lib.external_trending` (kept separate for the project's file-size discipline):
``external_trending.tag_external_corroboration`` (reading
:class:`TrendingItem.velocity_score` + components to tag corroboration vs scoop) and
``external_trending.detect_scoops`` (reading ``history_sample_count`` for the dormancy
signal + ``baseline_relative_ratio`` for acceleration) plus the rerank-multiplier
bridge. :class:`TrendingItem` is shaped to carry the components + render hooks both
need, so they extend rather than rewrite.

Rule 5 — 100% deterministic math. NO LLM, NO network, NO new pip dependency.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Make ``lib`` importable whether imported as the package member ``lib.trending``
# (via orbit.py's sys.path insert of the scripts dir) or run from the scripts dir
# directly. Mirrors rerank.py / cluster.py / fusion.py / signals.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (default injectable store module; import follows sys.path inserts)
from lib import log, signals  # noqa: E402  (import must follow the sys.path inserts above)
from lib.rerank import RankableItem, compute_creator_engagement_baselines, engagement_blend  # noqa: E402

# --- Tunable weights (the trending surface; first-cut values) ----------------
# Kept at module top per the brief so the maintainer tunes the trending shape here.
# These are first-cut values; real-network usage tunes them.

# How strongly the baseline-relative spike (item engagement vs the creator's OWN
# batch-median baseline) drives the velocity score. This term DOMINATES the raw
# engagement scale by construction — it is a RATIO against the creator's own normal, so
# a 5×-baseline item scores ~5 here regardless of absolute count, while a
# normal-for-its-creator item scores ~1 even if its raw counts are larger. This is the
# core Stage-5 distinction (baseline-relative, NOT raw popularity).
SPIKE_WEIGHT: float = 1.0

# How strongly convergence (distinct followed creators on one cluster) adds to the
# score. A per-extra-creator additive bump: a cluster touched by N distinct creators
# gets (N - 1) * CONVERGENCE_PER_CREATOR added, so a 3-creator cluster outranks a
# 1-creator cluster of EQUAL baseline-relative engagement (DoD #1). Kept additive (not
# multiplicative) so convergence cannot, on its own, flip the baseline-relative ordering
# between two SINGLE-creator items (DoD #2).
CONVERGENCE_PER_CREATOR: float = 0.5

# The neutral spike ratio a cluster with no scorable representative engagement falls
# back to (mirrors signals.NEUTRAL_SPIKE_RATIO) — a quiet cluster is neither rewarded
# nor crashed (Rule 12).
_NEUTRAL_SPIKE: float = signals.NEUTRAL_SPIKE_RATIO


@dataclass
class TrendingItem:
    """One ranked internal-trending entry for the right-rail (the Stage-5a unit).

    Carries the cluster reference, the composite velocity score, the decomposed
    components, AND the render hooks (title + card_url) — so Sub-phase 3 can tag
    corroboration/scoop off the components and Sub-phase 4 can render the right-rail +
    detect scoops without recomputing. This contract is what downstream sub-phases
    consume; the field names are stable.

    Attributes:
        item_external_id: The representative item's stable id (the headline the
            right-rail links to / the item whose spike was measured). Empty only when
            the cluster maps to no known item.
        cluster_id: The source :class:`lib.cluster.Cluster`'s id.
        creator_external_id: The representative item's creator (empty when unknown).
        title: The representative item's title — the right-rail headline text.
        card_url: The representative item's card/deep-link URL hook for the right-rail
            (the X permalink, or "" for the YouTube ``watch?v=ID`` fallback the renderer
            fills). Empty when the cluster has no resolvable representative.
        velocity_score: The composite velocity score (higher == more trending). The
            ranked output is sorted DESCENDING by this. Equals
            ``spike_weight * baseline_relative_ratio + convergence_bonus``.
        convergence_count: The cluster's ``source_diversity`` — distinct followed
            creators converging on this topic (the convergence velocity proxy).
        baseline_relative_ratio: The representative item's engagement vs the creator's
            OWN baseline (``> 1.0`` == above normal, ``~1.0`` == normal, ``< 1.0`` ==
            below). The core Stage-5 baseline-relative signal — NOT raw popularity.
        creator_baseline_median: The creator's batch-median engagement blend the spike
            was measured against (the baseline source). None when the creator had no
            usable baseline (then the ratio degraded to neutral).
        history_sample_count: How many prior items the creator has in the ``seen``
            history (read from the injected store). Sub-phase 4 reads this for the
            dormancy signal — a normally-dormant account has a LOW count + a high
            ``baseline_relative_ratio`` (suddenly accelerating). 0 == no store history.
        corroboration_tag: Filled by Sub-phase 3 (``"corroborated"`` / ``"scoop"`` /
            ``""`` until tagged). Reserved here so Sub-phase 3 extends, not rewrites.
        is_scoop: Filled by Sub-phase 4's ``detect_scoops``. Reserved here.
    """

    item_external_id: str
    cluster_id: str
    creator_external_id: str
    title: str
    card_url: str
    velocity_score: float
    convergence_count: int
    baseline_relative_ratio: float
    creator_baseline_median: Optional[float] = None
    history_sample_count: int = 0
    corroboration_tag: str = ""
    is_scoop: bool = False


def _representative_item(cluster: Any, items_by_id: dict[str, RankableItem]) -> Optional[RankableItem]:
    """Resolve the cluster's representative :class:`lib.rerank.RankableItem`, or None.

    Prefers the cluster's ``representative_item_id``; falls back to its short
    ``member_item_ids`` in order, then its cross-linked episode ids, so a long-form-only
    cluster (no short body) still resolves a representative. Returns None only when
    nothing in the cluster maps to a known item (a defensive degrade, Rule 12).

    Args:
        cluster: A :class:`lib.cluster.Cluster`.
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem`.

    Returns:
        The representative item, or None when no cluster id maps to a known item.

    Example:
        >>> from types import SimpleNamespace
        >>> item = RankableItem("a", "t", "c", "UC1", 1, 1, 1, "20260101")
        >>> cluster = SimpleNamespace(representative_item_id="a", member_item_ids=["a"], cross_links=[])
        >>> _representative_item(cluster, {"a": item}).item_external_id
        'a'
    """
    candidate_ids: list[str] = []
    representative_id = str(getattr(cluster, "representative_item_id", "") or "")
    if representative_id:
        candidate_ids.append(representative_id)
    candidate_ids.extend(str(member_id) for member_id in getattr(cluster, "member_item_ids", []) or [])
    for cross_link in getattr(cluster, "cross_links", []) or []:
        episode_id = str(getattr(cross_link, "episode_item_id", "") or "")
        if episode_id:
            candidate_ids.append(episode_id)
    for candidate_id in candidate_ids:
        if candidate_id in items_by_id:
            return items_by_id[candidate_id]
    return None


def compute_history_sample_counts(items_by_id: dict[str, RankableItem], store_module: Any = store) -> dict[str, int]:
    """Derive each creator's ``seen``-history depth from the injected store (the TIME dim).

    The ``seen`` table carries only ``first_seen_at`` (no engagement) — so what the store
    contributes is HOW MANY prior items a creator has been seen with, i.e. their history
    DEPTH. This is the dormancy signal Sub-phase 4's scoop detection consumes (a
    normally-dormant account has a LOW depth + a high spike). We resolve each creator's
    ``source_id`` via :func:`lib.store.list_sources` (matching on ``external_id``) and
    count its ``seen`` rows via :func:`lib.store.get_seen_ids`.

    Reads ONLY ``list_sources`` + ``get_seen_ids`` — never engagement. ``store_module`` is
    injectable so tests pass a fake/temp-DB store and this never hits a real user DB. Any
    store read that raises degrades that creator to 0 history (Rule 12 — observability,
    not a crash).

    Args:
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem`; its
            distinct ``creator_external_id``s are the creators to look up.
        store_module: The store module/object exposing ``list_sources()`` and
            ``get_seen_ids(source_id)``. Defaults to :mod:`store`; tests inject a fake.

    Returns:
        A map of ``creator_external_id`` -> seen-history item count (0 when the creator
        is absent from ``sources`` or the store read failed).

    Example:
        >>> from types import SimpleNamespace
        >>> fake = SimpleNamespace(
        ...     list_sources=lambda: [{"source_id": 1, "external_id": "UC1"}],
        ...     get_seen_ids=lambda source_id: {"a", "b", "c"},
        ... )
        >>> item = RankableItem("a", "t", "c", "UC1", 1, 1, 1, "20260101")
        >>> compute_history_sample_counts({"a": item}, fake)
        {'UC1': 3}
    """
    creators = {str(item.creator_external_id) for item in items_by_id.values() if item.creator_external_id}
    if not creators:
        return {}

    try:
        sources = store_module.list_sources()
    except Exception as exc:  # noqa: BLE001 — observability must not crash the trending run
        log.log_warning(
            "trending_history_sources_read_failed",
            error_message=str(exc),
            fix_suggestion="Confirm the store is initialized (store.init_db) and reachable; defaulting history depth to 0.",
        )
        return {creator: 0 for creator in creators}

    source_id_by_external: dict[str, int] = {}
    for source in sources:
        external_id = str(source.get("external_id", "") or "")
        if external_id:
            source_id_by_external[external_id] = int(source.get("source_id", 0) or 0)

    counts: dict[str, int] = {}
    for creator in creators:
        source_id = source_id_by_external.get(creator)
        if source_id is None:
            counts[creator] = 0
            continue
        try:
            counts[creator] = len(store_module.get_seen_ids(source_id))
        except Exception as exc:  # noqa: BLE001 — degrade this creator to 0, never crash
            log.log_warning(
                "trending_history_seen_read_failed",
                creator_external_id=creator,
                error_message=str(exc),
                fix_suggestion="Confirm the seen table exists; defaulting this creator's history depth to 0.",
            )
            counts[creator] = 0
    return counts


def compute_internal_trending(
    clusters: list[Any],
    items_by_id: dict[str, RankableItem],
    store_module: Any = store,
    *,
    creator_baselines: Optional[dict[str, float]] = None,
    spike_weight: float = SPIKE_WEIGHT,
    convergence_per_creator: float = CONVERGENCE_PER_CREATOR,
) -> list[TrendingItem]:
    """Rank clusters by internal-network velocity — convergence + baseline-relative spike.

    Deterministic (Rule 5): no LLM, no network, no embedding model. The velocity score
    for each cluster is::

        velocity_score = spike_weight * baseline_relative_ratio
                       + convergence_per_creator * max(convergence_count - 1, 0)

    where ``baseline_relative_ratio`` is the representative item's engagement vs the
    CREATOR'S OWN baseline (the core Stage-5 baseline-relative signal), and
    ``convergence_count`` is the cluster's ``source_diversity`` (distinct followed
    creators on the topic). The spike term is a per-creator RATIO so it dominates raw
    scale — a 5×-baseline item beats a normal-but-higher-raw item (DoD #2); convergence
    is an additive per-extra-creator bump so a 3-creator cluster outranks a 1-creator
    cluster of equal spike (DoD #1) WITHOUT letting convergence flip the baseline-relative
    ordering of two single-creator items.

    **Baseline source (documented):** the per-creator engagement baseline is the BATCH
    median of :func:`lib.rerank.engagement_blend` (via
    :func:`lib.rerank.compute_creator_engagement_baselines`) — because the ``seen`` store
    holds no engagement history (only ``first_seen_at``). Pass ``creator_baselines`` to
    override with a true historical map without touching this function. The injected
    ``store_module`` supplies only the TIME dimension (``history_sample_count`` — the
    creator's ``seen``-history depth) for Sub-phase 4's dormancy detection.

    Args:
        clusters: Sub-phase 1's :class:`lib.cluster.Cluster` list (its ``source_diversity``
            and ``representative_item_id`` are consumed).
        items_by_id: Map of ``item_external_id`` -> :class:`lib.rerank.RankableItem` for
            every item referenced by the clusters (so the representative's engagement is
            resolvable). Missing ids degrade to a neutral spike (Rule 12).
        store_module: The store module/object (``list_sources`` + ``get_seen_ids``) for
            the history-depth lookup. Defaults to :mod:`store`; tests inject a fake/temp-DB
            store so a real user DB is never touched.
        creator_baselines: Optional precomputed ``creator_external_id`` -> baseline-blend
            map (e.g. a true historical median). When None, the BATCH median over
            ``items_by_id`` is used (the documented fallback).
        spike_weight: Weight on the baseline-relative spike term (default
            :data:`SPIKE_WEIGHT`). Injectable for tuning/tests.
        convergence_per_creator: Additive bump per extra distinct creator (default
            :data:`CONVERGENCE_PER_CREATOR`). Injectable for tuning/tests.

    Returns:
        The :class:`TrendingItem`s sorted DESCENDING by ``velocity_score`` (ties break by
        ``cluster_id`` for a stable, deterministic order). Empty input -> ``[]`` (Rule 12
        — a quiet day never crashes).

    Example:
        >>> compute_internal_trending([], {})
        []
    """
    if not clusters:
        log.log_info("compute_internal_trending_completed", trending_count=0, cluster_count=0)
        return []

    # Reason: the spike baseline is the batch median (the store has no engagement
    # history) unless an explicit historical map is injected — the documented fallback.
    baselines = (
        creator_baselines
        if creator_baselines is not None
        else compute_creator_engagement_baselines(list(items_by_id.values()))
    )
    history_counts = compute_history_sample_counts(items_by_id, store_module)

    trending_items: list[TrendingItem] = []
    for cluster in clusters:
        cluster_id = str(getattr(cluster, "cluster_id", "") or "")
        convergence_count = int(getattr(cluster, "source_diversity", 0) or 0)
        representative = _representative_item(cluster, items_by_id)

        if representative is None:
            # Reason: a cluster whose ids map to no known item still ranks on its
            # convergence alone, with a neutral spike — never dropped, never crashed.
            item_external_id = str(getattr(cluster, "representative_item_id", "") or "")
            creator_external_id = ""
            title = ""
            card_url = ""
            baseline_median: Optional[float] = None
            baseline_relative_ratio = _NEUTRAL_SPIKE
            history_sample_count = 0
        else:
            item_external_id = str(representative.item_external_id)
            creator_external_id = str(getattr(representative, "creator_external_id", "") or "")
            title = str(getattr(representative, "title", "") or "")
            card_url = str(getattr(representative, "card_url", "") or "")
            baseline_median = baselines.get(creator_external_id)
            current_blend = engagement_blend(representative)
            baseline_relative_ratio = signals.baseline_relative_ratio(current_blend, baseline_median)
            history_sample_count = history_counts.get(creator_external_id, 0)

        convergence_bonus = convergence_per_creator * max(convergence_count - 1, 0)
        velocity_score = spike_weight * baseline_relative_ratio + convergence_bonus

        trending_items.append(
            TrendingItem(
                item_external_id=item_external_id,
                cluster_id=cluster_id,
                creator_external_id=creator_external_id,
                title=title,
                card_url=card_url,
                velocity_score=velocity_score,
                convergence_count=convergence_count,
                baseline_relative_ratio=baseline_relative_ratio,
                creator_baseline_median=baseline_median,
                history_sample_count=history_sample_count,
            )
        )

    # Reason: sort descending by velocity_score; break ties on cluster_id so the
    # right-rail order is stable and deterministic across runs (a flapping order would
    # confuse the user).
    trending_items.sort(key=lambda trending_item: (-trending_item.velocity_score, trending_item.cluster_id))

    log.log_info(
        "compute_internal_trending_completed",
        trending_count=len(trending_items),
        cluster_count=len(clusters),
        top_score=round(trending_items[0].velocity_score, 4) if trending_items else 0.0,
    )
    return trending_items
