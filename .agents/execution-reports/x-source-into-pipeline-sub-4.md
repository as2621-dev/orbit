# Phase 4 — Sub-phase 4: Feed X items into the shared classify + pipeline path (M2 unified digest)

## What was implemented

The M2 deliverable: ONE unified digest with BOTH YouTube video cards and X tweet cards, produced by REUSING the M1 classify/rank/render path (no fork).

### 1. `classify.py` — generalized the shared classify path (no X-specific classifier)
- Added `_read_first_item_field(item, field_names)` — reads the FIRST non-empty value among candidate field names off an Upload (attr) / Tweet (attr) / dict (key).
- `classify_item` now resolves `item_external_id` from `("video_id", "tweet_id")` — YouTube `video_id` OR X `tweet_id`.
- `_render_prompt` now reads the prompt body from `("title", "text")` and `("description", "text")` — a text-only Tweet maps its `text` into the prompt's title/description slots; the SAME `references/classify.md` prompt renders for both.
- `classify_item`'s signature, return type (`Classification`), override short-circuit, prior-seeding, and persistence via `store.set_classification` are all unchanged. An X tweet persists to the SAME `store.classifications` table keyed by `tweet_id`.

### 2. `rerank.py` (AUTHORIZED additive expansion) — X adapter + optional card URL
- Added optional dataclass field `card_url: str = ""` to `RankableItem` (default empty → YouTube behavior unchanged).
- Added classmethod `RankableItem.from_tweet(tweet, classification, *, creator_external_id="")` mirroring `from_parts`: maps `tweet.text`→`title`, `handle`→`channel_name`+`creator_external_id`, engagement (`retweet_count`→`view_count`, `like_count`→`like_count`, `reply_count`→`comment_count`), and sets `card_url=https://x.com/{handle}/status/{tweet_id}`.
- Added helper `_tweet_upload_date(created_at)` — reduces an ISO-8601 `created_at` to `YYYYMMDD` for recency scoring; returns `""` (→ neutral recency, never crashes) for unparseable/Twitter-format dates.
- `from_parts` and every existing YouTube field read are untouched.

### 3. `render.py` (AUTHORIZED additive expansion) — source-aware card link
- `_card_deep_link(item)` now prefers a non-empty `item.card_url` (the X permalink) and ONLY falls back to the hardcoded `watch?v={id}&t=0s` when `card_url` is empty (every YouTube item). One-line preference + fallback; no restructure. The x.com `https` URL passes the existing `is_safe_link_url` allowlist.

### 4. `orbit.py` (wiring only, Rule 5) — Stage 0 both sources + Stage 1 X merge
- `run_stage0_load_sources` gained injectable `x_loader`/`x_persist` params and now ALSO loads the X following (via new helper `_load_x_sources`) on every Stage-0 path (cache-hit, network-guard, and refresh). X loading is BEST-EFFORT: an `XAuthError` is logged with a `fix_suggestion` and SWALLOWED so a YouTube-only user still gets a digest (X is additive; YouTube auth failure stays fatal/re-raised).
- Added `run_stage1_build_x_items(config, depth, *, run_day_ordinal, x_delta, llm_classifier)` — loads X sources, runs the injectable delta (`bird_x.fetch_new_tweets`), classifies each tweet via the SHARED `classify.classify_item` (channel prior = source row `category` keyed by handle), and builds `RankableItem.from_tweet`. Returns `[]` when no X sources.
- Added `_current_day_ordinal()` — days since the Unix epoch (UTC), so X handle rotation advances across daily runs.
- `run_pipeline` now merges `youtube_items + x_items` into one stream before `run_stage6_rank_and_tier` → `run_stage7_render`. In a bare CLI run both halves are empty (YouTube delta/classify still upstream-stubbed; X classify needs a runtime LLM the build env lacks — the default fails loud, Rule 12). The full merge path is exercised end-to-end by the integration test with mocked boundaries.

## Files modified / created

- MOD `skills/orbit/scripts/lib/classify.py` (generalized item-field reading)
- MOD `skills/orbit/scripts/lib/rerank.py` — **AUTHORIZED divergence** (added `card_url` field + `from_tweet` + `_tweet_upload_date`)
- MOD `skills/orbit/scripts/lib/render.py` — **AUTHORIZED divergence** (source-aware `_card_deep_link`)
- MOD `skills/orbit/scripts/orbit.py` (Stage 0 X load + Stage 1 X build + merge wiring)
- NEW `tests/test_classify_x.py`
- NEW `tests/test_orbit_unified_digest.py`

## Divergences from the plan + why

