# Execution report — Phase 3, Sub-phase 1: Weighted derank scoring (Stage 6a)

**Status:** SUCCESS
**Date:** 2026-06-18

## What was implemented
A new `lib.rerank` module providing the unified rankable item model + the deterministic
weighted derank formula (Rule 5 — pure math, NO LLM), per the api-contracts.md "Derank
score contract (Stage 6)".

### Files created
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/rerank.py` (NEW, ~430 lines, well under 500)
- `/Users/asheshsrivastava/frommyfeed/tests/test_rerank.py` (NEW, 13 tests)

### Files modified
- None other than the two above.

## The RankableItem shape (downstream depends on this — verbatim)
`@dataclass class RankableItem` with fields, in order:
```
item_external_id: str          # video_id (YT) / tweet_id (X) — matches Classification.item_external_id
title: str
channel_name: str
creator_external_id: str       # channel_id (YT) / creator_handle (X) — KEY into config.creator_weights
view_count: Optional[int]
like_count: Optional[int]
comment_count: Optional[int]
upload_date: str               # "YYYYMMDD" (yt-dlp shape) or "" if absent
classification: Any = None     # lib.classify.Classification or None; carries is_also_posted
chapters: list = field(default_factory=list)   # list[lib.chapterize.Chapter]
```

### The from_parts adapter (the item-ingestion API — verbatim signature)
```python
@classmethod
def from_parts(
    cls,
    upload: Any,                       # lib.youtube_yt.Upload (or same-shaped obj)
    classification: Any = None,        # lib.classify.Classification or None
    chapters: Optional[list] = None,   # list[Chapter] or None -> []
    *,
    creator_external_id: str = "",     # the source row's external_id; "" -> neutral priority
) -> "RankableItem"
```
Maps the REAL Upload fields: `video_id -> item_external_id`, `title`, `channel_name`,
`view_count`/`like_count`/`comment_count`, `upload_date`. `classification` and `chapters`
pass through verbatim. **Phase 4 (X source) and Sub-phases 2/3/4 must build items via this
adapter** (or construct `RankableItem` directly for non-YouTube items) — `creator_external_id`
MUST be supplied by the caller from the source row so the priority-weight lookup works
(`from_parts` cannot derive it from the Upload, which has no channel id field).

### ScoredItem (what derank_items returns)
```python
@dataclass class ScoredItem:
    item: RankableItem
    score: float
```

## Public API surface for Sub-phase 2
- `derank_items(items: list[RankableItem], config, *, reference_date=None) -> list[ScoredItem]`
  — scores all, returns sorted DESCENDING by score (ties broken by `item_external_id`).
  Nothing dropped: `len(out) == len(in)`.
- `score_item(item, config, *, creator_baselines=None, reference_date=None) -> float`
- `compute_creator_engagement_baselines(items) -> dict[str, float]` (per-creator batch median)
- Helpers: `log1p_safe`, `engagement_blend`, `priority_weight_for`, `recency_decay`.

## The formula (named constants at module top)
```
score = priority_weight * CLUSTER_SIZE_NEUTRAL * TRENDING_MULTIPLIER_NEUTRAL
        * ( UNIQUENESS_BASELINE_BOOST
          + RELATIVE_ENGAGEMENT_WEIGHT * (engagement_blend - creator_baseline)
          + RECENCY_WEIGHT * recency_decay )
