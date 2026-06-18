# Execution report — Phase 3, Sub-phase 4: Page-budget spill + pipeline wiring (Stage 7b)

## What was built

**`skills/orbit/scripts/lib/render.py`** (EXTENDED — pagination built from scratch):
- `estimate_page_height(tiered_items) -> int` — pure, no I/O. Per-tier px table
  (`TIER_HEIGHT_PX`: Hero 220 / Standard 150 / Compact 44 / Index 30) + `CHAPTER_HEIGHT_PX`
  (26 px) per chapter on Hero/Standard cards + fixed `PAGE_CHROME_PX` (120) once.
  Documented as a FIRST-CUT tunable estimate-by-content (no headless browser).
- `PAGE_1_BUDGET_PX = 1400` (tunable, ~one tall screen).
- `render_digest_pages(tiered_items, config, *, page_2_href=DEFAULT_PAGE_2_FILENAME) -> list[str]`
  — returns `[page1]` when within budget, `[page1, page2]` when over. Spill is
  TIER-BASED: Hero+Standard stay on page 1 (gains a "Continued on page 2 →" link via the
  allowlisted `render_link`); Compact+Index go to page 2. HARD 2-page cap (only ever two
  `wrap_page` calls — page 2 takes all spill even if it would itself overflow).
- Helpers: `_split_grouped_for_spill`, `_build_body` (reuses the sub-phase-3 section
  builders unchanged), `_render_continued_link`. `DEFAULT_PAGE_2_FILENAME = "today-page2.html"`.
- `render_digest_html` now delegates to `render_digest_pages(...)[0]` — same single-string
  contract for the 8 sub-phase-3 tests (small fixtures stay single-page, no spill link).
- `render_completed` log gained additive `estimated_height_px` / `page_count` / `spilled`.

**`skills/orbit/scripts/orbit.py`** (MODIFIED — additive Stage 6→7 wiring, wiring-only/Rule 5):
- `run_stage6_rank_and_tier(items, config) -> list[TieredItem]` — `derank_items` + `assign_density_tiers`.
- `run_stage7_render(tiered_items, config, *, html_path=None, writer=_default_html_writer) -> list[Path]`
  — renders pages, writes page 1 to `config.delivery["html_path"]` (default `~/orbit/out/today.html`,
  `expanduser()`, `mkdir(parents=True)`), and `today-page2.html` beside it when spilled. `writer`
  and `html_path` both injectable so tests never touch the real per-user path.
- `_default_html_writer`, `_resolve_html_path`, `DEFAULT_HTML_PATH`.
- `run_pipeline` now logs stages 1-2 (`_STUBBED_UPSTREAM_STAGES`) as not-yet-implemented, then
  runs the REAL Stage 6→7 on `rankable_items` (empty until upstream lands in a later phase) and
  writes the digest. Documented that the upstream delta/classify/chapterize producers are stubs.

**Tests:**
- `tests/test_render.py` — +3 spill tests + a `TIER_COMPACT` import and a `_many` helper.
- `tests/test_orbit_pipeline.py` — NEW, 3 end-to-end tests over a mocked upstream (RankableItems
  built directly; no network/LLM/cookies; writes to `tmp_path`).

## Self-review findings + fixes
- **[Med] real-path write on bare CLI run** — `orbit.py --depth quick` writes to
  `~/orbit/out/today.html`. This is intended M1 behaviour (the phase ships an HTML file), the dir
  is gitignored and outside the repo tree, and the run is re-runnable/overwrites its own output.
  No fix — by design.
- **[Low] spill link in `render_digest_html` string-only path** — when oversized, the single-string
  `render_digest_html` returns page 1 WITH the link but no page-2 file is written by that caller.
  Documented in the docstring; orbit.py uses `render_digest_pages` which writes both. Acceptable.
- **Path safety** — `expanduser()` + `mkdir(parents=True, exist_ok=True)` verified; no swallowed errors.
- File sizes: render.py ~360 LOC, orbit.py ~410 LOC — both well under 1000.

## Validation results
- `uv run --with pytest pytest tests/ -q` → **72 passed** (66 pre-existing incl. the 8 sub-phase-3
  render tests STILL passing + 6 new: 3 spill, 3 pipeline). PASS.
- `ORBIT_STAGE0_SKIP_NETWORK=1 python3 skills/orbit/scripts/orbit.py --depth quick` → **exit 0**,
  no network; rank+render ran on an empty batch and wrote a valid single-page digest.
- `python3 -c "... from lib import render; import orbit; print('import ok')"` → `import ok`.

## Definition of done

Sub-phase 4 (4 bullets) — **PASS**:
1. Small set → single page, no page-2 file, no spill link — `test_small_digest_is_single_page_with_no_spill_link`.
2. Over-budget → page 2 emitted, page-1 has the link, Hero/Standard stayed, Compact/Index moved
   (hero id on p1 NOT p2; compact/index id on p2 NOT p1) — `test_oversized_digest_spills_low_tiers_to_page_two`.
3. 2-page hard cap even when page 2 alone overflows — `test_two_page_hard_cap_holds_even_when_page_two_overflows`.
4. orbit.py end-to-end over mocked pipeline writes a non-empty digest to a temp html_path with a
   surviving `&t=90s` deep-link — `test_end_to_end_writes_digest_with_surviving_deep_link`
   (+ default-writer/config-path test + end-to-end spill/2-page-cap test).

Phase-level DoD — **PASS**: end-to-end over mocked Phase 1-2 produces a self-contained
`<!DOCTYPE html>` digest at the configured html_path; every item lands in a tier (`len(tiered)==len(items)`,
nothing dropped); a chapterized item shows a working `watch?v=ID&t=90s` deep-link in the WRITTEN file;
an oversized digest spills Compact+Index to a linked `today-page2.html` capped at 2 pages.

## Concerns
None blocking. `PAGE_1_BUDGET_PX` and the per-tier px constants are explicitly first-cut/tunable
(the master-plan riskiest-assumption test). Upstream stages 1-2 (delta/classify/chapterize) remain
stubs by design this phase — the rank+render half is real.

## Sample HTML
Eyeball samples written to the gitignored `out/` (NOT in repo tree, confirmed via `git check-ignore`):
`out/sample-page1.html` (Hero+Standard + "Continued on page 2 →") and `out/sample-page2.html`
(Compact+Index) — 52 items, est 2366 px → 2 pages; hero on p1 only. The bare CLI run also wrote
`~/orbit/out/today.html` (empty-batch single page, intended M1 output location).
