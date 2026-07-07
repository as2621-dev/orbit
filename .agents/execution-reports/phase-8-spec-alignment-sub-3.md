# Phase 8 — Sub-phase 3: X virality selection — Execution Report

**Status: SUCCESS**

## Quote-field finding (Rule 8, first step)
The vendored bird CLI **does carry quote info**. In
`scripts/lib/vendor/bird-search/lib/twitter-client-utils.js`, `mapTweetResult` (line 362)
builds each tweet object with a **`quotedTweet`** key (line 401), populated only when the
source tweet has a `quoted_status_result.result` and the client's `quoteDepth > 0`. The
CLI (`bird-search.mjs`) constructs `SearchClient` without a `quoteDepth` option, so
`normalizeQuoteDepth(undefined)` returns its default of **1** — meaning a quote tweet DOES
emit a nested `quotedTweet` object, and `JSON.stringify` omits the key entirely for a
non-quote tweet. So presence of a truthy `quotedTweet` is the quote signal. `_detect_is_quote`
reads it tolerantly alongside the raw-API variants (`quoted_status`, `quoted_status_result`,
`quoted_status_id`, `quoted_status_id_str`, `isQuote`) and defaults False. **No dead code —
the flag is real and payload-backed.**

## What I implemented
- **Quote detection (`scripts/lib/bird_x.py`):** `Tweet` gained `is_quote: bool = False`;
  new pure `_detect_is_quote(entry)` helper; `_parse_tweets` sets `is_quote` per tweet.