The plan's stated Sub-phase-4 file boundary was `classify.py` + `orbit.py`. The prompt PRE-AUTHORIZED the cross-file expansion to `rerank.py` + `render.py` because the unified-digest DoD cannot hold otherwise: `render._card_deep_link` HARDCODED a `youtube.com/watch?v={id}` URL for EVERY card, so an X tweet rendered through the unchanged path would ship a BROKEN youtube link (the tweet id is not a video id). The minimal additive fix — an optional `card_url` carried on `RankableItem` and preferred by render — is the smallest change that lets a real x.com card link survive to the written HTML while leaving the YouTube path byte-for-byte unchanged (empty `card_url` → identical fallback). Both expansions are additive (new defaulted field, new classmethod, one-line render preference); no existing behavior was altered.

## Code-review findings + fixes

- **[confirmed — YouTube path byte-for-byte unchanged]** `_read_first_item_field` tries `video_id`/`title`/`description` FIRST for every YouTube item, so the resolved values are identical to the old `_read_item_field` calls. `RankableItem.card_url` defaults `""` → `_card_deep_link` falls through to the exact prior youtube URL. Proven: all 42 existing classify/render/rerank/pipeline/density tests stay green, including `test_orbit_pipeline.py`'s exact-deep-link assertion (`watch?v=vidE2E&amp;t=90s`).
- **[low, accepted]** A Tweet's `created_at` in Twitter's `"Wed Jan 15 ..."` form is not parsed to a date → empty `upload_date` → neutral recency (`RECENCY_NEUTRAL_DECAY`). Acceptable: never buries an X item, never crashes; ISO dates (the Sub-phase-3 example shape) ARE parsed. Documented in `_tweet_upload_date`.
- **[low, accepted]** Engagement maps `retweet_count`→`view_count` (tweets have no views). The blend is RELATIVE to the creator's own batch baseline, so the absolute scale difference between YT views and X retweets does not unfairly cross-rank sources.
- **[info, security]** X classify wiring passes only the tweet text/handle/counts to the LLM boundary and the source `category` as the prior — no cookies/tokens. The X loader's credential handling is owned by `bird_x` (Sub-phases 1-3), untouched here.
- No critical/high findings.

## Validation results (exact commands)

- `python3 -c "import ast; ast.parse(...)"` on orbit.py/classify.py/rerank.py/render.py → `ast ok`
- `cd skills/orbit/scripts && python3 -c "import orbit; from lib import classify, rerank, render, bird_x"` → `import ok`
- `uv run --with pytest pytest tests/test_classify_x.py tests/test_orbit_unified_digest.py -q` → **5 passed**
- `uv run --with pytest pytest tests/test_classify.py tests/test_render.py tests/test_rerank.py tests/test_orbit_pipeline.py tests/test_density.py -q` → **42 passed** (YouTube path no-regression)
- `uv run --with pytest pytest tests/ -q` (FULL suite) → **83 passed** in 1.74s (was 78 before this sub-phase; **+5 new, 0 regressions**)

All boundaries mocked: LLM injected per call, YouTube loader + X following loader + X delta all injected mocks, store on a temp DB. NO live X/YouTube/LLM calls, NO real cookies.

New test names:
- `test_classify_x.py`: `test_x_tweet_classifies_on_shared_path_and_persists`, `test_x_tweet_prompt_body_reads_tweet_text`, `test_x_tweet_user_override_respected_on_shared_path`
- `test_orbit_unified_digest.py`: `test_unified_digest_contains_both_youtube_and_x_cards`, `test_stage0_x_auth_failure_does_not_abort_youtube_only_run`

## Definition of done — PASS (both required tests)

1. **Shared classify path — PASS.** `test_x_tweet_classifies_on_shared_path_and_persists` feeds a real `Tweet` (text/tweet_id, NO video_id/title/description) through `classify.classify_item` with a MOCKED LLM and asserts a valid two-axis `Classification` PERSISTED to `store.classifications` keyed by `tweet_id`:
   > `assert persisted is not None, "an X tweet must persist to store.classifications (shared table)"`
   > `assert persisted["item_external_id"] == tweet.tweet_id`
   A companion test (`test_x_tweet_prompt_body_reads_tweet_text`) proves the tweet `text` reaches the SAME prompt body (not an empty item). Proves no X-specific classify path/table.

