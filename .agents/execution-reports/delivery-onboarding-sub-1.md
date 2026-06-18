# Phase 6 — Sub-phase 1 (Config schema + validation) + Part B (orbit.py M3 wiring)

**Status:** SUCCESS
**Date:** 2026-06-18

## Part A — Config schema + validation

Extended `skills/orbit/scripts/lib/config.py` (stdlib-only, no pydantic) on top of the
existing `ConfigError` / `OrbitConfig` / `load_config` / `_validate_enum`:

1. **Cron validation.** Added the pure, importable helper
   `is_valid_cron_expression(expr: str) -> bool` (reusable by the sub-phase-2 wizard;
   no I/O, no logging) plus `_validate_schedule`. Checks exactly 5 whitespace-separated
   fields, each matching a standard numeric crontab token grammar (`*`, integers, ranges
   `a-b`, lists `a,b`, steps `*/n` / `a-b/n`). Rejects empty / wrong-field-count / bad
   token. Malformed cron raises `ConfigError` naming `schedule` + the bad value, with a
   `fix_suggestion`. Syntax-only by design (OS cron owns value-range semantics); numeric
   grammar means day-name tokens like `mon` are rejected (documented + tested).
2. **Delivery validation** (`_validate_delivery`): `delivery` must be a dict;
   `html_path` (if present) a non-empty string; `imessage_to`/`whatsapp_to` (if present
   and non-null) strings. Phone format intentionally NOT validated (light, per spec).
3. **creator_weights coercion** (`_coerce_creator_weights`): must be a dict; each value
   coerced to float; non-numeric values raise a field-named `ConfigError`
   (`creator_weights.<key>`). Booleans rejected explicitly (bool is an int subclass —
   `float(True)` would silently become 1.0).
4. **Existing behavior intact:** defaults-on-missing-file and the `cookie_source=="env"`
   note are unchanged; all validators run inside `load_config` after the JSON-object
   check.

Created `orbit.config.example.json` (repo root) — a complete, valid-JSON example
matching `reference/api-contracts.md` (cookie_source, 2 creator_weights, interests,
depth, delivery with html_path + imessage_to + whatsapp_to:null, schedule "0 7 * * *").
It parses and validates cleanly through `load_config` (DoD test pins this).

`.env.example` left UNCHANGED — it already contains only empty placeholders
(`AUTH_TOKEN=`, `CT0=`) and a commented WhatsApp Business-API block, satisfying the DoD.
No Twilio block added (the existing commented WhatsApp block covers the optional path;
adding more would be speculative).

## Part B — orbit.py M3 Stage-5 wiring (adopted draft)

Applied `.agents/handoff/phase6-orbit-stage5-wiring.draft.patch` via `git apply`
(`--check` passed, exit 0 — no offset drift, hand-apply not needed). The draft adds:
- `run_stage5_overlap_trending_scoops(items, config, *, store_module=store, search_fn=None)`
  returning `(clusters, trending_items, scoops, trending_multipliers)`; empty items →
  `([], [], [], {})` (M1/M2 quiet path unchanged).
- `trending_multipliers=` threaded into `run_stage6_rank_and_tier`.
- `clusters/trending_items/scoops` threaded into `run_stage7_render`.
- All three wired into `run_pipeline`.
- The wiring test `test_orbit_stage5_wires_overlap_trending_scoops_through_rank_and_render`
  in `tests/test_scoops_and_render.py`.

### fake_store / real-lib verification (the flagged risk)
Read `lib/trending.py` (`compute_history_sample_counts`) and confirmed it calls
`store_module.list_sources()` (no args) and `store_module.get_seen_ids(source_id)`. The
draft's `fake_store = SimpleNamespace(list_sources=lambda: [...], get_seen_ids=lambda
source_id: {"prior"})` matches the real interface EXACTLY — **no fix to the fake was
needed.** It genuinely drives the dormancy path: UC_dorm → source_id 1 → 1 seen id →
`history_sample_count = 1 ≤ SCOOP_DORMANCY_MAX_HISTORY (5)`.

### Scoop fires genuinely (verified by tracing the math)
- The three fixture items (same title) cluster together; all same creator, equal
  priority (weights `{}`), so `max()` picks the first in id-sorted order = `d1` (the
  500k-view breakout) as representative.
- Batch-median engagement baseline for UC_dorm ≈ the tiny sibling blend (~2.35 log-space);
  d1's blend ≈ 11.6 → `baseline_relative_ratio` ≈ 4.9 ≥ `SCOOP_ACCELERATION_MIN_RATIO`
  (2.0). Dormancy (1 ≤ 5) AND acceleration → scoop. Confirmed: the test asserts a scoop,
  a `>1.0` multiplier for `d1`, `d1` ranks first in Stage 6, and Stage 7 HTML contains
  `overlap-block` / `trending-rail` / `scoops-strip`. The fixture did NOT need adjusting;
  assertions were NOT weakened.

## Files created / modified
- MODIFIED: `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/config.py`
- MODIFIED: `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/orbit.py`
- MODIFIED: `/Users/asheshsrivastava/frommyfeed/tests/test_scoops_and_render.py`
- CREATED:  `/Users/asheshsrivastava/frommyfeed/orbit.config.example.json`
- CREATED:  `/Users/asheshsrivastava/frommyfeed/tests/test_config.py` (17 tests)
- UNCHANGED: `/Users/asheshsrivastava/frommyfeed/.env.example` (already placeholder-only)

## Divergences from the draft
None. The patch applied cleanly and the fake_store shape was already correct against the
real libs, so no adaptation was required.

## Review findings + fixes
- Ruff: clean on all touched files (exit 0).
- AST parse: clean for config.py and orbit.py.
- LOW (accepted, not fixed): a string `"inf"`/`"nan"` would coerce to a float weight
  (Python accepts these). Per the brief's "light validation" directive the value is still
  numeric; special-casing would be over-engineering. No critical/high/medium issues found.

## Validation
- `uv run --with pytest pytest tests/ -q` → **133 passed** (baseline 115 + 1 wiring test
  + 17 new config tests). 1 fix-and-rerun attempt was NOT needed; passed first run.
- `python3 -c "import ast; ast.parse(...config.py); ast.parse(...orbit.py)"` → AST OK.

## Definition of done
- [PASS] A test asserts a valid config loads into OrbitConfig
  (`test_valid_config_loads_into_typed_orbit_config`).
- [PASS] A test asserts EACH invalid field raises a clear field-named ConfigError
  (bad cookie_source, bad depth, malformed cron x5 parametrized, bad delivery,
  empty html_path, non-string imessage_to, non-numeric + boolean creator_weights).
- [PASS] A test asserts `orbit.config.example.json` parses + validates as a complete
  example (`test_example_config_parses_and_validates`).
- [PASS] A test asserts `.env.example` contains only placeholders
  (`test_env_example_contains_only_placeholders`).
- [PASS] The wiring test passes and genuinely exercises
  cluster→trending→scoop→multiplier→rank→render on fixtures with no live boundary;
  empty-items returns `([], [], [], {})`.

## Concerns
- None blocking. The fake_store matched the real lib interface exactly and the scoop
  fired genuinely off the fixture math (no assertion weakening, no fixture fudging).
- The cron grammar is numeric-only (no day-name aliases like `mon`/`jan`). The wizard
  (sub-phase 2) emits numeric 5-field cron, so this is correct for Orbit's own output;
  flagged in case a future feature wants to accept name aliases.