- **Blend + multiplier + absolute term (`scripts/lib/rerank.py`):**
  - `RankableItem` gained `quote_count: Optional[int] = None` + `is_quote: bool = False`
    (appended AFTER `summary` so all existing positional constructions/doctests stay valid).
  - `from_tweet` now maps both (`from_parts`/YouTube leaves defaults → score-neutral).
  - `engagement_blend` adds `ENGAGEMENT_QUOTE_WEIGHT = 0.20` (below likes' 0.35);
    `log1p_safe(None)=0` keeps YouTube byte-for-byte unchanged.
  - New `QUOTE_TWEET_MULTIPLIER = 0.5` applied to the FINAL score when `is_quote`.
  - New pure `compute_batch_engagement_percentile(items)` — cumulative `<=`/N rank so top=1.0,
    single-item=1.0, empty={}, monotone; new `ABSOLUTE_ENGAGEMENT_WEIGHT = 0.5` term added
    inside the intrinsic bracket. `score_item` gained an `engagement_percentiles` kwarg;
    `derank_items` computes the map over X items only (via new `is_x_item` helper keyed on the
    `https://x.com/` card_url) and threads it through.
  - New pure `cap_x_items(scored_items, *, cap=X_DIGEST_TWEET_CAP)` → `(kept, dropped_count)`;
    `X_DIGEST_TWEET_CAP = 8`. Keeps all YouTube + top-N X, order preserved, logs
    `x_digest_cap_applied`.
- **Rank seam (`scripts/orbit.py`):** `run_stage6_rank_and_tier` now calls `cap_x_items`
  between `derank_items` and `assign_density_tiers`, and logs `x_cap_dropped_count` on
  `rank_and_tier_completed`. The cap sits OUTSIDE `assign_density_tiers`, so tiering's
  `len(out)==len(items)` invariant holds for what it receives. Import line updated. **No edits
  to `run_stage1_build_x_items` — sub-phase 2's Axis-A + category gates are untouched.**

## Files modified
- `scripts/lib/bird_x.py`
- `scripts/lib/rerank.py`
- `scripts/orbit.py` (import + `run_stage6_rank_and_tier` only)
- `tests/test_bird_x_delta.py`
- `tests/test_rerank.py`

## Divergences / decisions
- Cap log field named `x_cap_dropped_count` (not the spec's literal `x_stage1_cap_dropped`):
  the cap runs at stage 6, and the repo convention is `<thing>_dropped_count`
  (`dropped_noise_count`, `category_dropped_count`, `short_form_dropped_count`). The spec
  explicitly permitted "an equivalent count field, matching the repo's existing conventions."
- `is_x_item` discriminates on `card_url.startswith("https://x.com/")` — the one field that
  already reliably separates the two sources (from_tweet sets it, from_parts leaves it empty),
  avoiding a new platform tag threaded through every producer (Rule 3).
- Trimmed `orbit.py`'s `run_stage6_rank_and_tier` docstring to land the file at exactly 1077
  lines (the hard ceiling) after adding the cap wiring.

## Self-review findings + fixes
- Reviewed percentile edge cases (ties, single, empty — all covered by test + the `<=`/N
  definition), cap off-by-one (kept==cap, dropped==surplus — tested at 9/8 and 8/8), and
  YouTube score-neutrality (empty/absent percentile → 0.0 term; `quote_count None`→0 blend;
  `is_quote False`→no multiplier — all existing YouTube rerank/density/pipeline tests stayed
  green unchanged, which is the regression proof).
- Fixed one self-introduced issue: a >120-char line in a new test — reformatted so my lines
  pass `ruff format --check`.

## Validation
- `.venv/bin/python -m pytest tests/ -q` → **242 passed** (was ≥234 before; +8 new: 1 in
  test_bird_x_delta, 7 in test_rerank).
- `ruff check` on all 5 touched files → **All checks passed!**
- `ruff format --check` — my lines clean in rerank.py / test_rerank.py / test_bird_x_delta.py.
  `bird_x.py` still shows PRE-EXISTING drift (XAuthError message wrapping, `_parse_follows`,
  the old `quote_count` block) — none of it my lines (verified via `--diff`); left untouched
  per the "pre-existing repo drift" note.
- Doctests (`python -m doctest`) on rerank.py + bird_x.py → pass (new `cap_x_items` example
  marked `+SKIP` for its log line, matching the `derank_items` pattern).
- Line counts: **orbit.py 1077** (≤ 1077 ✓), rerank.py 871, bird_x.py 796 (both < 1000 ✓).

## Definition of done — PASS (each clause has a test that fails on wrong logic)
- quoted-tweet marker → `is_quote=True`, plain → False — `test_parse_tweets_flags_quote_tweets_from_quoted_tweet_marker`
- `from_tweet` carries `quote_count`/`is_quote` — `test_from_tweet_carries_quote_count_and_is_quote`
- blend includes quotes (with > without) — `test_engagement_blend_rewards_quotes`
- `QUOTE_TWEET_MULTIPLIER` ranks quote below identical original — `test_quote_multiplier_ranks_quote_below_identical_original`
- percentile 1.0 top / monotone / single-item (and empty) — `test_batch_percentile_top_is_one_monotone_and_single_item`
- 9 scored tweets → exactly 8 survive, drop logged — `test_x_cap_keeps_top_eight_and_drops_the_rest`
- batch ≤ 8 untouched — `test_x_cap_untouched_when_at_or_below_cap`
- YouTube count unaffected — `test_x_cap_never_drops_youtube_items`
- Full `pytest tests/` green — 242 passed

## Concerns
- **Quote multiplier in a negative-score regime:** `QUOTE_TWEET_MULTIPLIER` (like the existing
  `priority_weight` and `trending_multiplier`) multiplies the FINAL score. If an item's
  intrinsic bracket goes negative (a tweet far below its creator's own engagement baseline),
  ×0.5 makes the score LESS negative, which would rank a quote ABOVE the identical original.
  This is a pre-existing property of the whole multiplicative model (priority_weight has the
  same edge), not something this change introduces uniquely, and the spec explicitly said
  "applied to the final score." In practice quotes rarely sit below-baseline with the
  uniqueness floor (1.0) + recency (0,1] + absolute (≥0) terms in the bracket. Flagging it in
  case a later phase wants the multiplier moved inside the bracket.
- `ABSOLUTE_ENGAGEMENT_WEIGHT`, `ENGAGEMENT_QUOTE_WEIGHT`, `QUOTE_TWEET_MULTIPLIER` are
  first-cut constants (module-top, documented) — tune against real runs.