2. **Unified digest (M2 deliverable) — PASS.** `test_unified_digest_contains_both_youtube_and_x_cards` mocks the YouTube loader, X following loader, X delta, and LLM, drives Stage 0 → Stage 1 X build → merge → real `run_stage6_rank_and_tier` → `run_stage7_render` to a temp path, and asserts the WRITTEN HTML contains BOTH cards:
   > `assert "https://www.youtube.com/watch?v=ytVIDEO01" in written_html, "YouTube card must carry a watch?v= link"`
   > `assert "https://x.com/alice/status/1900000000000000042" in written_html, "X card must carry an x.com status link"`
   > `assert "watch?v=1900000000000000042" not in written_html, "X tweet must NOT render a youtube watch link"`
   The third assertion is the regression guard against the historical hardcoded-youtube-link render gap — the test FAILS if X items render with a broken/youtube URL or never reach render (`assert len(tiered) == 2`).

## SECURITY — confirmed (yes)

- No cookies/tokens touched here (bird_x owns credential handling, untouched). The new X Stage-1 wiring passes only tweet text/handle/counts + the source `category` to the LLM boundary; no credential is logged or interpolated.
- `_load_x_sources`'s swallowed-`XAuthError` log emits the exception message (a credential-free, constant/README-pointing string from `bird_x`) — never a token.
- No secrets/tokens in fixtures: the integration test uses dummy handles/ids only; the X delta + loaders are fully mocked (no real subprocess, no real cookies).

## Concerns + Phase 5 (M3 overlap/trending/scoop) handoff

**Unified item-stream shape (what M3 consumes):** every item — YouTube AND X — is a `lib.rerank.RankableItem` with fields: `item_external_id`, `title`, `channel_name`, `creator_external_id`, `view_count`, `like_count`, `comment_count`, `upload_date`, `classification`, `chapters`, and the NEW `card_url: str = ""` (empty → YouTube; `https://x.com/{handle}/status/{id}` → X). X items are built via `RankableItem.from_tweet`; YouTube via `RankableItem.from_parts`. There is no platform discriminator field beyond `card_url` being non-empty for X — if M3 needs an explicit source tag for cross-source clustering, add a `platform`/`source` field to `RankableItem` (additive, same pattern as `card_url`).

**Empty signal hooks M3 must fill (named precisely, all in `rerank.py`):**
- `CLUSTER_SIZE_NEUTRAL: float = 1.0` (rerank.py) — the source-diversity / cluster-size multiplier, currently a neutral no-op in `score_item`'s `score = priority_weight * CLUSTER_SIZE_NEUTRAL * TRENDING_MULTIPLIER_NEUTRAL * intrinsic`. M3 overlap/clustering replaces this with a real per-item cluster-size factor.
- `TRENDING_MULTIPLIER_NEUTRAL: float = 1.0` (rerank.py) — the trending/scoop multiplier in the same `score_item` product. M3 internal/external trending + scoop detection fills this.
- `compute_creator_engagement_baselines` (rerank.py) — currently a BATCH-median uniqueness/cluster-size baseline (documented to be replaceable by a true historical median without touching `score_item`). This is the uniqueness/cluster-size hook M3 swaps for a real cross-source baseline.

Both X and YouTube items flow through the SAME `score_item`, so M3 wiring those three hooks lights up trending/clustering for BOTH sources at once — no per-source fork.

## Return to orchestrator

1. **STATUS: SUCCESS**
2. Files touched: `skills/orbit/scripts/lib/classify.py`, `skills/orbit/scripts/lib/rerank.py`, `skills/orbit/scripts/lib/render.py`, `skills/orbit/scripts/orbit.py`, `tests/test_classify_x.py`, `tests/test_orbit_unified_digest.py`
3. Validation: **PASS** — full suite `uv run --with pytest pytest tests/ -q` → **83 passed** (was 78; +5 new, 0 regressions). New tests: `test_x_tweet_classifies_on_shared_path_and_persists`, `test_x_tweet_prompt_body_reads_tweet_text`, `test_x_tweet_user_override_respected_on_shared_path`, `test_unified_digest_contains_both_youtube_and_x_cards`, `test_stage0_x_auth_failure_does_not_abort_youtube_only_run`
4. Definition of done: **PASS** (both required tests — shared classify path + unified digest with BOTH cards)
5. SECURITY: **confirmed (yes)**
6. Concerns + Phase 5 handoff: unified stream = `RankableItem` (incl. new `card_url`; add `platform` if M3 needs a source tag); empty signal hooks `rerank.CLUSTER_SIZE_NEUTRAL`, `rerank.TRENDING_MULTIPLIER_NEUTRAL`, and the `compute_creator_engagement_baselines` uniqueness/cluster-size baseline — all in the single shared `score_item` product (one wire-up lights both sources)
