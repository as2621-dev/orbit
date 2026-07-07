# Phase 8 — Sub-phase 4 execution report: Cron auto-install + scope trims + decision record

## What I implemented
- **Cron auto-install** (`scripts/lib/setup_wizard.py`):
  - New `install_cron_entry(cron_entry, *, crontab_runner=_default_crontab_runner) -> bool`.
    Reads the current crontab via the injected runner, drops any line containing the marker
    `# orbit-daily-digest`, appends the entry tagged with that marker, and pipes the result
    to `crontab -`. Idempotent: a re-run replaces the single Orbit line, never duplicates.
  - New injectable boundary `CrontabRunner = Callable[[list[str], Optional[str]], subproc.SubprocResult]`
    and production impl `_default_crontab_runner` (uses `subprocess.run` with an **argv list**,
    never `shell=True`; captures stdout/stderr; 15s timeout). Mirrors bird_x.py's `subproc`
    injection posture so tests never touch the real crontab.
  - Helper `_existing_crontab_lines` interprets `crontab -l`: rc==0 → lines verbatim; rc!=0
    with empty or "no crontab" stderr → empty (fresh start); rc!=0 with any other stderr →
    `None` (genuine read failure → fail soft, refuse to clobber).
  - **Fail-soft**: a missing binary, an unreadable crontab, or a non-zero write each log
    `setup_cron_install_failed` with a `fix_suggestion` and return `False`. Wired into
    `run_setup_wizard` step 5: on `False` it falls back to printing the entry for manual
    `crontab -e` pasting, so a sandboxed/CI run still completes (exit 0).
- **Fixed schedule**: deleted `_gather_schedule`; `run_setup_wizard` now uses
  `DEFAULT_SCHEDULE` (`0 7 * * *`) directly. The wizard no longer prompts for a schedule.
  The config still writes `schedule` (api-contracts shape unchanged). `generate_cron_entry`
  and its validation untouched.
- **README** (`README.md`): quick start now says the wizard installs the cron entry itself at
  a fixed 7am (with the marker-tagged line and the manual-fallback note); shipped bullets note
  the ≥10-min long-form floor, the fixed `{ai, business, tech, sports, other}` taxonomy with
  `other` dropped, and the X top-8 virality cut; a new **Delivery** section states iMessage is
  the delivery channel and email is explicitly out of scope (decision 2026-07-06).
- **Master-plan** (`plans/master-plan.md`): Key Decision 6 gets a dated supersession note — X
  half now selects top-N (8) by virality (Phase 8); note is explicit that the original
  density-not-inclusion rule **still fully governs YouTube** (superseded, not blended, Rule 7).
- **Tests** (`tests/test_setup_wizard.py`): added an in-memory `_FakeCrontab` runner double;
  updated the 4 existing wizard tests to inject it and drop the now-dead schedule answer; added
  8 new tests covering every DoD clause.

## Files modified
- `scripts/lib/setup_wizard.py`
- `tests/test_setup_wizard.py`
- `README.md`
- `plans/master-plan.md`
- (`scripts/lib/config.py` NOT touched — `DEFAULT_SCHEDULE` already lived there; no constant
  needed to move.)

## Divergences from spec + why
- Spec suggested tagging happens on the installed line; I have `install_cron_entry` append the
  marker (`generate_cron_entry` stays untagged, as required). No divergence, noting the split.
- Added one test beyond the literal DoD list
  (`..._fails_soft_on_unreadable_crontab_without_clobbering`) to prove a genuine (non "no
  crontab") read error does NOT overwrite an existing crontab it failed to read — a real
  data-loss guard the "no crontab → empty" rule could otherwise mask.

## Self code-review findings + fixes
- **Subprocess safety**: `_default_crontab_runner` uses an argv list, no `shell=True`, no
  string interpolation of user input into a shell. PASS.
- **Idempotency**: marker-match drops ALL marker-bearing lines then appends exactly one; test
  asserts `count(marker) == 1` and that unrelated user jobs survive. PASS.
- **Fail-soft actually completes the wizard**: integration test drives a failing write end to
  end and asserts exit 0 + printed fallback + logged failure. PASS.
- **Secrets**: install logs only the marker string, return codes, and crontab stderr (the cron
  line is repo-path + `claude -p`, no credentials); nothing from `.env` is read or logged. PASS.
- No critical/high issues found; nothing left unfixed.

## Validation results
- `pytest tests/test_setup_wizard.py tests/test_readme_packaging.py -q` → **26 passed**.
- Full `pytest tests/ -q` → **250 passed** (baseline was ≥242; sub-phases 1-3's additions
  included, all green).
- `ruff check scripts/lib/setup_wizard.py tests/test_setup_wizard.py` → All checks passed.
- `setup_wizard.py` is 639 lines (under the 1000-line limit). `orbit.py` not touched by me.

## Definition of done — PASS
- Fresh install writes the marker-tagged line — PASS.
- Second run replaces, not duplicates — PASS.
- "no crontab for user" treated as empty — PASS.
- Failing crontab binary degrades to the printed entry + logs the failure — PASS.
- Wizard no longer prompts for a schedule; written config carries `0 7 * * *` — PASS.
- `pytest test_setup_wizard.py test_readme_packaging.py` green — PASS.
- README + master-plan diffs reviewed — PASS.

## Concerns
- None blocking. The "no crontab → empty" idiom means a `crontab -l` that fails with an
  unexpected-but-benign stderr would fail soft to print rather than install; that is the safe
  direction (never clobber) and is covered by a test.
