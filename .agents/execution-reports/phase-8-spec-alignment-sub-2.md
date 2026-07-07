# Phase 8 — Sub-phase 2 execution report: Long-form floor + category taxonomy gating

## STATUS: SUCCESS

## What I implemented

### 600s long-form floor
- New pure lib module `scripts/lib/stage1_youtube.py` holding:
  - `MIN_UPLOAD_DURATION_SECONDS = 600` (distinct from `chapterize.LONG_FORM_THRESHOLD_SECONDS = 1200`, which gates chapterize eligibility, not inclusion).
  - `drop_short_form_uploads(uploads) -> (long_form_uploads, dropped_count)` — drops uploads with `duration is not None and duration < 600` BEFORE the classify call. `duration is None` is KEPT (fail-open, per commit 654f0fb).
  - `_select_recent_uploads(...)` — **moved verbatim** out of `orbit.py` (see divergence note) so orbit.py did not grow.
- `run_stage1_build_youtube_items` (orbit.py) calls `drop_short_form_uploads` on the recency-windowed uploads before the per-channel classify-budget slice, so short clips never spend LLM budget. Per-run count logged as `short_form_dropped_count` in `youtube_stage1_build_completed`.

### Category axis (third classify axis)
- `Classification` (classify.py) gains `category: str = _CATEGORY_KEEP_ON_PARSE_FAILURE`.
- Fixed taxonomy `_CATEGORY_TAXONOMY = ("ai","business","tech","sports","other")`.
- New `_coerce_category` (case/whitespace tolerant; only taxonomy members accepted).
- `_parse_verdict` now returns `(axis_a, axis_b, category)`. A missing/garbled/off-taxonomy category fails OPEN to the keep-sentinel `"unknown"` (outside the taxonomy, so the gate keeps it) and logs `classify_category_unparseable` — never defaults to `"other"` (which would drop).
- `references/classify.md` gained a "The category (fixed taxonomy)" section and the output contract now includes `"category"`. Braces avoided in prose so `.format()` substitution stays intact.
- Category is NOT persisted to the store — it is a Stage-1 inclusion gate consumed from the returned `Classification`. No store schema change (kept scope minimal; store.py not in the allowed file list).

### Gating (both sources, shared classify path)
- YouTube: after a successful classify, `if classification.category == "other": category_dropped_count += 1; continue`. Left UNSEEN so a later prompt fix reconsiders it.
- X: same gate added after the existing Axis-A alpha gate in `run_stage1_build_x_items`.
- Both log a `category_dropped_count` field in their `*_stage1_build_completed` info logs.

## Files created / modified
- Created: `scripts/lib/stage1_youtube.py`
- Modified: `scripts/orbit.py`, `scripts/lib/classify.py`, `references/classify.md`, `tests/test_classify.py`, `tests/test_classify_x.py`, `tests/test_orbit_youtube_producer.py`

## Divergences from the plan (and why)
1. **Dataclass default is the keep-sentinel `"unknown"`, not the plan's literal `category: str = "other"`.** The plan text itself contradicts `= "other"` two lines later ("a missing category defaults to keep"), and the team-lead brief is explicit: garbled → keep, never "other". Critically, the user-override short-circuit constructs `Classification(...)` without a category; a `"other"` default would make the category gate DROP sacred user-corrected items. Keep-sentinel default protects that path. This is the self-consistent reading (Rule 7).
2. **Extracted `_select_recent_uploads` into the new lib module** (authorized: "extract the stage-1 YouTube helpers to a new lib module"). This freed ~52 lines from orbit.py, more than offsetting the ~15 lines the two gates added, keeping orbit.py at 1077 (was 1098). Tests reference `orbit._select_recent_uploads`; that still resolves via re-import — no test change needed for it.
3. **Category-drop surfaced as a `category_dropped_count` field** in the existing completion log, matching the established `dropped_noise_count` field convention (Rule 11), rather than a separate `youtube_stage1_category_dropped`/`x_stage1_category_dropped` event. Same "dropped and counted" DoD guarantee.
4. **Updated two pre-existing YouTube producer tests** (`test_youtube_producer_classifies_chapterizes_and_marks_seen`, `test_youtube_producer_skips_item_when_classify_times_out`) that incidentally used 300s uploads — now below the floor, they returned empty. Lifted those durations to 800s (clears the 600s floor, stays below the 1200s chapterize threshold, so their "included but no chapters" / "classify-timeout skip" intent is preserved). These files are in the allowed set.

## Code review findings + fixes
Self-reviewed the scoped diff. Findings, all addressed during implementation:
- **Override path could be dropped by the gate** — fixed by choosing the keep-sentinel as the dataclass default (divergence 1). Verified: override path takes the default, `"unknown" != "other"`, survives.
- **`.format()` brace hazard in classify.md** — the taxonomy is written as a backtick list with no literal single braces; the JSON example doubles its braces. Prompt renders without KeyError (asserted by `test_classify_prompt_renders_the_fixed_taxonomy`).
- **Fail-open logging present on every category fallback path** (both the not-a-dict early return and the coerce-None path), with `fix_suggestion`. No silent empties.
No critical/high issues outstanding.

## Validation results
- Targeted: `tests/test_classify.py tests/test_classify_x.py tests/test_orbit_youtube_producer.py` — **30 passed**.
- Full suite: `.venv/bin/python -m pytest tests/ -q` — **234 passed** (was 225; +9 new tests). Sub-phase 1's changes present and green.
- `ruff check` on all six touched files + new module — **All checks passed**.
- `ruff format --check scripts/lib/stage1_youtube.py` — already formatted (new file clean).
- `wc -l scripts/orbit.py` — **before: 1098, after: 1077** (≤ 1098 ✓).

## Definition of done: PASS
- 400s upload never reaches classifier ✓ (`test_youtube_producer_short_form_upload_never_reaches_classifier`)
- duration=None survives to classify ✓ (`test_youtube_producer_missing_duration_upload_survives_to_classify`)
- category "other" → dropped + counted, YouTube ✓ and X ✓
- each of ai/business/tech/sports → kept ✓ (`test_each_taxonomy_category_is_parsed_onto_the_classification`)
- unparseable/off-taxonomy category → kept + logged ✓ (two classify tests)
- X path gates identically ✓ (`test_x_producer_drops_category_other_tweet`)
- prompt renders taxonomy from references/classify.md ✓
- Full `pytest tests/` green ✓; orbit.py ≤ 1098 ✓; ruff clean ✓

## Concerns for the orchestrator
- **Category is not persisted** to `store.classifications`. If a later phase wants category-based analytics or re-gating from stored rows, a store migration is needed. This phase intentionally kept it in-memory (store.py out of scope).
- **Sub-phase 3 also touches `orbit.py`'s X stage-1 tail** (the top-8 cap). My X edit is inside `run_stage1_build_x_items` (the category gate + completion-log field). No overlap with a stage-3 rank-seam cap, but if SP3 edits the same function, coordinate the merge.
- **Short-form-dropped uploads are left UNSEEN** (not mark_seen'd), so they re-filter cheaply (no LLM) each run until they age past the 2-day recency window. Deliberate; negligible cost.
