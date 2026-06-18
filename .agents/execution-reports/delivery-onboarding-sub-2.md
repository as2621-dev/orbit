# Phase 6 (delivery-onboarding) — Sub-phase 2: `/orbit --setup` wizard + cron-entry generation

**Status:** SUCCESS

## What was implemented

1. **`generate_cron_entry(schedule, command=None, *, repo_path=None) -> str`** — a PURE,
   deterministic function (Rule 5). Validates the schedule via
   `lib.config.is_valid_cron_expression` (reused, not duplicated) and raises `ValueError`
   (fail loud, Rule 12) on a malformed cron. The default command reflects brief §8.3 step 5
   / §2: `cd <repo> && claude -p "/orbit"`, with `repo_path` injectable for testability.

2. **`run_setup_wizard(...)`** — the interactive wizard (brief §8.3, 5 steps):
   - Reads YouTube subs (injectable `youtube_loader`, fatal on auth failure) + X follows
     (injectable `x_loader`, best-effort — `XAuthError` logged + swallowed, mirroring
     orbit.py Stage 0 so a YouTube-only user still gets a config).
   - Auto-classifies each creator via the EXISTING `classify.classify_item` path (no
     separate classifier) with an injectable `llm_classifier`; user confirms/flips each
     category.
   - Picks priority creators -> `creator_weights` (weight 2.0), seeds `interests` from
     subscription display names (lower-cased, de-duped, order-preserved).
   - Gathers delivery (`html_path` + optional opt-in `imessage_to`) and a validated cron
     schedule (re-prompts once, then falls back to default).
   - Writes `orbit.config.json` (api-contracts shape) to an injectable `config_path`, then
     prints + logs the cron entry.
   - All interactivity routes through an injectable `input_fn` (defaults to `input`).
   - Returns an `int` exit code (0 on success).

3. **orbit.py wiring** — replaced the `run_setup()` stub body to delegate to
   `run_setup_wizard(llm_classifier=_default_llm_classifier)` (real defaults: live loaders,
   the module-level classify boundary, builtin `input`, `./orbit.config.json`). Added the
   import and updated the `--setup` help text. `main()`/`build_argument_parser` untouched;
   orbit.py stays wiring-only.

4. **SKILL.md** — documented `/orbit` (daily run) and `/orbit --setup` (the 5-step wizard).
   Removed the "Stub for now" / "Scaffold only" language.

## Files created / modified

- CREATED `skills/orbit/scripts/lib/setup_wizard.py` (483 lines, under the 500 limit)
- MODIFIED `skills/orbit/scripts/orbit.py` (import + help text + `run_setup` body)
- MODIFIED `skills/orbit/SKILL.md`
- CREATED `tests/test_setup_wizard.py` (6 tests)
- CREATED this report

## Divergences (+ why)

- **Added an injectable `store_module` parameter** to the wizard (threaded into the classify
  call). NOT in the original spec's explicit injectable list, but REQUIRED: `classify_item`
  reads/writes the SQLite store, so without this the wizard's auto-classify would write to
  the real per-user DB (`~/.local/share/orbit/orbit.db`) during tests — violating the
  directive's "IO all mocked — NO live calls" rule. With it, tests inject a mock store and
  auto-classify is fully offline. Verified: running only `test_setup_wizard.py` against a
  freshly-deleted DB dir leaves NO real DB behind. (The full suite still creates the DB, but
  that is a pre-existing baseline test's behavior, not mine — left untouched per Rule 3.)
- **Priority weight = 2.0** (a constant). The brief leaves the exact value to the
  maintainer; 2.0 matches the api-contracts example and is user-editable in the config.

## Self-review findings + fixes

- All boundaries injectable (loaders / input / llm / config-path / store) — confirmed.
- Rule 5 honored: only `classify_item` (the model) makes a judgment; cron-building, weight
  assembly, interest seeding, and IO are deterministic.
- Security: no secrets logged; `cookie_source` is a non-secret browser name; no credential
  fields reach any log.
- **Fix:** ruff flagged one unused import (`Follow`) in the test — removed.

## Validation

- `ast.parse` clean on `setup_wizard.py` and `orbit.py`.
- `ruff check` on all touched source/test files: **All checks passed**.
- `uv run --with pytest pytest tests/ -q`: **139 passed** (133 baseline + 6 new). PASS.

## Definition of done — PASS

- [x] Wizard with mocked loaders + mock llm_classifier + scripted input writes a valid
      `orbit.config.json` that loads back through `lib.config.load_config` and carries the
      chosen `creator_weights`, seeded `interests`, and `schedule`.
- [x] `generate_cron_entry("0 7 * * *", ...)` returns a valid crontab line containing
      `claude -p "/orbit"`.
- [x] `generate_cron_entry` rejects a malformed cron (fail loud).
- [x] Wizard auto-classifies via the existing classify path (injected `llm_classifier`
      asserted CALLED — proves `classify_item`, not a separate classifier).
- [x] Edge/failure: X-loader `XAuthError` -> wizard continues YouTube-only and still writes
      a valid config. (Plus a bonus test: provided iMessage number is persisted.)
- [x] Loaders / LLM / input / IO all mocked — no live calls.

## Concerns

- **Confirmed: the wizard uses the existing `classify_item` path** with an injectable LLM
  boundary (a test asserts the mock was called). No separate classifier exists.
- **Confirmed: the written config validates through `load_config`** (a test loads it back
  and asserts the carried-through fields).
- `setup_wizard.py` is 483 lines — close to the 500-line limit, but the bulk is docstrings;
  logic is modular (small helpers). If it grows, the prompt-gathering helpers could move to
  a sibling module.
- The full test suite leaves a real `~/.local/share/orbit/orbit.db` behind, caused by a
  PRE-EXISTING test (not mine and not in my file set) — flagged but out of scope for this
  surgical sub-phase.
