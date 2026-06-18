# Sub-phase 2 report — Assign density tiers (Stage 6b)

**Status:** SUCCESS — DoD PASS. (Work was started by a prior orchestrator and
left uncommitted; this report documents the validation done on resume.)

## What was implemented
- `skills/orbit/scripts/lib/density.py` — `assign_density_tiers(scored_items) -> list[TieredItem]`
  mapping the descending-sorted `ScoredItem` distribution to
  `hero | standard | compact | index` by **rank position** over the passing
  distribution (proportional cutoffs `HERO_FRACTION=0.10`, `STANDARD_FRACTION=0.25`,
  `COMPACT_FRACTION=0.35`, `ceil`-rounded so a tiny day still seeds a Hero), NOT a
  hard top-N. Forced-index routing: an item that failed classification
  (`Classification.is_also_posted` True, or `classification is None`) is forced into
  `index` regardless of score. `len(out) == len(in)` always — nothing dropped.
- `record_top_tier_carryforward(tiered_items, unopened_ids, *, store_module=store)`
  kept SEPARATE from the pure tiering function (side-effecting store write). Records
  only `hero`/`standard` unopened items via `store.record_carryforward`.

## Files created / modified
- `skills/orbit/scripts/lib/density.py` (created)
- `tests/test_density.py` (created — 10 tests)
- `skills/orbit/scripts/store.py` (MODIFIED — additive: `record_carryforward` /
  `get_carryforward` + `CARRYFORWARD_SURFACED_COUNT_CAP=1`)

## Divergence from the plan (flagged)
- **store.py was added to the touched-files set.** Sub-phase 2's carryforward DoD
  needs store helpers that did not exist; `store.py` was not in the phase file's
  "Files touched". Resolved per the planning-gap note in the progress file: the
  change is purely **additive** (two new functions + one constant) on the existing
  `carryforward` table whose schema already shipped in Phase 1. No existing code
  changed. The `carryforward` table has no UNIQUE on `item_external_id` in the v1
  schema, so the resurface-once cap is enforced in `record_carryforward` (insert with
  `surfaced_count=1`, else clamp on update) rather than a schema constraint.

## Validation results (on resume)
- `uv run --with pytest pytest tests/test_density.py -q` → 10 passed.
- `uv run --with pytest pytest tests/test_rerank.py tests/test_density.py -q` → 21 passed.
- Full suite `uv run --with pytest pytest tests/ -q` → 58 passed.
- `python3 -c "import ..."` imports clean (density imports store + lib.rerank + lib.log).

## Definition of done: PASS
1. `len(tiered) == len(scored)` — `test_every_scored_item_receives_a_tier_nothing_dropped`
   asserts exact length AND id-set/order preservation. PASS.
2. Classification-failed item lands in `index` despite the SINGLE HIGHEST score —
   `test_classification_failed_item_forced_to_index_despite_highest_score`. PASS.
3. Top-tier unopened item recorded in `carryforward` with `surfaced_count` capped at 1
   across TWO runs — `test_top_tier_unopened_item_recorded_with_surfaced_count_capped_at_one`.
   PASS.

## Concerns for the orchestrator
- The store.py additive change must be staged in the phase commit (it is part of the
  phase intent, per the planning-gap resolution). Surfaced here for the final report.
- "Did the user open it?" has no signal source in M1 — the unopened-id set is passed
  IN by the caller. orbit.py wiring (sub-phase 4) currently has no open-tracking source,
  so carryforward will be a no-op in the live M1 pipeline until a signal exists (M3+).
