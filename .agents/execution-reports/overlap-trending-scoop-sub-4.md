# Execution report — Phase 5 (M3) Sub-phase 4: scoop detection + M3 render sections

**Status:** SUCCESS
**Date:** 2026-06-18

## Implemented

1. **Scoop detection + rerank-multiplier bridge** — `detect_scoops` and
   `build_trending_multiplier_map`. A SCOOP requires BOTH **dormancy** (`history_sample_count
   <= SCOOP_DORMANCY_MAX_HISTORY`, default 5) AND **acceleration** (`baseline_relative_ratio
   >= SCOOP_ACCELERATION_MIN_RATIO`, default 2.0) — a deterministic two-threshold AND, no LLM.
   `build_trending_multiplier_map` maps scoops to `SCOOP_RERANK_MULTIPLIER` (1.5) and non-scoop
   trending items to `TRENDING_RERANK_MULTIPLIER` (1.2), both `> 1.0`.
2. **Live trending/scoop multiplier in `rerank.py`** — `score_item` and `derank_items` gained an
   optional keyword-only `trending_multipliers: dict[str, float] | None = None`. The product now
   uses a per-item multiplier (looked up by `item_external_id`, falling back to
   `TRENDING_MULTIPLIER_NEUTRAL` 1.0) in place of the constant no-op. With no map passed, the
   score is byte-for-byte the M1 score. The scoring FORMULA was not rewritten — only the
   multiplier slot was wired.
