"""DoD tests for density-tier assignment (Phase 3 / Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Density tiering (Stage 6b) is where rank turns into VISUAL DENSITY without ever
dropping an item — so the tests assert the three product invariants the renderer
depends on (the sub-phase Definition of Done), each constructed to FAIL on wrong
logic, not just "returns a list":

  1. Nothing dropped: given N scored items, ALL N receive a tier
     (``len(tiered) == len(scored)``). A regression that filtered low scorers would
     silently lose the "they also posted" strip — rank controls density, NEVER
     inclusion (api-contracts derank contract).
  2. Forced-index routing: a classification-FAILED item (``is_also_posted`` True)
     lands in ``index`` even with the HIGHEST score. A regression that tiered purely
     by score would float noise into Hero.
  3. Carryforward resurface-once: a top-tier UNOPENED item is recorded in
     ``carryforward`` with ``surfaced_count`` NOT exceeding 1 across repeated runs.
     A regression that incremented unbounded would resurface the same item forever.

Inputs are constructed ScoredItem fixtures (no network/LLM/rerank run). The
carryforward test points the store at a TEMP sqlite DB via ``ORBIT_DB_PATH`` +
``store._db_override`` (mirrors tests/test_store.py) — no real per-user DB write.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make ``scripts`` importable so ``from lib import ...`` and ``import
# store`` resolve regardless of the working directory. Mirrors tests/test_rerank.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import density, paths  # noqa: E402
from lib.classify import Classification  # noqa: E402
from lib.density import TieredItem, assign_density_tiers, record_top_tier_carryforward  # noqa: E402
from lib.rerank import RankableItem, ScoredItem  # noqa: E402


def _scored(
    item_external_id: str,
    score: float,
    *,
    axis_a_signal: int = 1,
    axis_b_on_topic: int = 1,
    classified: bool = True,
) -> ScoredItem:
    """Build a ScoredItem fixture with a controllable classification.

    ``classified=False`` leaves ``classification`` None (never judged -> forced index).
    Otherwise a Classification with the given axes is attached (both 1 = passing;
    a 0 on either axis makes ``is_also_posted`` True -> forced index).
    """
    classification = (
        Classification(
            item_external_id=item_external_id,
            axis_a_signal=axis_a_signal,
            axis_b_on_topic=axis_b_on_topic,
            is_user_override=0,
        )
        if classified
        else None
    )
    item = RankableItem(
        item_external_id=item_external_id,
        title=f"title {item_external_id}",
        channel_name="channel",
        creator_external_id="UC1",
        view_count=100,
        like_count=10,
        comment_count=1,
        upload_date="20260110",
        classification=classification,
    )
    return ScoredItem(item=item, score=score)


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


# --- DoD #1 — nothing dropped (rank controls density, never inclusion) ------


def test_every_scored_item_receives_a_tier_nothing_dropped() -> None:
    """Given N scored items, ALL N come out with a tier — none dropped.

    WHY: rank controls DENSITY, never inclusion (api-contracts derank contract). The
    "they also posted" strip and the page-2 spill both depend on every item still
    being present after tiering. A regression that filtered low scorers (or the
    forced-index items) would silently lose content the user is entitled to see. We
    assert exact length AND that the exact same id set survives, in the same order.
    """
    scored = [_scored(f"v{index}", score=float(100 - index)) for index in range(20)]

    tiered = assign_density_tiers(scored)

    assert len(tiered) == len(scored), "nothing may be dropped — rank controls density, not inclusion"
    assert [t.scored_item.item.item_external_id for t in tiered] == [
        s.item.item_external_id for s in scored
    ], "tiering must preserve every item and the descending rank order"
    assert all(t.density_tier in {"hero", "standard", "compact", "index"} for t in tiered)


def test_empty_input_returns_empty_without_crashing() -> None:
    """An empty batch returns an empty list (a run may have no new items)."""
    assert assign_density_tiers([]) == []


def test_single_item_lands_in_hero_not_dropped() -> None:
    """A single passing item gets a tier (Hero, via ceil) and is not dropped.

    WHY: the ceil on the band cutoffs guarantees a tiny day still seeds a Hero rather
    than rounding the top band to zero and leaving the one item tier-less.
    """
    tiered = assign_density_tiers([_scored("solo", score=9.0)])
    assert len(tiered) == 1
    assert tiered[0].density_tier == "hero"


# --- DoD #2 — forced-index routing regardless of score ----------------------


def test_classification_failed_item_forced_to_index_despite_highest_score() -> None:
    """A classification-FAILED item lands in ``index`` even with the TOP score.

    WHY: design decision 5/6 — an item that fails an axis (noise OR off-topic) is
    routed to "they also posted", NEVER to Hero, no matter how high it scores. We give
    the failed item the SINGLE HIGHEST score in the batch; if tiering went purely by
    score it would be Hero. It MUST be ``index`` instead. A regression that dropped the
    forced-index check (tiering on score alone) fails this outright.
    """
    # The noisy item has the highest score; the rest pass classification.
    noisy_top = _scored("noisy_top", score=1000.0, axis_a_signal=0, axis_b_on_topic=1)
    passing = [_scored(f"ok{index}", score=float(50 - index)) for index in range(9)]

    tiered = assign_density_tiers([noisy_top, *passing])
    tier_by_id = {t.scored_item.item.item_external_id: t.density_tier for t in tiered}

    assert tier_by_id["noisy_top"] == "index", (
        "a classification-failed item must land in 'they also posted' (index) regardless of score"
    )
    # And a passing top-scorer DID reach hero — proving the forcing is targeted, not blanket.
    assert tier_by_id["ok0"] == "hero"


def test_off_topic_and_unclassified_items_both_forced_to_index() -> None:
    """Off-topic (axis B 0) AND never-classified (None) items both route to index.

    WHY: both failure shapes — ``is_also_posted`` True via either axis, and a missing
    classification — mean "not a top-line item". Tiering must treat both as forced
    index so an unjudged item never accidentally floats into a top card.
    """
    off_topic = _scored("off_topic", score=900.0, axis_a_signal=1, axis_b_on_topic=0)
    unclassified = _scored("unclassified", score=800.0, classified=False)
    passing = [_scored(f"ok{index}", score=float(10 - index)) for index in range(3)]

    tiered = assign_density_tiers([off_topic, unclassified, *passing])
    tier_by_id = {t.scored_item.item.item_external_id: t.density_tier for t in tiered}

    assert tier_by_id["off_topic"] == "index"
    assert tier_by_id["unclassified"] == "index"


# --- DoD #3 — carryforward resurface-once cap -------------------------------


def test_top_tier_unopened_item_recorded_with_surfaced_count_capped_at_one() -> None:
    """A top-tier unopened item is carried forward with surfaced_count NEVER above 1.

    WHY: the api-contracts ``carryforward`` table resurfaces an un-opened top-tier item
    ONCE — not forever. We mark a hero item as unopened and run the carryforward step
    TWICE (simulating two daily runs where it stayed unopened). After both runs its
    ``surfaced_count`` must be exactly 1. A regression that incremented unbounded (or
    inserted a duplicate row) would show 2+ and resurface the same item every day.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))

        hero_unopened = _scored("hero_unopened", score=100.0)
        tiered = assign_density_tiers([hero_unopened])
        assert tiered[0].density_tier == "hero", "fixture must be top-tier for the carryforward path"

        # Two runs, same item still unopened.
        first = record_top_tier_carryforward(tiered, unopened_ids={"hero_unopened"}, store_module=store)
        second = record_top_tier_carryforward(tiered, unopened_ids={"hero_unopened"}, store_module=store)

        assert first == ["hero_unopened"]
        assert second == ["hero_unopened"], "an unopened top-tier item is recorded each run it stays unopened"

        row = store.get_carryforward("hero_unopened")
        assert row is not None, "the unopened top-tier item must be recorded in carryforward"
        assert row["surfaced_count"] == 1, (
            f"resurface-once: surfaced_count must be capped at 1, got {row['surfaced_count']}"
        )
        assert row["density_tier"] == "hero"


