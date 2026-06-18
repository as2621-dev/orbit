# Phase 3: Derank into density tiers + HTML render with deep-links (YouTube)

**Milestone:** M1 — YouTube half, end to end
**Status:** Not started
**Estimated effort:** L

## Goal
Classified, chapterized YouTube items are scored by Orbit's weighted derank formula, sorted into Hero → Standard → Compact → Index density tiers (rank controls density, never inclusion), and rendered into a self-contained HTML one-pager that spills Compact+Index to a linked page 2 when a height budget is crossed — every video card and chapter carrying a working `watch?v=ID&t=Ns` deep-link. A YouTube-only Orbit digest is openable and usable standalone (M1 ships).

## Resolved open questions folded into this phase
- **Page-budget height heuristic (master-plan Q2).** The reference render (`render.py`, `html_render.py`) has ZERO pagination/height logic — it emits one HTML file, no upper bound. So Orbit BUILDS this from scratch. Resolved heuristic: a **per-tier estimated-height table** (each tier maps to an estimated px cost: Hero ≈ large card, Standard ≈ medium, Compact ≈ row, Index ≈ line; chapter lists add per-chapter px). `estimate_page_height(items)` sums estimated px; when the running total crosses `PAGE_1_BUDGET_PX` (a tunable constant, default sized to a ~A4/screen page), Compact+Index tiers spill to page 2. Hard cap: 2 pages. Char/element counts → estimated px (no headless-browser measurement pass, to stay dependency-free per stdlib-first rule).
- **Rank controls density, never inclusion (design decision 6 / api-contracts derank contract).** The weighted score only assigns a tier; nothing is dropped by score. "Failed-classification" items still appear in the bottom "they also posted" Index strip.
- **No embeddings needed for M1.** Clustering/overlap is M3; M1 ranks per-item. The lexical-similarity machinery (master-plan Q1: reference uses char-trigram + token Jaccard, no embedding model) is deferred to M3 untouched here.

## Sub-phases