3. **Three M3 render sections in `render.py`** — `_render_overlap_block` ("Everyone's talking
   about", from clusters; merged short body + cross-links into episode chapter deep-links),
   `_render_trending_rail` (right-rail, tagged corroborated/scoop), `_render_scoops_strip` (loud
   strip, top of body). `_build_body` / `render_digest_pages` / `render_digest_html` gained
   optional `clusters` / `trending_items` / `scoops` args (default None -> sections omitted ->
   M1 page unchanged). M3 sections render on PAGE 1. All links go through `html_render.render_link`
   / `_card_deep_link` (allowlist + escape); deep-links reuse the cards' source-aware logic.

## Files touched (absolute paths)

- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/rerank.py` (multiplier wiring)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/render.py` (3 M3 sections + optional args)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/html_render.py` (**TOUCHED** — CSS only)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/external_trending.py` (`detect_scoops` + `build_trending_multiplier_map`)
- `/Users/asheshsrivastava/frommyfeed/tests/test_scoops_and_render.py` (NEW — 5 DoD tests)

## Divergences from the sub-phase brief

- **`detect_scoops` + the multiplier bridge live in `external_trending.py`, NOT `trending.py`.**
  The brief said extend `trending.py`. By the time this sub-phase ran, Sub-phase 3 had been
  refactored: `tag_external_corroboration` and the Stage-5b/5c surface were split out of
  `trending.py` into a new sibling `external_trending.py` (for the 500-line agent-file
  discipline), and `trending.py`'s own docstring now explicitly names
  `external_trending.detect_scoops` and "the rerank-multiplier bridge" as belonging there.
  Per Rule 11 (conform to the codebase) and Rule 7 (pick the more recent pattern), the scoop
  logic was placed in `external_trending.py`. `trending.py` was left untouched.
- **`html_render.py` WAS touched — CSS only, no new primitive.** I added ~55 lines of CSS for the
  three new sections (`.scoops-strip`, `.overlap-block`, `.trending-rail` + children). No new
  render primitive was added; the section-builders in `render.py` reuse the existing
  `render_link` / `escape` / `_format_timestamp` primitives. This stayed within the brief's
  "prefer section-builders in render.py" guidance — html_render.py changed for styling only.

## Self-review findings + fixes

- **(High, fixed) Concurrent codebase drift.** Mid-task, `trending.py` was reverted to its
  Sub-phase-2-only state and `external_trending.py` appeared with my scoop code merged in. Fixed:
  removed a stale `web_search_keyless` import I had restored into `trending.py` (no longer needed
  there), repointed the test imports to `lib.external_trending`.
- **(Low, noted) `render.py` reaches `html_render._format_timestamp`.** A private helper is reused
  for the cross-link timestamp label. Same module family, no logic duplicated; kept rather than
  promoting it to public or adding a new primitive (Rule 3 surgical). Note for a future cleanup.
- **(Low, by design) Overlap block skips singletons.** `_render_overlap_block` only renders
  clusters with `>= 2` short members OR a cross-link, so a lone item (already a card) is not
  echoed — this is the genuine "everyone's talking about" set, not every item again.

## Validation

- `uv run --with pytest pytest tests/ -q` -> **115 passed** (110 pre-existing + 5 new). No regression.
- `uv run --with ruff ruff check` on all touched files -> **All checks passed.**
- DoD #3 additionally verified by rendering a real fixture and asserting all three section
  containers + the three deep-link hrefs (`vidEP&amp;t=90s`, `twReact&amp;t=0s`, scoop link)
  are present in the HTML string.

## Definition of done — PASS/FAIL per item

1. **Dormancy AND acceleration (not raw engagement)** — PASS. Test
   `test_dormant_account_spike_is_scoop_but_frequent_poster_spike_is_not`: dormant+spike flagged;
   frequent-poster spike NOT flagged; dormant-but-normal NOT flagged.
2. **Scoop multiplier raises score above identical non-scoop** — PASS. Test
   `test_scoop_multiplier_raises_score_above_identical_non_scoop`: two byte-identical items, only
   the scoop is in the map; scoop scores strictly higher and ranks first via `derank_items`.
3. **Three M3 sections render with deep-links** — PASS. Test
   `test_three_m3_sections_render_with_deep_links` + manual HTML inspection.
4. **M1/M2 regression (no M3 data -> unchanged)** — PASS. Test
   `test_no_m3_data_renders_m1_page_without_new_sections` + `test_trending_multiplier_map_is_empty_no_op_when_no_trending`
   + the 110 pre-existing tests stay green.
5. **No LLM / no network** — PASS. All new code is stdlib + lib math/string building; the only
   external seam (`SearchFn`) belongs to Sub-phase 3 and is unused by scoop detection/render.

## Final rendered-digest shape

A digest now renders, top to bottom on page 1:
`TL;DR header -> SCOOPS STRIP (loud, ⚡, "your network first") -> OVERLAP BLOCK ("Everyone's talking
about", with cross-link chapter deep-links into episodes) -> main cards (Hero/Standard + chapter
lists, then Compact) -> RIGHT-RAIL TRENDING ("Trending in your network", tagged
corroborated/scoop) -> "They also posted" index strip`. Page 2 (spill) carries only the low-tier
overflow (Compact + Index) — the M3 sections stay on page 1. With no clusters/trending/scoops
supplied, all three M3 sections are omitted and the page is the unchanged M1 page.

## Phase-6 config knobs (`/orbit --setup` wizard should expose)

- **Scoop dormancy:** `external_trending.SCOOP_DORMANCY_MAX_HISTORY` (default 5) — max prior-post
  count for "normally dormant".
- **Scoop acceleration:** `external_trending.SCOOP_ACCELERATION_MIN_RATIO` (default 2.0) — min
  baseline-relative spike for "accelerating".
- **Rerank boosts:** `external_trending.SCOOP_RERANK_MULTIPLIER` (1.5),
  `external_trending.TRENDING_RERANK_MULTIPLIER` (1.2) — how hard a scoop/trending item is lifted.
- **Corroboration (Sub-phase 3):** `CORROBORATION_RESULT_THRESHOLD` (3),
  `DEPTH_CROSS_SEARCH_BUDGET` (quick 3 / default 8 / deep 15) — external cross-search cost.
- **Internal trending (Sub-phase 2):** `trending.SPIKE_WEIGHT` (1.0),
  `trending.CONVERGENCE_PER_CREATOR` (0.5).
- **Page budget (render):** `render.PAGE_1_BUDGET_PX` (1400) and the `TIER_HEIGHT_PX` /
  `CHAPTER_HEIGHT_PX` depth-estimate table.
