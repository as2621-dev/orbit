"""External trending — corroboration-vs-scoop tagging + scoop detection (Phase 5 / Stage 5b-5c).

Everything that operates on :class:`lib.trending.TrendingItem` AFTER the internal-network
ranking lives here, keeping :mod:`lib.trending` focused on the Stage-5a internal half (and
both files under the project's 500-line file-size discipline). This module imports the
``TrendingItem`` contract from :mod:`lib.trending` and EXTENDS it — it never recomputes
internal velocity.

Two layers, both deterministic (Rule 5 — no LLM, no judgment call), both bounded:

  * **Stage 5b — corroboration vs scoop** (:func:`tag_external_corroboration`): for the TOP
    internal-trending items, a light KEYLESS external cross-search asks "does this topic ALSO
    have signal OUTSIDE the user's network?" The RESULT COUNT is classified by a pure count
    threshold (:func:`_classify_corroboration`) into ``corroborated`` (also big outside the
    network) vs ``scoop`` (your people first — near-zero external signal). The external egress
    is bounded by ``depth`` (cost control / CSO) and funnelled through the injectable
    :data:`lib.web_search_keyless.SearchFn` seam so tests never touch the live web.

  * **Stage 5c — anomaly / scoop detection** (:func:`detect_scoops`,
    :func:`build_trending_multiplier_map`): a SCOOP is a normally-DORMANT account (low
    ``history_sample_count``) that SUDDENLY ACCELERATES (high ``baseline_relative_ratio``) —
    a deterministic two-threshold AND. The multiplier map activates the rerank trending boost.

Rule 12 — defensive throughout: empty inputs return empty, a misbehaving search degrades the
item to a SAFE tag (``scoop``), nothing crashes the digest. CSO — keyless egress: no API key,
no secret, no cookie; the only thing sent out is the public title string.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``lib`` importable whether imported as the package member ``lib.external_trending``
# (via orbit.py's sys.path insert of the scripts dir) or run from the scripts dir directly.
# Mirrors trending.py / web_search_keyless.py / cluster.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.trending import TrendingItem  # noqa: E402  (the contract this module extends)
from lib.web_search_keyless import SearchFn, keyless_search  # noqa: E402  (the external-tag boundary)

# --- External corroboration tagging (Phase 5 / Stage 5b) ---------------------
# This tags the TOP internal-trending items (already ranked DESCENDING by
# compute_internal_trending) with a light, KEYLESS, bounded external cross-search:
# how much signal does this topic ALSO have OUTSIDE the user's network? The
# classification is a DETERMINISTIC count threshold (Rule 5 — NOT an LLM call): the
# corroboration-vs-scoop distinction is the brief's Stage-5b signal. The DEPTH→budget
# map BOUNDS the number of cross-searches issued (cost control) — items beyond the
# budget keep the default UNTAGGED tag (no search spent).

# The corroboration tag values (stable strings the renderer + scoops strip read).
CORROBORATION_TAG_CORROBORATED: str = "corroborated"  # also big OUTSIDE the network
CORROBORATION_TAG_SCOOP: str = "scoop"  # your people first — little external signal
CORROBORATION_TAG_UNTAGGED: str = ""  # beyond the depth budget, or never searched

# The DETERMINISTIC corroboration threshold: an item whose external cross-search
# returns >= this many results is "corroborated" (the topic is also big outside the
# user's network); fewer means "scoop" (your network had it first, little external
# signal). First-cut value — tunable. With the keyless search capped at
# EXTERNAL_RESULTS_PER_SEARCH results, a near-zero external footprint reads as a
# scoop and a well-covered topic reads as corroborated.
CORROBORATION_RESULT_THRESHOLD: int = 3

# How many results to request per cross-search. Capped low: the tag only needs to
# know "many vs near-zero", so a small page bounds parse cost and request size. Must
# be >= CORROBORATION_RESULT_THRESHOLD so a corroborated topic can actually clear it.
EXTERNAL_RESULTS_PER_SEARCH: int = 5

# DEPTH → max cross-searches issued. The COST control (CSO): the external egress is
# bounded by the user's depth throttle so a busy day never fires unbounded web
# requests on the user's plan. Keys are config's ALLOWED_DEPTHS ("quick"/"default"/
# "deep"); first-cut values — tunable. Only the TOP-N internal-trending items (the
# right-rail head) are worth corroborating, so even "deep" stays small.
DEPTH_CROSS_SEARCH_BUDGET: dict[str, int] = {
    "quick": 3,
    "default": 8,
    "deep": 15,
}

# Fallback budget for an unknown depth string (defensive — config validates depth,
# but tag_external_corroboration also accepts a raw depth param). Uses the "default"
# budget so a typo degrades to the middle, never to unbounded.
_DEFAULT_CROSS_SEARCH_BUDGET: int = DEPTH_CROSS_SEARCH_BUDGET["default"]


def _classify_corroboration(external_result_count: int, result_threshold: int) -> str:
    """Map an external result count to a corroboration tag (the deterministic rule).

    Rule 5 — this is a pure count threshold, NOT an LLM judgment. ``>= threshold``
    means the topic is also big OUTSIDE the user's network ("corroborated"); fewer
    means the user's network had it first with little external signal ("scoop").

    Args:
        external_result_count: How many external results the cross-search returned.
        result_threshold: Count at/above which the item is ``corroborated``.

    Returns:
        :data:`CORROBORATION_TAG_CORROBORATED` or :data:`CORROBORATION_TAG_SCOOP`.

    Example:
        >>> _classify_corroboration(5, 3)
        'corroborated'
        >>> _classify_corroboration(0, 3)
        'scoop'
    """
    if external_result_count >= result_threshold:
        return CORROBORATION_TAG_CORROBORATED
    return CORROBORATION_TAG_SCOOP


def tag_external_corroboration(
    trending_items: list[TrendingItem],
    *,
    search_fn: SearchFn = keyless_search,
    depth: str = "default",
    result_threshold: int = CORROBORATION_RESULT_THRESHOLD,
) -> list[TrendingItem]:
    """Tag the TOP internal-trending items corroborated-vs-scoop via a bounded cross-search.

    The brief's Stage-5b signal: for the highest-velocity internal items, run a light
    KEYLESS external cross-search and tag each ``corroborated`` (also big outside the
    user's network — many external results) vs ``scoop`` (your people first — near-zero
    external signal). Classification is a DETERMINISTIC count threshold
    (:func:`_classify_corroboration`), NOT an LLM call (Rule 5).

    **Cost control (CSO):** ``depth`` BOUNDS the number of cross-searches issued via
    :data:`DEPTH_CROSS_SEARCH_BUDGET` — ``quick`` fires the fewest, ``deep`` the most.
    The input is assumed ALREADY RANKED descending by velocity (as
    :func:`lib.trending.compute_internal_trending` returns it), so the budget spends on
    the head of the list — the items most worth corroborating. Items BEYOND the budget
    keep :data:`CORROBORATION_TAG_UNTAGGED` (no search spent on them). The external
    egress is KEYLESS — no API key, no secret; the only thing sent out is the public
    title string.

    **Defensive (Rule 12):** an empty list issues no searches and returns ``[]``. A
    ``search_fn`` that raises or returns empty for an item degrades that item to a SAFE
    default tag (``scoop`` — near-zero external signal — never a crash). Mutates and
    returns the SAME :class:`lib.trending.TrendingItem` objects (sets ``corroboration_tag``
    in place) so caller references stay valid.

    Args:
        trending_items: The ranked internal-trending output (descending by
            ``velocity_score``). The head, up to the depth budget, is cross-searched.
        search_fn: The injected keyless cross-search ``(query: str) -> list[SearchResult]``.
            Defaults to :func:`lib.web_search_keyless.keyless_search`; tests inject a
            fake returning canned result lists so NO live web call is made.
        depth: The cost throttle — one of ``"quick"``/``"default"``/``"deep"`` (config's
            ``ALLOWED_DEPTHS``). Selects the cross-search budget; an unknown value
            degrades to the ``"default"`` budget (never unbounded).
        result_threshold: External-result count at/above which an item is
            ``corroborated`` (default :data:`CORROBORATION_RESULT_THRESHOLD`). Injectable
            for tuning/tests.

    Returns:
        The same ``trending_items`` list, with each searched item's ``corroboration_tag``
        set to ``corroborated``/``scoop`` and items beyond the budget left untagged.

    Example:
        >>> tag_external_corroboration([])
        []
    """
    if not trending_items:
        log.log_info("tag_external_corroboration_completed", tagged_count=0, searches_issued=0)
        return trending_items

    cross_search_budget = DEPTH_CROSS_SEARCH_BUDGET.get(depth, _DEFAULT_CROSS_SEARCH_BUDGET)

    searches_issued = 0
    corroborated_count = 0
    scoop_count = 0
    for trending_item in trending_items:
        if searches_issued >= cross_search_budget:
            # Reason: cost control — beyond the depth budget, spend no more egress; the
            # tail keeps the untagged default so the renderer can tell "not searched"
            # apart from a searched scoop.
            break

        query = (trending_item.title or "").strip()
        if not query:
            # Reason: no headline to search on — degrade to the safe scoop default
            # WITHOUT spending an egress call on a blank query.
            trending_item.corroboration_tag = CORROBORATION_TAG_SCOOP
            scoop_count += 1
            continue

        searches_issued += 1
        try:
            results = search_fn(query)
        except Exception as exc:  # noqa: BLE001 — a misbehaving search must degrade the tag, not crash the digest
            log.log_warning(
                "external_cross_search_failed",
                cluster_id=trending_item.cluster_id,
                error_type=type(exc).__name__,
                fix_suggestion="External corroboration is best-effort; the item is tagged 'scoop' (no external signal) and the digest continues.",
            )
            trending_item.corroboration_tag = CORROBORATION_TAG_SCOOP
            scoop_count += 1
            continue

        external_result_count = len(results) if results else 0
        # Reason: classify deterministically off the count + the (possibly tuned)
        # threshold (Rule 5 — a count threshold, NOT an LLM call). The local
        # result_threshold param is honored here so tests/tuning can override it.
        tag = _classify_corroboration(external_result_count, result_threshold)
        trending_item.corroboration_tag = tag
        if tag == CORROBORATION_TAG_CORROBORATED:
            corroborated_count += 1
        else:
            scoop_count += 1

    log.log_info(
        "tag_external_corroboration_completed",
        trending_count=len(trending_items),
        searches_issued=searches_issued,
        cross_search_budget=cross_search_budget,
        depth=depth,
        corroborated_count=corroborated_count,
        scoop_count=scoop_count,
    )
    return trending_items


# --- Anomaly / scoop detection (Phase 5 / Stage 5c) --------------------------
# Sub-phase 4 EXTENDS this section. A SCOOP is the brief's HIGHEST-value signal: a
# normally-DORMANT account (few prior posts — a LOW history_sample_count) that
# SUDDENLY posts something ACCELERATING fast (a HIGH baseline_relative_ratio from
# Sub-phase 2). BOTH conditions are required — dormancy alone (a quiet account
# posting normally) is not a scoop, and a high spike from a HIGH-frequency creator
# is not a scoop either (that is merely "high engagement", explicitly NOT the
# signal). The detection is a DETERMINISTIC two-threshold AND (Rule 5 — no LLM).

# Dormancy threshold: a creator with AT MOST this many prior items in the ``seen``
# history is treated as "normally dormant". First-cut value — tuned against the
# maintainer's real network (a creator who posts daily clears this within days; a
# creator who surfaces a few times a month stays under it). Tunable.
SCOOP_DORMANCY_MAX_HISTORY: int = 5

# Acceleration threshold: the representative item's engagement must be AT LEAST this
# many times the creator's OWN baseline (baseline_relative_ratio) to count as
# "accelerating fast". > 1.0 means above the creator's normal; this requires a real
# spike, not a normal post. First-cut value — tunable.
SCOOP_ACCELERATION_MIN_RATIO: float = 2.0

# --- Trending/scoop rerank multiplier (the wiring for rerank.py) -------------
# The multipliers fed into rerank.score_item's optional trending map. A SCOOP gets
# the largest boost (the highest-value signal), a non-scoop trending item a smaller
# one, so a scoop's derank score rises ABOVE an otherwise-identical non-scoop item
# (DoD #2). Both are > 1.0 (the M1 no-op was exactly 1.0). First-cut values —
# tunable. Kept > the scoop boost strictly above the trending boost so the ordering
# scoop > trending > neutral is guaranteed regardless of intrinsic ties.
SCOOP_RERANK_MULTIPLIER: float = 1.5
TRENDING_RERANK_MULTIPLIER: float = 1.2


def detect_scoops(
    trending_items: list[TrendingItem],
    *,
    dormancy_max_history: int = SCOOP_DORMANCY_MAX_HISTORY,
    acceleration_min_ratio: float = SCOOP_ACCELERATION_MIN_RATIO,
) -> list[TrendingItem]:
    """Flag dormant-account acceleration scoops — the brief's highest-value signal.

    A :class:`lib.trending.TrendingItem` is a SCOOP when BOTH hold (a deterministic
    two-threshold AND — Rule 5, no LLM):

      1. **Dormancy** — the creator is normally quiet: ``history_sample_count <=
         dormancy_max_history`` (few prior items in the ``seen`` history, the TIME
         dimension Sub-phase 2 attached). A creator with NO resolvable history
         (count 0) counts as dormant.
      2. **Acceleration** — the representative item is spiking far above the
         creator's OWN normal: ``baseline_relative_ratio >= acceleration_min_ratio``
         (the Sub-phase 2 baseline-relative spike).

    Crucially BOTH are required. A HIGH-frequency creator's spike (high ratio but a
    history count ABOVE the dormancy threshold) is NOT a scoop — that is merely "high
    engagement", which the brief explicitly distinguishes from the scoop signal. A
    dormant account posting at its NORMAL level (low history but ratio below the
    acceleration threshold) is likewise NOT a scoop. Only dormancy + acceleration
    together is the loud, highest-value signal.

    Mutates each flagged item's :attr:`lib.trending.TrendingItem.is_scoop` to ``True`` in
    place (so caller references and the right-rail render see the flag) and RETURNS the
    flagged subset — the scoops strip — in the input's (velocity-descending) order. Items
    that are not scoops keep ``is_scoop = False``.

    Args:
        trending_items: The internal-trending output (ideally already
            external-tagged); each carries ``history_sample_count`` +
            ``baseline_relative_ratio``.
        dormancy_max_history: Max prior-item count for a creator to count as dormant
            (default :data:`SCOOP_DORMANCY_MAX_HISTORY`). Injectable for tuning/tests.
        acceleration_min_ratio: Min baseline-relative spike ratio to count as
            accelerating (default :data:`SCOOP_ACCELERATION_MIN_RATIO`). Injectable.

    Returns:
        The subset of ``trending_items`` flagged as scoops (``is_scoop`` set True on
        each), in the input order. Empty input -> ``[]`` (Rule 12 — quiet day).

    Example:
        >>> detect_scoops([])
        []
    """
    if not trending_items:
        log.log_info("detect_scoops_completed", scoop_count=0, trending_count=0)
        return []

    scoops: list[TrendingItem] = []
    for trending_item in trending_items:
        is_dormant = trending_item.history_sample_count <= dormancy_max_history
        is_accelerating = trending_item.baseline_relative_ratio >= acceleration_min_ratio
        # Reason: BOTH conditions — dormancy AND acceleration — define a scoop. A frequent
        # poster's spike (not dormant) and a dormant account's normal post (not
        # accelerating) are deliberately NOT scoops (the brief's distinction).
        if is_dormant and is_accelerating:
            trending_item.is_scoop = True
            scoops.append(trending_item)
        else:
            trending_item.is_scoop = False

    log.log_info(
        "detect_scoops_completed",
        scoop_count=len(scoops),
        trending_count=len(trending_items),
        dormancy_max_history=dormancy_max_history,
        acceleration_min_ratio=acceleration_min_ratio,
    )
    return scoops


def build_trending_multiplier_map(
    trending_items: list[TrendingItem],
    *,
    scoop_multiplier: float = SCOOP_RERANK_MULTIPLIER,
    trending_multiplier: float = TRENDING_RERANK_MULTIPLIER,
) -> dict[str, float]:
    """Build the ``item_external_id`` -> rerank multiplier map for the trending boost.

    The bridge that activates the ``TRENDING_MULTIPLIER_NEUTRAL`` 1.0 no-op left in
    :func:`lib.rerank.score_item`: each trending item maps to a multiplier ``> 1.0`` so
    its derank score rises above an otherwise-identical non-trending item, and a SCOOP
    (``is_scoop``) maps to the LARGER :data:`SCOOP_RERANK_MULTIPLIER` so a scoop
    outranks a non-scoop trending item of equal intrinsic score (DoD #2). This lives
    here (not in rerank) so rerank stays trending-agnostic — it merely consumes the
    map it is handed.

    Items with an empty ``item_external_id`` are skipped (no rankable item to boost).
    When two trending entries share an ``item_external_id`` (a representative reused
    across clusters), the scoop multiplier wins (``max``) so a scoop is never demoted
    by a sibling trending entry.

    Args:
        trending_items: The trending output, ideally after :func:`detect_scoops` has
            set ``is_scoop`` (so scoops get the larger boost). Items not yet
            scoop-checked simply get the trending multiplier.
        scoop_multiplier: Multiplier for ``is_scoop`` items (default
            :data:`SCOOP_RERANK_MULTIPLIER`). Injectable for tuning/tests.
        trending_multiplier: Multiplier for non-scoop trending items (default
            :data:`TRENDING_RERANK_MULTIPLIER`). Injectable.

    Returns:
        A ``item_external_id`` -> multiplier map to pass to
        :func:`lib.rerank.derank_items`'s ``trending_multipliers``. Empty when no
        trending item has a resolvable id.

    Example:
        >>> build_trending_multiplier_map([])
        {}
    """
    multiplier_by_item: dict[str, float] = {}
    for trending_item in trending_items:
        item_external_id = str(trending_item.item_external_id or "")
        if not item_external_id:
            continue
        multiplier = scoop_multiplier if trending_item.is_scoop else trending_multiplier
        # Reason: if the same item id appears twice, keep the larger boost so a scoop is
        # never overwritten by a sibling non-scoop trending entry.
        existing = multiplier_by_item.get(item_external_id)
        multiplier_by_item[item_external_id] = multiplier if existing is None else max(existing, multiplier)
    return multiplier_by_item
