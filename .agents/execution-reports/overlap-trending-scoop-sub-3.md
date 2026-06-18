# Execution report — Phase 5, Sub-phase 3: External trending (corroboration vs scoop)

**Status:** SUCCESS
**Date:** 2026-06-18

## What shipped
- `external_trending.tag_external_corroboration(trending_items, *, search_fn=keyless_search, depth="default", result_threshold=CORROBORATION_RESULT_THRESHOLD) -> list[TrendingItem]`:
  for the TOP internal-trending items (input assumed ranked descending by velocity), runs a light
  KEYLESS external cross-search and tags each `corroborated` (>= threshold external results) vs
  `scoop` (near-zero external signal). Classification is a DETERMINISTIC count threshold
  (`_classify_corroboration`), NOT an LLM call (Rule 5). Bounded by `depth` via
  `DEPTH_CROSS_SEARCH_BUDGET` (cost control); items beyond the budget stay untagged (no egress).
- `web_search_keyless.keyless_search(query, *, count, page_fetcher)` — the keyless DDG-HTML cross-search,
  with the live network call funnelled through ONE injectable `page_fetcher: (url) -> str` boundary
  (`default_page_fetcher`, stdlib `urllib.request`). No `lib.http` (which the reference imports and Orbit
  lacks); no new pip dependency.
- DoD tests in `tests/test_external_trending.py` (12 tests).

## MANDATORY refactor — trending.py split (the load-bearing change this sub-phase)
`trending.py` was **733 lines** on disk (NOT the 574 the brief estimated) — a prior interrupted run had
dumped THREE concerns into it: Sub-phase 2 internal trending, Sub-phase 3 external corroboration (mine),
AND Sub-phase 4 scoop detection + rerank-multiplier (`detect_scoops`, `build_trending_multiplier_map`,
the scoop/rerank constants). Removing ONLY my external half left it at 552 lines — still over the 500-line
CLAUDE.md hard limit. There was no way to get under 500 by relocating only my own functions.

**Resolution (Rule 7 — pick one, justify):** created
`/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/external_trending.py` as the home for
EVERYTHING that operates on `TrendingItem` AFTER internal ranking, and relocated BOTH halves into it:
- **Sub-phase 3 (mine):** `CORROBORATION_TAG_*`, `CORROBORATION_RESULT_THRESHOLD`,
  `EXTERNAL_RESULTS_PER_SEARCH`, `DEPTH_CROSS_SEARCH_BUDGET`, `_DEFAULT_CROSS_SEARCH_BUDGET`,
  `_classify_corroboration`, `tag_external_corroboration`, and the `from lib.web_search_keyless import
  SearchFn, keyless_search` boundary.
- **Sub-phase 4 (stray, relocated VERBATIM — logic untouched):** `SCOOP_DORMANCY_MAX_HISTORY`,
  `SCOOP_ACCELERATION_MIN_RATIO`, `SCOOP_RERANK_MULTIPLIER`, `TRENDING_RERANK_MULTIPLIER`,
  `detect_scoops`, `build_trending_multiplier_map`.

Both halves are unreviewed prior-run strays; I OWN and reviewed my half, and RELOCATED (not modified)
the Sub-phase 4 half — the only path to satisfy the <500 hard constraint without touching internal logic.
`external_trending.py` imports `TrendingItem` from `trending.py` (one-way dependency, no cycle).

**Confirmation internal-trending logic is unchanged:** `trending.py` is now 395 lines of ONLY the
Sub-phase 2 internal half (`TrendingItem`, `_representative_item`, `compute_history_sample_counts`,
`compute_internal_trending`). The only edits to `trending.py` were: (1) removed the now-unused
`from lib.web_search_keyless import ...` line; (2) updated the module-docstring paragraph that said
Sub-phases 3/4 "EXTEND this module" to point at the sibling `lib.external_trending`. NO function body in
the internal half was touched — proven by `tests/test_trending.py` (8 tests asserting exact velocity-score
math, convergence ordering, and baseline-relative ratios) still passing unchanged.

## What I FOUND in each stray draft + what I changed
- **`web_search_keyless.py`** (stray): a complete, sound keyless DDG-HTML search with a frozen
  `SearchResult` dataclass, `SearchFn`/`PageFetcher` type aliases, an injectable `default_page_fetcher`
  (stdlib `urllib.request`, never raises -> ""), `keyless_search` (never raises -> []), positional
  snippet-windowing parse, and `uddg=` redirect unwrap. Reviewed against the reference
  (`last30days-skill/.../web_search_keyless.py`): the reference imports `from . import http` (absent in
  Orbit) — this draft correctly substitutes the injectable `page_fetcher` seam instead. **Adopted as-is**
  (no logic change needed); it already meets the "one injectable fetcher, mocked in tests, stdlib-only"
  spec. Ruff clean.