### Sub-phase 1: Weighted derank scoring (Stage 6a)
- **Files touched:** `skills/orbit/scripts/lib/rerank.py`
- **What ships:** `score_item(item, config) -> float` implementing the api-contracts derank formula: creator `priority_weight` (from `creator_weights`/`sources`), source diversity (cluster size = 1 in M1), uniqueness boost (a lone sharp take from a high-priority creator doesn't sink), engagement **relative to the creator's own baseline** (not raw views — uses the lifted `log1p_safe`/per-source normalization shape from `signals.py`, with the creator's own recent median as the baseline), recency decay, and a trending/scoop multiplier (=1.0 in M1, wired for M3). All deterministic math (Rule 5 — no LLM). `derank_items(items, config) -> list[ScoredItem]` sorts descending by score.
- **Definition of done:** A test asserts that two items with identical raw engagement but different `priority_weight` sort with the higher-weight creator first (encodes "priority to the creator" thumb-on-scale, not just "returns a sorted list"). A test asserts an item with high engagement relative to its creator's low baseline outranks an item with higher raw engagement but normal-for-that-creator levels (baseline-relative intent, brief Stage 6). A test asserts a high-priority creator's unique item is NOT bottom-ranked despite low engagement (uniqueness boost).
- **Dependencies:** none (consumes Phase 2 classified items)

### Sub-phase 2: Assign density tiers (Stage 6b)
- **Files touched:** `skills/orbit/scripts/lib/density.py`
- **What ships:** `assign_density_tiers(scored_items) -> list[TieredItem]` mapping the sorted score distribution to `hero | standard | compact | index` — by score-rank position/thresholds, NOT by a hard top-N cutoff (every item gets a tier; nothing excluded). Items that failed classification (Axis A or B) are forced into the `index` "they also posted" tier regardless of score. Exposes the tier boundaries as tunable constants. Resolves the carryforward rule: top-tier (`hero`/`standard`) items the user didn't open are eligible for one resurface via `store.carryforward` (`surfaced_count` capped at 1).
- **Definition of done:** A test asserts that given N scored items, ALL N receive a tier (none dropped — `len(tiered) == len(scored)`, encoding "rank controls density never inclusion"). A test asserts a classification-failed item lands in `index` even with a high score. A test asserts a top-tier item marked unopened is recorded in `carryforward` with `surfaced_count` not exceeding 1 on repeated runs (resurface-once intent).
- **Dependencies:** Sub-phase 1

### Sub-phase 3: Render the HTML one-pager (Stage 7a)
- **Files touched:** `skills/orbit/scripts/lib/render.py`, `skills/orbit/scripts/lib/html_render.py`, `skills/orbit/references/design-brief.md`
- **What ships:** `render_digest_html(tiered_items, config) -> str` producing a self-contained HTML page per the Design Brief (brief §3 Stage 7): one-line TL;DR header, creator episode cards (Hero/Standard get full cards with chapter lists; Compact get rows; Index gets the bottom "they also posted" strip), each chapter and card linking to its `watch?v=ID&t=Ns` deep-link. Lifts the reference's sentinel-replacement template approach (`HTML_TEMPLATE` + `__BODY__`/`__CSS__` swaps) and the XSS-safe link helper (`_SAFE_LINK_SCHEMES` allowlist), restyled for Orbit's tiers (the reference has no tier system — built here). `references/design-brief.md` is the self-contained look spec (no remote design-references — explicitly out of scope per master-plan).
- **Definition of done:** A test renders a fixture of items across all four tiers and asserts: the output HTML contains a `<a href="https://www.youtube.com/watch?v=...&t=90s"` for a chapterized item (deep-link survives all the way to HTML — the headline feature, end to end); Hero items render with chapter lists while Index items render in the "they also posted" section; the rendered links pass the scheme allowlist (no `javascript:` injection from a malicious title). A snapshot/structural assertion confirms the TL;DR header is present.
- **Dependencies:** Sub-phase 2

### Sub-phase 4: Page-budget spill to page 2 + pipeline wiring (Stage 7b)
- **Files touched:** `skills/orbit/scripts/lib/render.py`, `skills/orbit/scripts/orbit.py`
- **What ships:** `estimate_page_height(tiered_items) -> int` (per-tier px estimate table) and a paginating wrapper in `render_digest_html`: when estimated height crosses `PAGE_1_BUDGET_PX`, Compact+Index tiers move to a second linked HTML file (`today-page2.html`), with a "continued on page 2 →" link on page 1; hard-capped at 2 pages. `orbit.py` wires the full YouTube pipeline Stage 6→7: score → tier → render → write HTML to `config.delivery.html_path` (and page 2 beside it). Logs `render_completed` with page count.
- **Definition of done:** A test with a small item set asserts a single page (no page-2 file, no spill link). A test with a large item set (estimated height > budget) asserts page 2 is emitted, page 1 contains the "page 2" link, and Hero/Standard stayed on page 1 while Compact+Index moved to page 2 (spill-the-low-tiers intent, not arbitrary splitting). A test asserts the 2-page hard cap holds even with an oversized set. `orbit.py` end-to-end over a mocked pipeline writes a non-empty HTML file to a temp `html_path`.
- **Dependencies:** Sub-phase 3

## Phase-level definition of done
`pytest tests/` passes. Running `orbit.py` end-to-end over mocked Phase 1-2 outputs (sources → delta → transcript-with-cues → classify → chapterize) produces a self-contained HTML digest at the configured `html_path` where: every item appears in some tier (nothing dropped), tiers map to visual density, a >20-min chapterized item shows a chapter list with working `watch?v=ID&t=Ns` deep-links, and an oversized digest spills Compact+Index to a linked page 2 capped at 2 pages. M1's YouTube-only Orbit is openable and standalone-usable — satisfying the riskiest-assumption test surface (the maintainer can run it on real subs for several days).

## Out of scope
- No X items in the digest (M2 feeds the same render path).
- No clustering/overlap block, right-rail trending, or scoops strip (M3 — these sections render empty/absent in M1).
- No iMessage/WhatsApp delivery or `--setup` wizard (M4) — M1 writes the HTML file only.
- No headless-browser height measurement — estimate-by-content only.

## Open questions
- `PAGE_1_BUDGET_PX` and the per-tier px estimates are first-cut constants; the maintainer's real-day usage tunes whether the one-pager actually fits and whether the right items land in Hero (this IS the riskiest-assumption test). Not blocking — tunable constants.
- The Design Brief (`references/design-brief.md`) visual spec is authored here from brief §3 Stage 7; iteration expected. Not blocking.

## Self-critique

**Product lens:** PASS — and this phase is where M1's riskiest assumption becomes measurable. By phase end the maintainer has a real daily HTML digest; running it exposes whether Hero/Standard/Compact/Index laddering and the derank weights float the right items and whether deep-links get used (master-plan riskiest-assumption test). Every Stage-6/7 MVP capability (weighted score, density tiers, deep-link HTML, page-2 spill, never-drop) maps to a sub-phase DoD. No out-of-brief features; M3/M4 sections explicitly deferred and render absent.
**Engineering lens:** PASS. Within stack (Python templating, stdlib-only; no framework). DoDs are concrete and fresh-context checkable (HTML contains a specific `&t=90s` href; `len(tiered)==len(scored)`; page-2 file present/absent). Scoring is deterministic math (Rule 5), no LLM in this phase at all. Sub-phase 4 wires the pipeline last and does not cement a flexible choice prematurely — it consumes the tier/render shapes settled in 1-3. The trending/scoop multiplier is wired as a `1.0` no-op so M3 extends rather than rewrites `rerank.py`.
**Risk lens:** Findings + fixes. (1) **File-boundary conflict:** Sub-phases 3 and 4 BOTH edit `render.py`. Resolved by explicit dependency (4 depends on 3, sequential) — flagged so `/run-phase` does not parallelize them. Sub-phase 4 also edits `orbit.py` (owned since Phase 1) — additive Stage 6→7 wiring, no conflict. (2) Test coverage per Rule 9: each DoD fails on wrong logic (priority-weight ordering, baseline-relative ranking, nothing-dropped, deep-link-in-HTML, correct-tier spill), not just "renders". (3) XSS: the link-scheme allowlist test guards against a malicious creator title injecting `javascript:` — a real safety check, not cosmetic. (4) Painting-into-a-corner: 1(score)→2(tier)→3(render)→4(paginate+wire); page-2 spill in 4 needs the tiers from 2 and the render from 3, both present. Order holds.
**Irreversible sub-phases:** None. (Writes HTML files to a user-configured path and `carryforward` rows on the existing schema; re-runnable, overwrites its own output by design.)