def test_compact_and_index_items_are_not_carried_forward() -> None:
    """Only top-tier (hero/standard) unopened items carry forward — not compact/index.

    WHY: carryforward is for items that were PROMINENT and still went unopened — a
    compact row or a "they also posted" line was never a strong surface, so resurfacing
    it would be noise. We mark a forced-index (failed) item as unopened and assert it is
    NOT recorded.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))

        index_item = _scored("index_item", score=100.0, axis_a_signal=0)  # forced to index
        tiered = assign_density_tiers([index_item])
        assert tiered[0].density_tier == "index"

        recorded = record_top_tier_carryforward(tiered, unopened_ids={"index_item"}, store_module=store)

        assert recorded == [], "a non-top-tier item must not be carried forward"
        assert store.get_carryforward("index_item") is None


def test_no_unopened_ids_records_nothing() -> None:
    """With no unopened ids, the carryforward step is a no-op (the common case)."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tiered = assign_density_tiers([_scored("hero", score=10.0)])
        assert record_top_tier_carryforward(tiered, unopened_ids=None, store_module=store) == []


# --- Edge cases (Rule 12 — degrade, never crash) ----------------------------


def test_all_items_failed_classification_all_index_nothing_dropped() -> None:
    """If EVERY item failed classification, all land in index and none are dropped.

    WHY: a day where nothing passed classification must still render every item in
    "they also posted" — the never-drop invariant holds even at the all-failed extreme,
    and the passing-band math (denominator 0) must not crash.
    """
    scored = [_scored(f"bad{index}", score=float(10 - index), axis_a_signal=0) for index in range(5)]

    tiered = assign_density_tiers(scored)

    assert len(tiered) == len(scored)
    assert all(t.density_tier == "index" for t in tiered), "all-failed batch must be entirely index"


def test_tiered_item_carries_score_and_item_through_unchanged() -> None:
    """TieredItem carries the scored item (and its score) through unchanged.

    WHY: the renderer reads the score and the full RankableItem (title, deep-link
    fields, chapters) off the TieredItem. Tiering must not mutate or replace them.
    """
    original = _scored("keep", score=42.5)
    tiered = assign_density_tiers([original])
    assert tiered[0].scored_item is original
    assert tiered[0].scored_item.score == 42.5


def _run_all_standalone() -> int:
    """Run every ``test_*`` function in this module without pytest. Returns exit code."""
    test_functions = [
        value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for test_function in test_functions:
        try:
            test_function()
            print(f"PASS {test_function.__name__}")
        except Exception as exc:  # noqa: BLE001 — standalone runner surfaces any failure
            failures.append(f"FAIL {test_function.__name__}: {exc!r}")
            print(failures[-1])
    print(f"\n{len(test_functions) - len(failures)}/{len(test_functions)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all_standalone())
