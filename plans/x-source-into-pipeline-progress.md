# Progress: phase-4-x-source-into-pipeline

**Phase file:** plans/phase-4-x-source-into-pipeline.md
**Started:** 2026-06-18
**Base HEAD:** d8d8b0d (Phase 3)
**Mode:** SEQUENTIAL (3 ⚠ irreversible sub-phases + heavy file overlap on bird_x.py / vendor dir / orbit.py)
**Status:** COMPLETE — all 4 sub-phases SUCCESS; phase DoD/slop/CSO PASS; 83 tests green.

## Sub-phase progress
- [x] 1: Extend vendored bird client with the `Following` op (⚠ irreversible — vendor lift + additive mixin) — COMPLETED (2 Node tests pass; vendor lifted verbatim; base.js additive only; no credential logging). `--following <id>` takes a NUMERIC userId; Sub-phase 2 owns screen_name→rest_id. `--following --json` emits `[{creator_handle, display_name, rest_id}, ...]` or `{error, items:[]}`.
- [x] 2: Python wrapper for Following — X source loading (Stage 0) — COMPLETED (3 tests pass; full suite 75). `Follow(creator_handle, display_name, rest_id)`. X sources via `store.list_sources(platform="x")` (only handle persisted to `sources`, not rest_id — `from:handle` keys on handle). Numeric self-id from `X_USER_ID` env (loud `XAuthError` if unset). `DEPTH_CONFIG={quick:12,default:30,deep:60}`. No-credential-logging test proves the security invariant.
- [x] 3: Per-handle SearchTimeline delta with pacing + rotation (Stage 1) — COMPLETED (3 tests pass; full suite 78; rotation invariant is genuine, not tautological). `Tweet(text, tweet_id, handle, created_at, like_count, retweet_count, reply_count, quote_count)`. Card URL = `https://x.com/{handle}/status/{tweet_id}`. Stage-1 call: `fetch_new_tweets(store.list_sources(platform="x"), depth, run_day_ordinal, sleeper=time.sleep)`. Budget=#handles, stable order by source_id asc, max_workers=3, delay 1.5s. NOTE: bird_x.py now 755 LOC total (587 code-ish, heavy docstrings) — over the 500 agent-file target; flag for phase slop scan.
- [x] 4: Feed X items into the shared classify + pipeline path — COMPLETED (5 new tests, full suite 83, 0 regressions). Unified digest proven: HTML contains both `watch?v=` and `x.com/.../status/...` cards. AUTHORIZED divergence: also touched rerank.py (additive `card_url` field + X adapter) and render.py (additive source-aware `_card_deep_link`) — required for the M2 DoD; YouTube path byte-for-byte unchanged.

## Notes
- Vendor dir does NOT yet exist in orbit tree — Sub-phase 1 lifts `vendor/bird-search/*` verbatim from the reference clone FIRST, then extends.
- `bird_x.py` does NOT yet exist — Sub-phase 2 creates it (borrowing cookie/subproc/depth patterns from the reference `bird_x.py`, NOT the topic-search machinery).
- Reference clone: /Users/asheshsrivastava/last30days-skill/skills/last30days/scripts/
- Test runner: `uv run --with pytest pytest tests/`
