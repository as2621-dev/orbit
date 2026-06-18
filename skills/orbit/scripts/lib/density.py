"""Density-tier assignment (Phase 3 / Stage 6b) for Orbit.

Takes the descending-sorted :class:`lib.rerank.ScoredItem` list from
:func:`lib.rerank.derank_items` and assigns each item a ``density_tier``
(``hero`` | ``standard`` | ``compact`` | ``index``). The renderer (Sub-phase 3)
turns the tier into visual density: Hero = large card, Standard = medium card,
Compact = row, Index = the bottom "they also posted" line strip.

THE CORE INVARIANT (api-contracts derank contract, design decision 6):
**rank controls density, never inclusion.** Every scored item gets a tier —
``len(assign_density_tiers(scored)) == len(scored)``. Nothing is ever dropped.

Two rules shape the assignment:

  1. **Forced-index routing.** An item that FAILED classification — its
     :attr:`lib.classify.Classification.is_also_posted` is True (Axis A noise OR
     Axis B off-topic), OR it has no classification at all (``classification is
     None``) — is forced into the ``index`` "they also posted" tier REGARDLESS of
     its score. A noisy item with a sky-high engagement score still belongs in
     "they also posted", not Hero.
  2. **Proportional (rank-position) tiering of the rest.** The items that PASSED
     classification are tiered by their RANK POSITION over the passing
     distribution — the top :data:`HERO_FRACTION` go to ``hero``, the next
     :data:`STANDARD_FRACTION` to ``standard``, the next :data:`COMPACT_FRACTION`
     to ``compact``, and the remainder to ``index``. This is proportional /
     threshold-based, NOT a hard top-N count — so a 6-item day and a 200-item day
     both get a sensible Hero band.

The carryforward rule (api-contracts ``carryforward`` table) is exposed as a
separate, injectable step :func:`record_top_tier_carryforward`: top-tier
(``hero`` / ``standard``) items the user did NOT open are eligible for ONE
resurface via the store's ``record_carryforward`` (``surfaced_count`` capped at 1).
"Did the user open it?" has no signal source wired in M1, so the set of unopened
ids is passed IN by the caller; ``store_module`` is injectable so tests use a temp
DB. This is kept OUT of :func:`assign_density_tiers` so tiering stays a pure,
side-effect-free function (Rule 5 — deterministic transform, no model).

Rule 5: there is NO LLM here — tiering is pure rank arithmetic.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# Make ``lib`` and ``store`` importable whether this module is imported as the package
# member ``lib.density`` (via orbit.py's sys.path insert of the scripts dir) or run
# from the scripts dir directly. Mirrors rerank.py / classify.py: ``lib/`` is this
# file's parent; the scripts dir (which holds ``store.py``) is its grandparent.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (import must follow the sys.path inserts above)
from lib import log  # noqa: E402
from lib.rerank import ScoredItem  # noqa: E402

# --- Tier names (the four density bands, top to bottom) ---------------------
TIER_HERO: str = "hero"
TIER_STANDARD: str = "standard"
TIER_COMPACT: str = "compact"
TIER_INDEX: str = "index"

# The tiers that count as "top-tier" for carryforward eligibility.
TOP_TIERS: frozenset[str] = frozenset({TIER_HERO, TIER_STANDARD})

# --- Tier-boundary constants (the tunable surface) --------------------------
# Fractional cutoffs over the SORTED PASSING distribution (rank position, NOT a
# hard top-N count). The top HERO_FRACTION of passing items go to hero, the next
# STANDARD_FRACTION to standard, the next COMPACT_FRACTION to compact, and the
# remainder to index. These are first-cut values; real-day usage tunes the shape
# (master-plan riskiest-assumption test — does the right item land in Hero?).
# They need not sum to 1.0; whatever is left after the three bands falls to index.
HERO_FRACTION: float = 0.10
STANDARD_FRACTION: float = 0.25
COMPACT_FRACTION: float = 0.35


@dataclass
class TieredItem:
    """A :class:`lib.rerank.ScoredItem` paired with its assigned density tier.

    This is the unit the renderer (Sub-phases 3-4) consumes: it carries the scored
    item (and through it the full :class:`lib.rerank.RankableItem` — title, deep-link
    fields, chapters, classification) plus the tier that decides its visual density.

    Attributes:
        scored_item: The scored item (``.item`` is the :class:`RankableItem`,
            ``.score`` its derank score). Unchanged from rerank — tiering never
            mutates score or drops items.
        density_tier: One of :data:`TIER_HERO` | :data:`TIER_STANDARD` |
            :data:`TIER_COMPACT` | :data:`TIER_INDEX`.
    """

    scored_item: ScoredItem
    density_tier: str


def _tier_for_rank_position(rank_position: int, passing_count: int) -> str:
    """Map a 0-based rank position within the passing distribution to a tier.

    Proportional / threshold assignment (NOT a hard top-N count): cutoffs are
    computed as fractions of ``passing_count`` via :data:`HERO_FRACTION` etc., each
    rounded UP (``ceil``) so a tiny batch still seeds at least one Hero. Whatever
    rank positions fall past the compact cutoff land in :data:`TIER_INDEX`.

    Args:
        rank_position: The item's 0-based position in the descending-sorted passing
            list (0 == top score).
        passing_count: How many items passed classification (the band denominator).

    Returns:
        The tier name for this rank position.

    Example:
        >>> _tier_for_rank_position(0, 100)
        'hero'
    """
    # Reason: ceil so a small day (e.g. 3 passing items) still gets a Hero rather
    # than rounding the top band down to zero.
    hero_cutoff = math.ceil(passing_count * HERO_FRACTION)
    standard_cutoff = hero_cutoff + math.ceil(passing_count * STANDARD_FRACTION)
    compact_cutoff = standard_cutoff + math.ceil(passing_count * COMPACT_FRACTION)

    if rank_position < hero_cutoff:
        return TIER_HERO
    if rank_position < standard_cutoff:
        return TIER_STANDARD
    if rank_position < compact_cutoff:
        return TIER_COMPACT
    return TIER_INDEX


def _failed_classification(scored_item: ScoredItem) -> bool:
    """True when the item must be FORCED into the index "they also posted" tier.

    An item is forced to index when it has no classification at all
    (``classification is None`` — never judged, so not a top-line item) OR its
    :attr:`lib.classify.Classification.is_also_posted` is True (Axis A noise or
    Axis B off-topic). Either way it is "they also posted", never Hero.

    Args:
        scored_item: The scored item to test.

    Returns:
        True if the item must be routed to :data:`TIER_INDEX` regardless of score.
    """
    classification = getattr(scored_item.item, "classification", None)
    if classification is None:
        return True
    # Reason: is_also_posted is a property on lib.classify.Classification; guard with
    # getattr so a duck-typed classification missing it degrades to "not also-posted"
    # rather than crashing the whole tiering pass (Rule 12).
    return bool(getattr(classification, "is_also_posted", False))


def assign_density_tiers(scored_items: list[ScoredItem]) -> list[TieredItem]:
    """Assign every scored item a density tier — nothing dropped (the core invariant).

    Input is the descending-sorted :class:`lib.rerank.ScoredItem` list from
    :func:`lib.rerank.derank_items`. Output is a :class:`TieredItem` per input item,
    in the SAME order, so ``len(out) == len(in)`` always (rank controls density, never
    inclusion — api-contracts derank contract).

    Assignment:
      1. Items that FAILED classification (:func:`_failed_classification`: ``is_also_posted``
         True, or ``classification is None``) are forced into :data:`TIER_INDEX`
         REGARDLESS of score — they belong in "they also posted", not Hero.
      2. The items that PASSED are tiered by RANK POSITION over the passing
         distribution (proportional cutoffs from :data:`HERO_FRACTION` etc.), NOT a
         hard top-N count.

    Args:
        scored_items: The descending-sorted scored items from ``derank_items``.

    Returns:
        A :class:`TieredItem` per input item, preserving input order.

    Example:
        >>> from lib.rerank import RankableItem, ScoredItem
        >>> from lib.classify import Classification
        >>> passed = Classification("a", axis_a_signal=1, axis_b_on_topic=1, is_user_override=0)
        >>> item = RankableItem("a", "t", "c", "UC1", 1, None, None, "20260101", classification=passed)
        >>> tiered = assign_density_tiers([ScoredItem(item=item, score=5.0)])
        >>> tiered[0].density_tier
        'hero'
        >>> len(tiered) == 1  # nothing dropped
        True
    """
    if not scored_items:
        log.log_info("density_tiers_assigned", item_count=0)
        return []

    # Partition into passing vs forced-index, keeping each item's original index so we
    # can restore the input order in the output (the renderer relies on the descending
    # rank order being preserved).
    passing_with_index: list[tuple[int, ScoredItem]] = []
    forced_index_positions: set[int] = set()
    for original_index, scored_item in enumerate(scored_items):
        if _failed_classification(scored_item):
            forced_index_positions.add(original_index)
        else:
            passing_with_index.append((original_index, scored_item))

    passing_count = len(passing_with_index)
    tier_by_original_index: dict[int, str] = {}
    # Passing items keep their relative rank order (the input is already sorted
    # descending), so their position within the passing list is their rank position.
    for rank_position, (original_index, _scored_item) in enumerate(passing_with_index):
        tier_by_original_index[original_index] = _tier_for_rank_position(rank_position, passing_count)
    for original_index in forced_index_positions:
        tier_by_original_index[original_index] = TIER_INDEX

    tiered_items = [
        TieredItem(scored_item=scored_item, density_tier=tier_by_original_index[original_index])
        for original_index, scored_item in enumerate(scored_items)
    ]

    tier_distribution = {
        TIER_HERO: 0,
        TIER_STANDARD: 0,
        TIER_COMPACT: 0,
        TIER_INDEX: 0,
    }
    for tiered_item in tiered_items:
        tier_distribution[tiered_item.density_tier] += 1

    log.log_info(
        "density_tiers_assigned",
        item_count=len(tiered_items),
        passing_count=passing_count,
        forced_index_count=len(forced_index_positions),
        hero_count=tier_distribution[TIER_HERO],
        standard_count=tier_distribution[TIER_STANDARD],
        compact_count=tier_distribution[TIER_COMPACT],
        index_count=tier_distribution[TIER_INDEX],
    )
    return tiered_items


def record_top_tier_carryforward(
    tiered_items: list[TieredItem],
    unopened_ids: Optional[Iterable[str]] = None,
    *,
    store_module: Any = store,
) -> list[str]:
    """Record top-tier items the user did NOT open into carryforward (resurface-once).

    Top-tier (``hero`` / ``standard``) items whose ``item_external_id`` is in
    ``unopened_ids`` are recorded via ``store_module.record_carryforward`` (which caps
    ``surfaced_count`` at 1 — calling repeatedly never exceeds the cap, the
    resurface-once intent of the api-contracts ``carryforward`` table). Compact / index
    items are never carried forward. This is a SEPARATE, side-effecting step from
    :func:`assign_density_tiers` so tiering itself stays pure and testable.

    "Did the user open it?" has no signal source wired in M1, so the unopened set is
    passed IN (e.g. from a future open-tracking store read). ``store_module`` is
    injectable so tests point it at a temp DB.

    Args:
        tiered_items: The output of :func:`assign_density_tiers`.
        unopened_ids: ``item_external_id``s the user did NOT open. None / empty means
            nothing is carried forward this run.
        store_module: The store module (injectable for tests). Defaults to :mod:`store`.

    Returns:
        The list of ``item_external_id``s actually recorded to carryforward.

    Example:
        >>> record_top_tier_carryforward(tiered, unopened_ids={"vid_abc"})  # doctest: +SKIP
        ['vid_abc']
    """
    unopened_set = set(unopened_ids) if unopened_ids else set()
    recorded_ids: list[str] = []
    if not unopened_set:
        log.log_info("carryforward_skipped", reason="no_unopened_ids")
        return recorded_ids

    for tiered_item in tiered_items:
        if tiered_item.density_tier not in TOP_TIERS:
            continue
        item_external_id = tiered_item.scored_item.item.item_external_id
        if item_external_id not in unopened_set:
            continue
        store_module.record_carryforward(item_external_id=item_external_id, density_tier=tiered_item.density_tier)
        recorded_ids.append(item_external_id)

    log.log_info(
        "carryforward_top_tier_recorded",
        recorded_count=len(recorded_ids),
        unopened_count=len(unopened_set),
    )
    return recorded_ids
