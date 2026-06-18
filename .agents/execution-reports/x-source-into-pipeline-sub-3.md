# Phase 4 — Sub-phase 3: Per-handle SearchTimeline delta with pacing + rotation (Stage 1)

## What was implemented
Extended `skills/orbit/scripts/lib/bird_x.py` (additive — Sub-phase 2's functions/tests untouched):

- **`@dataclass Tweet`** — `text: str`, `tweet_id: str`, `handle: str`, `created_at: str`, plus optional engagement: `like_count`, `retweet_count`, `reply_count`, `quote_count` (all `Optional[int]`). Carries exactly what Sub-phase 4 needs to build the x.com card + rank.
- **`fetch_new_tweets(sources, depth, run_day_ordinal, sleeper=time.sleep) -> list[Tweet]`** — the Stage-1 entry point. Orders X sources stably by `source_id` asc, selects the rotation window, fans out `from:<handle>` SearchTimeline subprocesses (bounded pool), delta-filters against `store.get_seen_ids(source_id)`, `store.mark_seen` only after a successful fetch+parse, paces between handles via the injected sleeper. Returns the new tweets across all selected handles.
- **`select_rotation_window(handles, depth, run_day_ordinal) -> list[str]`** — deterministic round-robin: budget = `DEPTH_CONFIG[depth]` as a HANDLE-COUNT cap; `len<=budget` → all handles; else a wrap-around window of size `budget` at offset `run_day_ordinal % len(handles)`.
- **Helpers:** `_pull_handle_tweets` (one subprocess + parse; failures yield `[]` for that handle, never abort the run), `_parse_tweets` (handles camelCase + snake_case engagement variants, author fallback to queried handle, skips id-less entries), `_first_present`, `_coerce_optional_int`.
- **Cap decisions (documented in code):**
  - Budget = **number of handles** deep-pulled this run (NOT per-handle item count). `DEPTH_CONFIG[depth]` is also reused as the per-handle `--count` request inside `_pull_handle_tweets`.
  - Stable ordering key = **`source_id` ascending** (persistence order, deterministic).
  - `_MAX_CONCURRENT_HANDLE_PULLS = 3` (NOT the reference's 5 — ToS-gray posture).
  - `INTER_REQUEST_DELAY_SECONDS = 1.5` (conservative first-cut; maintainer tunes), `_SEARCH_TIMELINE_TIMEOUT_SECONDS = 30`.

## Files modified/created
- MOD `skills/orbit/scripts/lib/bird_x.py` (extended; 755 LOC total, under the 1000-line hard limit for a lib file)
- NEW `tests/test_bird_x_delta.py`
(No other files touched — Rule 3.)

## Divergences + why
- **DEPTH_CONFIG interpreted as a HANDLE-count budget** (max handles deep-pulled per run), per the prompt's explicit instruction ("the budget must cap the NUMBER OF HANDLES"). The same `DEPTH_CONFIG[depth]` value is additionally passed as the per-handle `--count` (items requested per handle) — both interpretations coexist without conflict and are documented in the docstrings. The DoD rotation test keys on which HANDLES are pulled, so the handle-count cap is the load-bearing interpretation.
- **`select_rotation_window` is a public (tested) function** rather than inline — makes the resolved Q5 invariant a directly-assertable unit (Rule 9).
- Pacing applied before each thread-pool **submission** past the first (spaces out when we hit X), not as a post-hoc sleep — matches "paces HANDLES across the Python loop".

## Code-review findings + fixes
- **[medium, resolved by design]** Cross-thread SQLite writes — avoided: workers only do subprocess+parse; ALL `get_seen_ids`/`mark_seen` happen in the single main-thread `as_completed` consumer loop, so DB access is serialized.
- **[low]** Concurrency-vs-pacing: with `max_workers=3` the sleeper still spaces submissions serially; test asserts `n-1` calls regardless of worker count. Kept.
- **[info, security]** `_pull_handle_tweets` failure logs carry only `handle` + an X/OS `error_detail` string — never a cookie; cookies flow solely via `_subprocess_env()`. Log redactor is a backstop.
- No critical/high findings.

## Validation results (exact commands)
- `python3 -c "import ast; ast.parse(...)"` → `ast ok`
- `cd skills/orbit/scripts && python3 -c "from lib import bird_x"` → `import ok`
- `uv run --with pytest pytest tests/test_bird_x_delta.py -q` → **3 passed** in 0.09s
- `uv run --with pytest pytest tests/ -q` (full suite, no-regression) → **78 passed** in 0.18s (was 75 before this sub-phase; +3 new; Sub-phase 2's `tests/test_bird_x_following.py` and all others still green)

Subprocess boundary mocked via `patch.object(bird_x.subproc, "run_with_timeout", <handle-recording stub>)`. Sleeper injected as a recorder/no-op. NO live X call, NO real cookies, NO real sleeping.

## Definition of done — PASS (all 3 required tests)
1. **Delta intent** — `test_delta_returns_only_unseen_and_marks_them`: pre-seed `seen` with `t1,t2` for alice; timeline returns `t1,t2,t3,t4`; asserts returned ids `== {"t3","t4"}` (seen ones filtered) AND `{"t1","t2","t3","t4"}.issubset(store.get_seen_ids(alice_id))` after (new ones marked). **PASS.**
2. **Rotation-fairness intent** — `test_rotation_fairness_grows_coverage_across_days`: 15 handles, `quick` budget 12 (`assert len(handles) > DEPTH_CONFIG["quick"]` precondition). A handle-recording subprocess stub captures which handles were queried on day 0 vs day 1. Key assertion (NOT a tautology — fails if the window does not rotate):
   > `assert len(coverage_after_day1) > len(coverage_after_day0)`
   > `assert day1_handles - day0_handles  # day 1 pulled at least one handle day 0 missed`
   Also asserts each day pulls exactly the budget (12). **PASS.**
3. **Pacing** — `test_inter_request_delay_is_invoked_between_handles`: 4 handles, recording sleeper; asserts `len(sleep_calls) == len(handles) - 1` (once per handle past the first) and every call == `INTER_REQUEST_DELAY_SECONDS`. **PASS.**

## SECURITY — confirmed (yes)
- Cookies read at runtime ONLY, merged into the Node subprocess env via `_subprocess_env()` (reused from Sub-phase 2); never passed as CLI args, never written to disk, never logged, never in an exception.
- New log calls (`x_fetch_new_tweets_started/completed`, `x_handle_delta_completed`, `x_handle_pull_*`) pass only `handle`, counts, `depth`, `run_day_ordinal`, and non-credential error strings — no token. `lib.log` auto-redaction is a backstop.
- Test fixtures contain NO real tokens (no credentials needed — the subprocess is fully mocked).

## Concerns + Sub-phase 4 handoff
- **Exact `Tweet` field names** (Sub-phase 4 maps Tweet → RankableItem + builds the x.com card):
  `Tweet(text: str, tweet_id: str, handle: str, created_at: str, like_count: Optional[int], retweet_count: Optional[int], reply_count: Optional[int], quote_count: Optional[int])`.
  Card URL = `https://x.com/{handle}/status/{tweet_id}`. `created_at` is the raw CLI string (e.g. `"2026-06-18T00:00:00Z"` or Twitter's `"Wed Jan 15 ..."` form) — Sub-phase 4 should normalize if ranking needs a parsed date (the reference `parse_bird_response` shows both shapes).
- **Stage-1 call shape from `orbit.py`:**
  `x_sources = store.list_sources(platform="x")` → `new_tweets = bird_x.fetch_new_tweets(x_sources, depth, run_day_ordinal)` (default `sleeper=time.sleep` in production; orbit.py needs to compute/supply `run_day_ordinal` — e.g. a day count since an epoch — so rotation advances across daily runs). The returned `list[Tweet]` is merged with YouTube uploads into the unified item stream before classify.
- **`X_USER_ID` config requirement** (from Sub-phase 2) still applies for Stage 0; Stage 1 here keys on the persisted `external_id` handle and needs no numeric id.
- **Cross-thread DB note:** `fetch_new_tweets` does all `mark_seen`/`get_seen_ids` on the main thread; if Sub-phase 4 wraps it in further concurrency, keep store writes single-threaded.

## Return to orchestrator
1. **STATUS: SUCCESS**
2. Files touched: `skills/orbit/scripts/lib/bird_x.py`, `tests/test_bird_x_delta.py`
3. Validation: **PASS** — `uv run --with pytest pytest tests/test_bird_x_delta.py -q` → 3 passed; full suite `uv run --with pytest pytest tests/ -q` → 78 passed (no regression; +3 over Sub-phase 2's 75)
4. Definition of done: **PASS** (all 3 required tests; rotation invariant is a real strictly-grows assertion)
5. SECURITY: **confirmed (yes)**
6. Concerns + handoff: `Tweet` field names + x.com card URL pattern + Stage-1 call shape (`list_sources(platform="x")` → `fetch_new_tweets(...)` with orbit.py supplying `run_day_ordinal`) above