```
- **priority_weight**: `config.creator_weights[creator_external_id]`, default 1.0; multiplicative.
- **cluster size / source diversity**: `CLUSTER_SIZE_NEUTRAL = 1.0` (M3 hook).
- **trending/scoop**: `TRENDING_MULTIPLIER_NEUTRAL = 1.0` (M3 hook).
- **uniqueness boost**: `UNIQUENESS_BASELINE_BOOST = 1.0` floor INSIDE the bracket, so it is
  multiplied by priority_weight → the higher the trust, the higher the floor (DoD #3 mechanism).
- **engagement relative to creator's OWN baseline**: `engagement_blend` (log1p_safe-weighted
  view/like/comment) minus the creator's baseline. **Baseline source (M1):** the per-creator
  MEDIAN of the blend across THE CURRENT BATCH (`compute_creator_engagement_baselines`), since
  no historical per-creator engagement store is wired into rank yet. Median resists a single
  viral outlier. A lone-in-batch creator's baseline == its own blend → relative engagement 0
  (neutral). M3 can swap in a true historical median without touching `score_item` (it consumes
  whatever baseline map it's given).
- **recency decay**: half-life exponential (`RECENCY_HALF_LIFE_DAYS = 7.0`), today→1.0,
  future/clock-skew clamped to 1.0, empty/garbage `upload_date` → `RECENCY_NEUTRAL_DECAY = 0.5`.

## Divergences from the brief (and why)
- Brief mentions `priority_weight` "from `creator_weights`/`sources`". M1 reads ONLY
  `config.creator_weights` (the `sources` table mirrors it per api-contracts; config is the
  in-process surface available to rank). No divergence in behavior — same key space.
- The uniqueness boost is implemented as a priority-scaled floor INSIDE the multiplied bracket
  (rather than a separate additive term). This is the cleanest way to satisfy the brief's
  explicit requirement that "the boost ties to priority_weight so a unique high-priority item
  isn't bottom-ranked" — it falls directly out of the formula structure.

## Self code-review findings + fixes
- **[low] Doctest stdout pollution:** two docstring examples call logging functions that emit
  JSON to stdout, breaking `python -m doctest`. Fixed by marking those two examples
  `# doctest: +SKIP` (matches the codebase convention — youtube_yt/classify/chapterize all
  `+SKIP` examples that touch side-effecting boundaries). Doctests now pass clean.
- **[checked, OK] Missing engagement (None/0):** `log1p_safe` returns 0.0 for None/<=0/garbage
  → covered by test.
- **[checked, OK] Empty/garbage upload_date:** `_parse_upload_date` returns None for non-8-digit,
  non-numeric, or impossible dates (e.g. `20269999`) → neutral decay; covered by test.
- **[checked, OK] Creator absent from weights:** `priority_weight_for` defaults to 1.0; a
  non-numeric weight logs a warning and defaults. Covered by test.
- **[checked, OK] Empty item list:** `derank_items([])` returns `[]` without computing baselines.

## Validation output
- AST parse: OK. Clean import as `lib.rerank`: OK.
- `uvx ruff check`: All checks passed. `uvx ruff format`: applied (cosmetic line-joins only).
- Doctests (`python -m doctest`): pass.
- `uv run --with pytest pytest tests/test_rerank.py -q` → **13 passed** (report initially showed
  11; 2 `from_parts` tests added bring it to 13).
- Full suite `uv run --with pytest pytest tests/ -q` → **48 passed in 0.18s** (no regressions).

## Definition of done — PASS (all 3 encoded, each fails on wrong logic)
1. **PASS** `test_identical_engagement_higher_priority_sorts_first` — identical raw engagement,
   priority 2.0 vs 1.0 → high-priority sorts first, strictly higher score.
2. **PASS** `test_breakout_vs_own_low_baseline_beats_higher_raw_but_normal_item` — a 200k-view
   breakout from a LOW-baseline creator outranks a 1M-view item that is normal-for-its-creator
   (asserts the breakout has FEWER raw views, so a raw-views ranker would fail this).
3. **PASS** `test_high_priority_unique_low_engagement_item_not_bottom_ranked` — a priority-3.0
   creator's lone ~zero-engagement item is NOT last among 5 ordinary engaged items.
Plus: nothing-dropped + descending-order, empty list, absent weight, missing counts, garbage
date, half-life decay shape, and the two `from_parts` field-mapping tests.

## Concerns for the orchestrator
- **`creator_external_id` must be supplied at ingestion.** The `Upload` dataclass has NO channel
  id field — only `channel_name`. So whoever builds `RankableItem`s (the Stage-6 wiring in
  Sub-phase 4 / `orbit.py`) MUST pass `from_parts(..., creator_external_id=<source.external_id>)`
  from the matching `sources` row, or every item gets the neutral 1.0 weight and the
  `creator_weights` thumb-on-scale silently does nothing. Flagging because it's the one place
  this can break quietly.
- **Batch-median baseline is M1-only.** A creator with a single item this batch has
  relative-engagement 0 (neutral). This is correct for M1 but means the baseline-relative signal
  only bites when a creator has multiple items in one run. M3's historical store replaces it via
  the `creator_baselines` param — no `score_item` change needed.
- **Score is unbounded / not normalized.** Sub-phase 2 should tier by RANK POSITION / relative
  thresholds over the sorted distribution (as the brief already specifies), not by absolute score
  cutoffs, since scores scale with priority_weight and log-engagement magnitude.
- **`config` is duck-typed (`Any`).** `score_item`/`derank_items` only read `config.creator_weights`,
  so tests pass a `SimpleNamespace`. Real runs pass `OrbitConfig` — compatible.
```