- **`tag_external_corroboration` + `_classify_corroboration` + corroboration constants** (stray, were
  in `trending.py`): reviewed against the ACTUAL `TrendingItem` shape Sub-phase 2 shipped
  (`item_external_id`, `cluster_id`, `creator_external_id`, `title`, `card_url`, `velocity_score`,
  `convergence_count`, `baseline_relative_ratio`, `corroboration_tag: str = ""`, `is_scoop: bool = False`).
  The draft tags off the reserved `corroboration_tag` field correctly, honors the injectable
  `result_threshold`, breaks the loop at the depth budget, and degrades blank-title/raising/empty searches
  to a safe `scoop`. **Adopted (logic correct), RELOCATED** to `external_trending.py`.
- **`tests/test_external_trending.py`** (stray, 12 tests): reviewed each test's intent against the spec.
  They correctly pin: (#1) the deterministic corroboration-vs-scoop threshold incl. the exact `>=` boundary;
  (#2) `depth="quick"` caps cross-searches + the budget map is strictly monotonic; (#3) empty/raising/empty-
  return/blank-title degrade safely with no crash; (#4) the keyless module parses injected HTML with no live
  call, short-circuits a blank query without fetching, never raises out of a raising fetcher, and exposes the
  stdlib seam. **Changed only the import block:** the corroboration symbols now import from
  `lib.external_trending`; `TrendingItem` still imports from `lib.trending`. No test logic changed.
- **`.agents/execution-reports/overlap-trending-scoop-sub-3.md`** (stray, pre-written): VERIFIED against
  reality and REWRITTEN (this file). The stray report claimed a SURFACED CONFLICT — that `test_trending.py`
  failed at import against the new store-injection API. **That is now STALE/RESOLVED:** `test_trending.py`
  passes (8 tests) against the current `compute_internal_trending(clusters, items_by_id, store_module, *,
  creator_baselines=...)` API. The stray report also predated the trending.py split and the discovery of the
  co-resident Sub-phase 4 content. Do not trust the stray version.

## Files touched (absolute)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/external_trending.py` (CREATED — external + scoop halves relocated)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/trending.py` (REMOVED the relocated external/scoop functions + unused import; updated one docstring paragraph; internal logic untouched)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/web_search_keyless.py` (reviewed/adopted as-is)
- `/Users/asheshsrivastava/frommyfeed/tests/test_external_trending.py` (reviewed; import block repointed to `lib.external_trending`)

## Divergences from the brief
- **Brief said trending.py was 574 lines + internal/external only.** Reality: 733 lines including stray
  Sub-phase 4 content. Forced the broader split (external_trending.py also hosts the Sub-phase 4 funcs) —
  the only way to hit <500 without touching internal/Sub-phase-4 logic. Flagged for Sub-phase 4 below.
- **`tag_external_corroboration` / `detect_scoops` / `build_trending_multiplier_map` import path moved**
  from `lib.trending` to `lib.external_trending`. The phase plan listed these under `trending.py`; they now
  live in the sibling module. A stray `tests/test_scoops_and_render.py` (Sub-phase 4 draft) already imports
  them from `lib.external_trending`, consistent with this split.

## Self-review findings + fixes
- **[fixed]** `trending.py` still imported `from lib.web_search_keyless import SearchFn, keyless_search`
  after the external functions were relocated — unused (Ruff F401). Removed. `trending.py` now imports no
  network boundary at all (reinforces Rule 5: the internal half is pure math).
- **[fixed]** The `trending.py` module docstring claimed Sub-phases 3/4 "EXTEND this module" with
  `tag_external_corroboration`/`detect_scoops`. Stale after the split — updated to point at
  `lib.external_trending`.
- **[verified]** No import cycle: `external_trending` -> imports `trending` (for `TrendingItem`) +
  `web_search_keyless`; `trending` imports neither. One-way.
- **[verified]** `test_trending.py`'s `test_trending_and_signals_have_no_network_or_llm_imports` still holds:
  `trending.py` has no `urllib.request`/`openai`/`httpx` import (the urllib egress lives only in
  `web_search_keyless.py`, reached via the injected seam from `external_trending.py`).
- **[verified, ruff]** `ruff check` clean on all four touched files.

## Exact validation outputs
- `ast.parse` of the 3 lib files: **`ast ok`**.
- `from lib import web_search_keyless, external_trending, trending, signals, cluster`: **`import ok`**.
- `pytest tests/test_external_trending.py tests/test_trending.py -q`: **20 passed** (12 external + 8 trending).
- Per-file line counts (ALL < 500): `trending.py` **395**, `external_trending.py` **384**,
  `web_search_keyless.py` **273**.
- Full suite `pytest tests/ -q`: **115 passed, 0 failures** (>= 110 required). No regressions.
- `ruff check` (external_trending, web_search_keyless, trending, test_external_trending): **All checks passed!**

## Definition of done — PASS/FAIL (the 3 criteria)
1. **Many external results -> `corroborated`, near-zero -> `scoop` (deterministic threshold).** **PASS**
   (`test_many_external_results_tags_corroborated_and_near_zero_tags_scoop`,
   `test_result_count_exactly_at_threshold_is_corroborated_boundary` pins the exact `>=` boundary).
2. **`depth="quick"` CAPS the number of cross-searches (cost control), fewer than a deeper depth.** **PASS**
   (`test_depth_quick_caps_number_of_cross_searches` asserts call count == quick budget with more items than
   the budget + beyond-budget items untagged; `test_deeper_depth_allows_more_cross_searches_than_quick` pins
   quick < default < deep).
3. **Cross-search boundary mocked (no live web) — structural + behavioral.** **PASS** (keyless module imports
   stdlib + `lib.log` only, no new pip dep; `test_keyless_module_imports_only_stdlib_and_lib_no_new_dependency`;
   injected fake fetcher parses HTML with no network; raising fetcher -> [] not crash; raising `search_fn` ->
   safe `scoop`).

## SECURITY note (CSO) — keyless egress leaks no secret / no PII
- **Keyless / no secret:** the only external egress is `web_search_keyless.default_page_fetcher` hitting the
  public DuckDuckGo HTML endpoint. NO API key, NO token, NO cookie, NO auth header, NO SearXNG config — only
  a plain static User-Agent and the public title string in the query. Nothing is read from `.env`.
- **Only the public title goes out:** `tag_external_corroboration` sends `trending_item.title.strip()` as the
  query — a public headline, never a user id, creator id, or any PII.
- **No secret/PII logged:** the warnings carry only `error_type`, `cluster_id`, counts, `depth`, and a
  `fix_suggestion`; the query string is NOT logged. `default_page_fetcher`/`keyless_search` never raise.
- **Cost bounded:** `DEPTH_CROSS_SEARCH_BUDGET = {quick: 3, default: 8, deep: 15}`; the loop breaks once
  `searches_issued >= budget`, so a busy day cannot fire unbounded web requests. Unknown depth -> the
  `default` budget (never unbounded).

## Handoff for Sub-phase 4 (anomaly/scoop detection + render the M3 sections)
- **Import path CHANGED:** Sub-phase 4's `detect_scoops` and `build_trending_multiplier_map` now live in
  `lib.external_trending`, NOT `lib.trending`. Import from `lib.external_trending`. (They were relocated there
  verbatim — logic unchanged — to keep `trending.py` under the 500-line limit. A stray
  `tests/test_scoops_and_render.py` already imports them from `lib.external_trending`.)
  Likewise the scoop/rerank constants (`SCOOP_DORMANCY_MAX_HISTORY`, `SCOOP_ACCELERATION_MIN_RATIO`,
  `SCOOP_RERANK_MULTIPLIER`, `TRENDING_RERANK_MULTIPLIER`) live in `lib.external_trending`.
- **Two docstring references to fix:** `render.py:456` and `rerank.py:536` still say
  `:func:`lib.trending.detect_scoops`` / `:func:`lib.trending.build_trending_multiplier_map``. Update those
  `:func:` paths to `lib.external_trending.*` when Sub-phase 4 wires the multiplier. (They are docstrings, not
  imports, so they don't break anything today.)
- **Corroboration tag consumer (the scoops strip):** Sub-phase 4 reads `TrendingItem.corroboration_tag`. The
  stable string values are:
  - `lib.external_trending.CORROBORATION_TAG_CORROBORATED == "corroborated"` (also big outside the network)
  - `lib.external_trending.CORROBORATION_TAG_SCOOP == "scoop"` (your people first — little external signal)
  - `lib.external_trending.CORROBORATION_TAG_UNTAGGED == ""` (beyond the depth budget / never searched)
- **`tag_external_corroboration` signature (post-split):**
  `lib.external_trending.tag_external_corroboration(trending_items: list[TrendingItem], *, search_fn: SearchFn = keyless_search, depth: str = "default", result_threshold: int = CORROBORATION_RESULT_THRESHOLD) -> list[TrendingItem]`.
  It mutates each item's `corroboration_tag` in place and returns the same list.
