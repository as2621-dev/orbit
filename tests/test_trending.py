"""DoD tests for internal-network trending (Phase 5 / Sub-phase 2, Stage 5a).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Internal trending (Stage 5a) is what turns the flat clustered stream into the
right-rail "trending" list, and the brief's CORE Stage-5 distinction is that trending
is BASELINE-RELATIVE velocity, NOT raw popularity. So the tests assert the product
intents the brief depends on, each constructed to FAIL on wrong business logic:

  1. Convergence / cluster-size velocity: a cluster touched by 3 DIFFERENT followed
     creators ranks ABOVE a single-creator cluster of EQUAL raw engagement. A
     regression that ignored ``source_diversity`` (no convergence term) would fail.
  2. Baseline-relative spike: an item at ~5× the creator's OWN baseline ranks as
     trending while an item at the creator's NORMAL level does NOT — even when the
     normal-level item has HIGHER RAW engagement. This test FAILS if someone reverts
     to raw-popularity ranking (the brief's core Stage-5 distinction).
  3. No LLM/network (Rule 5): trending.py + signals.py import no network/LLM/embedding
     client; the functions are pure given the injected fake store.

Also covers the signals.py primitives this sub-phase owns (``baseline_relative_ratio``,
``normalize``) and the store-injection seam (a fake store supplies history depth — no
real user DB is touched).

All inputs are constructed fixtures; the store is a fake/dict-backed object injected via
``store_module`` so no network/LLM/real-DB is hit in this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``skills/orbit/scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_cluster.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import signals  # noqa: E402
from lib.cluster import Cluster  # noqa: E402
from lib.rerank import RankableItem, engagement_blend  # noqa: E402
from lib.trending import TrendingItem, compute_internal_trending  # noqa: E402


# --- Fixtures ---------------------------------------------------------------


def _item(
    item_external_id: str,
    *,
    creator: str,
    view_count: int,
    like_count: int = 0,
    comment_count: int = 0,
) -> RankableItem:
    """A short rankable item with explicit engagement (no chapters -> not long-form)."""
    return RankableItem(
        item_external_id=item_external_id,
        title=f"title {item_external_id}",
        channel_name=f"channel {creator}",
        creator_external_id=creator,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        upload_date="20260110",
    )


def _cluster(cluster_id: str, *, representative: str, members: list[str], source_diversity: int) -> Cluster:
    """Build a Cluster directly (Sub-phase 1's output shape) with a fixed source_diversity."""
    return Cluster(
        cluster_id=cluster_id,
        member_item_ids=list(members),
        representative_item_id=representative,
        cross_links=[],
        source_diversity=source_diversity,
    )


class _FakeStore:
    """A fake store exposing ONLY list_sources + get_seen_ids (the trending seam).

    Backed by in-memory dicts so the history-depth lookup never touches a real DB. This
    is the injected ``store_module`` — proving production code reads the store solely
    through these two functions and is pure given the fake.
    """

    def __init__(self, sources: list[dict], seen_by_source: dict[int, set[str]]) -> None:
        self._sources = sources
        self._seen_by_source = seen_by_source

    def list_sources(self) -> list[dict]:
        return self._sources

    def get_seen_ids(self, source_id: int) -> set[str]:
        return self._seen_by_source.get(source_id, set())


def _empty_store() -> _FakeStore:
    """A fake store with no sources and no seen history (history depth -> 0 everywhere)."""
    return _FakeStore(sources=[], seen_by_source={})


# --- DoD #1: convergence / cluster-size velocity ----------------------------


def test_three_creator_cluster_outranks_single_creator_cluster_of_equal_engagement() -> None:
    """A cluster 3 different followed creators converge on outranks a 1-creator cluster.

    WHY (convergence/velocity intent, brief Stage 5a): "multiple followed creators
    converging on the same cluster" is a network-velocity signal — 3 different people in
    your network on one topic is MORE trending than 1 person, at EQUAL raw engagement. A
    regression that dropped the ``source_diversity`` convergence term (ranking only on
    the representative's raw/relative engagement) would tie or invert these and FAIL.
    """
    # Two clusters whose representatives have IDENTICAL engagement (so the spike term is
    # equal). The ONLY difference is source_diversity: 3 distinct creators vs 1.
    converged_rep = _item("converged_rep", creator="c1", view_count=1000, like_count=50, comment_count=5)
    lone_rep = _item("lone_rep", creator="c9", view_count=1000, like_count=50, comment_count=5)
    items_by_id = {item.item_external_id: item for item in (converged_rep, lone_rep)}

    converged = _cluster("cluster-1", representative="converged_rep", members=["converged_rep"], source_diversity=3)
    lone = _cluster("cluster-2", representative="lone_rep", members=["lone_rep"], source_diversity=1)

    ranked = compute_internal_trending([converged, lone], items_by_id, _empty_store())

    assert [trending_item.cluster_id for trending_item in ranked] == ["cluster-1", "cluster-2"], (
        "the 3-creator cluster must rank above the 1-creator cluster of equal engagement"
    )
    converged_score = next(t for t in ranked if t.cluster_id == "cluster-1").velocity_score
    lone_score = next(t for t in ranked if t.cluster_id == "cluster-2").velocity_score
    assert converged_score > lone_score, "convergence must strictly raise the velocity score"
    assert next(t for t in ranked if t.cluster_id == "cluster-1").convergence_count == 3


# --- DoD #2: baseline-relative spike, NOT raw popularity ---------------------


def test_spiking_item_outranks_higher_raw_but_normal_item() -> None:
    """A 5×-own-baseline item trends ABOVE a higher-RAW item that's normal-for-its-creator.

    WHY (the brief's CORE Stage-5 distinction — baseline-relative, NOT raw popularity):
    trending must reward an item spiking far above ITS creator's own normal, even when a
    different item has higher RAW engagement that is merely normal for ITS (huge) creator.
    This is the line between "trending in your network" and "globally popular". A
    regression to raw-popularity ranking (e.g. ranking on engagement_blend directly, or
    a baseline that is not per-creator) would put the big-but-normal item on top and FAIL.

    Construction: a small creator whose normal is ~tens of views posts a breakout far
    above its own baseline; a huge creator posts at its OWN normal level but with much
    HIGHER raw views. Each creator's batch baseline is the median of its own items, so the
    small creator's breakout has a high baseline_relative_ratio while the huge creator's
    item sits at its own median (ratio ~1.0). Baseline-relative ranking puts the spiking
    small-creator item on top despite the huge creator's higher raw count.
    """
    # Small creator's recent normal items (define its batch-median baseline) + one breakout.
    small_normal_a = _item("small_normal_a", creator="small", view_count=100, like_count=5, comment_count=1)
    small_normal_b = _item("small_normal_b", creator="small", view_count=120, like_count=6, comment_count=1)
    small_breakout = _item("small_breakout", creator="small", view_count=50_000, like_count=4_000, comment_count=600)

    # Huge creator: many items all at the SAME (huge) level, so its median == its normal,
    # and its "trending candidate" sits exactly at that normal (ratio ~1.0) — yet its RAW
    # engagement is far higher than the small creator's breakout.
    huge_a = _item("huge_a", creator="huge", view_count=5_000_000, like_count=200_000, comment_count=30_000)
    huge_b = _item("huge_b", creator="huge", view_count=5_000_000, like_count=200_000, comment_count=30_000)
    huge_normal = _item("huge_normal", creator="huge", view_count=5_000_000, like_count=200_000, comment_count=30_000)

    items = [small_normal_a, small_normal_b, small_breakout, huge_a, huge_b, huge_normal]
    items_by_id = {item.item_external_id: item for item in items}

    # One cluster per candidate representative; equal source_diversity so convergence is
    # NOT the deciding factor — the spike term alone must decide.
    spike_cluster = _cluster("cluster-spike", representative="small_breakout", members=["small_breakout"], source_diversity=1)
    normal_cluster = _cluster("cluster-normal", representative="huge_normal", members=["huge_normal"], source_diversity=1)

    ranked = compute_internal_trending([spike_cluster, normal_cluster], items_by_id, _empty_store())

    spike = next(t for t in ranked if t.cluster_id == "cluster-spike")
    normal = next(t for t in ranked if t.cluster_id == "cluster-normal")

    # Fixture invariant: the huge-but-normal item DOES have higher RAW engagement — so a
    # raw-popularity ranker would put it first. Baseline-relative ranking must NOT.
    assert engagement_blend(huge_normal) > engagement_blend(small_breakout), (
        "fixture invariant: the normal item must have higher RAW engagement than the spike"
    )
    assert spike.baseline_relative_ratio > 1.5, "the breakout must register as well above its creator's baseline"
    assert abs(normal.baseline_relative_ratio - 1.0) < 0.2, "the huge creator's normal item must register as ~normal"
    assert spike.velocity_score > normal.velocity_score, (
        "the baseline-relative spike must outrank the higher-RAW-but-normal item — "
        "this fails if ranking reverts to raw popularity"
    )
    assert ranked[0].cluster_id == "cluster-spike"


# --- DoD #3: no LLM / no network (Rule 5), structural ------------------------


def test_trending_and_signals_have_no_network_or_llm_imports() -> None:
    """trending.py + signals.py must import no network/LLM/embedding client (Rule 5).

    WHY: the brief mandates internal trending is deterministic local math — no LLM, no
    network client, no embedding dependency. A regression that pulled in
    ``requests``/``openai``/``httpx``/an embedding client would silently add a
    network/cost dependency to a path that must stay offline. Asserting on the source
    text catches that at the import line. (Sub-phase 3's external cross-search lives
    behind the injected, mocked ``lib.web_search_keyless`` boundary — a local module,
    not a network client imported here.)
    """
    forbidden = ("openai", "anthropic", "requests", "httpx", "urllib.request", "sentence_transformers")
    for module_name in ("trending", "signals"):
        source = (SCRIPTS_DIR / "lib" / f"{module_name}.py").read_text(encoding="utf-8")
        for token in forbidden:
            assert f"import {token}" not in source and f"from {token}" not in source, (
                f"{module_name}.py must not import {token} (Rule 5: no network/LLM)"
            )


def test_compute_internal_trending_is_pure_given_injected_fake_store() -> None:
    """compute_internal_trending must run purely against an injected fake store.

    WHY: the store is the only external boundary; the brief requires it be INJECTABLE so
    tests never hit a real user DB. Passing a fake store object (not the real module) and
    getting a deterministic result proves the function reads the store only through the
    injected seam — no hidden global/network access.
    """
    item = _item("a", creator="UC1", view_count=1000, like_count=50, comment_count=5)
    cluster = _cluster("cluster-1", representative="a", members=["a"], source_diversity=1)
    fake_store = _FakeStore(
        sources=[{"source_id": 7, "external_id": "UC1"}],
        seen_by_source={7: {"a", "b", "c", "d"}},
    )

    ranked_first = compute_internal_trending([cluster], {"a": item}, fake_store)
    ranked_second = compute_internal_trending([cluster], {"a": item}, fake_store)

    assert len(ranked_first) == 1
    # The injected store supplies the history depth (4 seen rows for UC1) — the dormancy
    # signal Sub-phase 4 consumes. Proves the store seam is actually read.
    assert ranked_first[0].history_sample_count == 4
    assert ranked_first[0].velocity_score == ranked_second[0].velocity_score, "must be deterministic"


# --- signals.py primitives this sub-phase owns ------------------------------


def test_baseline_relative_ratio_expresses_per_creator_spike() -> None:
    """baseline_relative_ratio is current-over-baseline, neutral on missing baseline.

    WHY: this is the per-creator reference-normalization primitive the whole Stage-5
    distinction rests on (a value is judged against the creator's OWN normal). A 5× value
    over its baseline must read as 5.0; a creator with no baseline must degrade to the
    neutral 1.0 (never a false spike), not crash or divide by zero.
    """
    assert signals.baseline_relative_ratio(10.0, 2.0) == 5.0  # happy path: 5× the baseline
    assert signals.baseline_relative_ratio(5.0, None) == 1.0  # no baseline -> neutral
    assert signals.baseline_relative_ratio(5.0, 0.0) == 1.0  # zero/sub-floor baseline -> neutral, no div-by-zero


def test_normalize_scales_to_unit_interval_with_neutral_midpoint_on_ties() -> None:
    """normalize min-max scales to [0,1], maps an all-equal batch to 0.5, passes None.

    WHY: normalize is the primitive trending/scoop composition uses to put heterogeneous
    signals on a comparable [0,1] footing. An all-tied batch must NOT collapse to all-zero
    (it would erase a single-item or all-equal day); None must pass through untouched so a
    missing signal stays missing rather than becoming a spurious 0.
    """
    assert signals.normalize([0.0, 5.0, 10.0]) == [0.0, 0.5, 1.0]
    assert signals.normalize([3.0, 3.0]) == [0.5, 0.5]  # all-tied -> neutral midpoint
    assert signals.normalize([None, 2.0, 4.0]) == [None, 0.0, 1.0]


# --- Defensive (Rule 12) -----------------------------------------------------


def test_empty_clusters_returns_empty_list() -> None:
    """No clusters -> empty trending list (a quiet day never crashes, Rule 12)."""
    assert compute_internal_trending([], {}, _empty_store()) == []


def test_cluster_with_unresolvable_representative_degrades_to_neutral_spike() -> None:
    """A cluster whose ids map to no known item still ranks on convergence, neutral spike.

    WHY (Rule 12): a stale/cross-source id must not crash the whole right-rail. The cluster
    is kept, scored on its convergence alone with a neutral baseline-relative ratio.
    """
    cluster = _cluster("cluster-1", representative="missing", members=["missing"], source_diversity=2)
    ranked = compute_internal_trending([cluster], {}, _empty_store())
    assert len(ranked) == 1
    result: TrendingItem = ranked[0]
    assert result.baseline_relative_ratio == signals.NEUTRAL_SPIKE_RATIO
    assert result.convergence_count == 2
    assert result.creator_external_id == ""


if __name__ == "__main__":  # pragma: no cover - standalone fallback when pytest is absent
    import traceback

    test_functions = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = 0
    for test_function in test_functions:
        try:
            test_function()
            print(f"PASS {test_function.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test_function.__name__}")
            traceback.print_exc()
    print(f"\n{len(test_functions) - failures}/{len(test_functions)} passed")
    sys.exit(1 if failures else 0)
