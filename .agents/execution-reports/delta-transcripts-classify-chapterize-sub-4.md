# Execution Report — Phase 2, Sub-phase 4: Chapterize long-form (Stage 3)

**Status:** COMPLETE (uncommitted — orchestrator commits at phase end)
**Date:** 2026-06-18

## Implemented

`chapterize_episode(upload, transcript, *, segmenter)` and its decision tree (Rule 5: only
transcript SEGMENTATION is a model judgment; threshold, creator-chapter mapping, snapping,
and deep-link building are deterministic):

1. `duration is None or <= 1200s` → `[]` (short stays short), log `chapterize_skipped_short`.
2. creator `upload.chapters` present → mapped verbatim to `Chapter`s, deep-links from each
   `start_time`, no LLM. Log `chapterize_used_creator_chapters`.
3. long-form, no creator chapters → render cues → injected segmenter → parse strict-JSON
   segment list → SNAP each `start_seconds` to the nearest real cue offset → deep-links from
   the snapped offset. Log `chapterize_segmented`. Empty transcript → `[]`
   (`chapterize_no_cues`); unparseable/no-usable-segment verdict → single chapter at first
   cue (`chapterize_verdict_unparseable` / `chapterize_no_usable_segments`). Never crashes,
   never invents a timestamp.

Long-form stays ONE episode unit: a `list[Chapter]` is returned for the single upload; the
upload is never split (documented in module + function docstrings).

## Files (absolute)

- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/youtube_yt.py` — SURGICAL edit:
  added `chapters: list[dict] | None = None` (last, defaulted) to the `Upload` dataclass +
  docstring; added one parse line `chapters=entry.get("chapters")` in `_parse_uploads_ndjson`.
  Nothing else touched — `fetch_new_uploads` logic untouched.
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/chapterize.py` — NEW (~310 lines, under 500 limit).
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/references/chapterize.md` — NEW prompt template
  (placeholders `{video_title}`, `{cue_lines}`; "tuned during real usage" note at top; JSON example braces doubled).
- `/Users/asheshsrivastava/frommyfeed/tests/test_chapterize.py` — NEW (6 tests).

## Divergences + why

- Prompt placeholders are `{video_title}` and `{cue_lines}` (the brief said "joined cue lines /
  transcript window" generically). `cue_lines` renders each cue as `<start_seconds>\t<text>` so
  the model can only pick among real cue offsets in the left column; the snap step then enforces it.
- Fallback chapter title is `"Full episode"` (brief didn't specify a label). Mirrors classify.py's
  sensible-default posture; surfaced here for visibility.
- Added two extra defensive log paths beyond the brief's single fallback: `chapterize_no_cues`
  (long-form but empty transcript → `[]`) and `chapterize_no_usable_segments` (model returned a
  list but no numeric offsets → single-chapter fallback). Both are within the "never crash, never
  invent" contract; flagged for review.

## Review + fixes

Self-reviewed the diff. The youtube_yt.py edit is the risk point (shared with Sub-phase 1):
verified it is exactly one defaulted field + one parse line, in the existing `Upload(...)`
construction, with `fetch_new_uploads` and `_parse_uploads_ndjson`'s skip/parse logic untouched.
Re-ran `tests/test_delta_uploads.py` → still passes (no regression). No critical/high findings;
no fixes needed.

## Validation — PASS

- Import check: `chapterize.chapterize_episode` resolves; `Upload(...).chapters` defaults to
  `None`; `LONG_FORM_THRESHOLD_SECONDS == 1200`.
- `pytest tests/test_chapterize.py tests/test_delta_uploads.py -q` → `11 passed`.
- FULL suite `pytest tests/ -q` → **`37 passed in 0.13s`** (phase-level gate, no regression).

## DoD — PASS

- Creator chapters used verbatim + each `Chapter.deep_link` ends in correct `&t=Ns`
  (`&t=0s`, `&t=300s`) from `start_time`; segmenter `assert_not_called` (deterministic path). PASS
- `duration=600` → `[]`, segmenter never called. PASS
- Segmentation: model offsets 125/590 snap to real cues 120.0/600.0; every `Chapter.start_seconds`
  ∈ real cue offsets; deep_link == `build_deep_link(id, snapped_offset)`. PASS
- Unparseable verdict → single chapter at first real cue, no crash. PASS
- Default segmenter raises `NotImplementedError` (fail loud). PASS
- Phase-level invariant (a deep-link's `t=` equals a real cue offset) holds; LLM mocked. PASS

## Concerns

- None blocking. The single-chapter fallback emits a synthetic `"Full episode"` title — fine for
  M1, may want a real-title fallback later.
- Chunking strategy for very long episodes (token budget vs cue density) is unchanged from the
  phase's noted open question — the prompt feeds all kept cues (already word-capped at 5000 by
  Sub-phase 2's `TRANSCRIPT_MAX_WORDS`). Not blocking.

## Phase 3 handoff (rank / density / render)

**`Chapter` shape** (`lib.chapterize.Chapter`):
- `title: str` — short human-readable segment label.
- `start_seconds: float` — ALWAYS a real source offset (creator `start_time` or a real cue's
  `cue_start_seconds`); never invented.
- `deep_link: str` — `https://www.youtube.com/watch?v=<id>&t=<int(start)>s`. **The deep-link
  timestamps live in `deep_link` (whole-second `t=`), derived from `start_seconds`.**

**Classified + chapterized item record Phase 3 consumes** (assembled by the pipeline driver; not
a new type — the three existing shapes travel together per upload):
- `Upload` (`lib.youtube_yt.Upload`): `video_id, title, description, upload_date, view_count,
  like_count, comment_count, duration, channel_name, chapters` (raw creator array or None).
- `Classification` (`lib.classify.Classification`): `item_external_id (== video_id), axis_a_signal,
  axis_b_on_topic, is_user_override`, plus property `is_also_posted` (True if it fails either axis →
  "they also posted" strip; never dropped).
- `list[Chapter]` — empty for short items (`duration <= 1200` or None) and for long-form items with
  no usable transcript; otherwise the sub-navigation into that ONE upload.

Phase 3 ranks/renders the `Upload` keyed by `Classification` (top-line vs. also-posted) and renders
each `Chapter.deep_link` as a jump-to-moment link. Long-form is ONE item with chapters underneath —
not multiple items.
