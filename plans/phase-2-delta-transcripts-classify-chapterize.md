# Phase 2: Delta fetch, VTT transcripts, classify, chapterize (YouTube)

**Milestone:** M1 — YouTube half, end to end
**Status:** Not started
**Estimated effort:** L

## Goal
For the YouTube subscriptions loaded in Phase 1, Orbit detects new uploads since last run, fetches their transcripts as VTT **retaining cue timestamps**, classifies each item on two axes (signal/noise × on/off-topic) with a channel-level prior, and chapterizes long-form videos into timestamped chapters that resolve to `watch?v=ID&t=Ns` — i.e. Stages 1-3 for YouTube run end to end.

## Resolved open questions folded into this phase
- **VTT cue retention (key design decision 4 / stack-notes gotcha).** The reference `youtube_yt.py` flattens transcripts to plain text in `_clean_vtt()` (`youtube_yt.py:396`), the exact line `re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3}\s*-->...')` deleting cue offsets. Sub-phase 2 below adds a **cue-preserving parser** (`parse_vtt_cues` → list of `(start_seconds, end_seconds, text)`) used instead of `_clean_vtt` for Orbit; the flattener may stay for any plain-text need but the timed path is the default. The reference's yt-dlp VTT command is lifted verbatim (`--write-auto-subs --sub-lang en,es,pt --sub-format vtt --skip-download`).
- **Chapterization long-form threshold (master-plan Q6).** Resolved: **long-form = video duration > 1200 seconds (20 min).** `duration` is already present in yt-dlp `--dump-json` output (seconds). Creator-supplied `chapters` (array of `{title,start_time,end_time}`) are obtainable from the same `--dump-json` call but are NOT parsed by the reference today — Sub-phase 4 adds `video.get("chapters")` to the parse. Policy: if creator chapters exist, use them verbatim; else if duration > 1200s, segment the transcript; else treat as a single short item (no chapters).
- **Depth throttle.** Lifted `TRANSCRIPT_LIMITS = {quick:0, default:2, deep:8}` and `DEPTH_CONFIG` shape are reused as the per-channel transcription cap (`quick` skips transcription entirely). `TRANSCRIPT_MAX_WORDS=5000` cap retained.

## Sub-phases

### Sub-phase 1: Delta detection of new uploads (Stage 1a)
- **Files touched:** `skills/orbit/scripts/lib/youtube_yt.py`
- **What ships:** `fetch_new_uploads(source, depth) -> list[Upload]` that lists a channel's recent uploads via `yt-dlp --flat-playlist --dump-json` of the channel's uploads URL, parses per-video metadata (`video_id`, `title`, `description`, `upload_date`, `view_count`, `like_count`, `comment_count`, `duration`, `channel_name`), then filters to videos whose `video_id` is NOT in `store.get_seen_ids(source_id)`. New `video_id`s are returned (and marked via `store.mark_seen` only after a successful run, to avoid losing items on crash). This replaces the reference's `ytsearchN:` keyword-search input model with a per-channel uploads-feed model.
- **Definition of done:** A test mocks the yt-dlp subprocess to return 5 upload JSON lines, pre-seeds `seen` with 2 of those `video_id`s, and asserts `fetch_new_uploads` returns exactly the 3 unseen ones (delta intent: previously-seen uploads must not resurface). A second test asserts an empty channel (no new uploads) returns `[]` and logs `delta_fetch_completed` with `count=0`. No real yt-dlp call.
- **Dependencies:** none (consumes Phase 1's `store` + `sources`)

### Sub-phase 2: VTT transcript fetch retaining cue timestamps (Stage 1b)
- **Files touched:** `skills/orbit/scripts/lib/transcribe.py`
- **What ships:** `parse_vtt_cues(vtt_text) -> list[TranscriptCue]` (each cue `start_seconds: float`, `end_seconds: float`, `text: str`), and `fetch_transcript_with_cues(video_id) -> Transcript` that runs the lifted yt-dlp VTT command (verbatim flags) and parses the result through `parse_vtt_cues` — NOT `_clean_vtt`. A `Transcript` exposes both the timed cue list and a `plain_text()` helper (joined, for classify/embed) while the cues survive for chapterize/deep-links. Honors `TRANSCRIPT_LIMITS[depth]` as the per-run transcription cap and `TRANSCRIPT_MAX_WORDS`. `build_deep_link(video_id, start_seconds) -> str` returns `https://www.youtube.com/watch?v=<id>&t=<int(start)>s`.
- **Definition of done:** A test feeds a real VTT fixture (`fixtures/sample.vtt` with known cues at 00:00:05.000 and 00:01:30.000) and asserts `parse_vtt_cues` returns cues with `start_seconds == 5.0` and `90.0` and the right text — i.e. timestamps SURVIVE (the core product invariant per design decision 4, not merely "a list is returned"). A test asserts `build_deep_link("abc", 90.0) == "https://www.youtube.com/watch?v=abc&t=90s"`. A test asserts `depth="quick"` fetches 0 transcripts.
- **Dependencies:** Sub-phase 1

### Sub-phase 3: Two-axis classification with channel prior (Stage 2)
- **Files touched:** `skills/orbit/scripts/lib/classify.py`, `skills/orbit/references/classify.md`
- **What ships:** `classify_item(item, channel_category, interests) -> Classification` producing Axis A (signal/noise) and Axis B (on/off-topic) per item, seeded by the channel-level `category` prior from `sources` and the user's `interests`. The LLM judgment call (the only model use here, Rule 5) is driven by the prompt template in `references/classify.md`; the function shapes the input, calls the host LLM via the established boundary, and parses a strict JSON verdict. User overrides in `store.classifications` (`is_user_override=1`) take precedence and are NEVER re-classified. Items failing either axis are tagged for the "they also posted" strip — never dropped (design decision 5). Results persist via `store.set_classification`.
- **Definition of done:** A test mocks the LLM boundary to return `{axis_a_signal:1, axis_b_on_topic:0}` and asserts the item is classified on-record and routed to the "also posted" bucket (not dropped) — encoding the brief's "never drop, derank" rule. A test asserts an item with an existing `is_user_override=1` classification is returned from store WITHOUT calling the LLM (override-persistence intent). A test asserts the channel prior seeds the verdict when the LLM is uncertain. LLM is mocked; no real call.
- **Dependencies:** Sub-phase 1 (needs upload items + channel category)

### Sub-phase 4: Chapterize long-form (Stage 3)
- **Files touched:** `skills/orbit/scripts/lib/chapterize.py`, `skills/orbit/references/chapterize.md`, `skills/orbit/scripts/lib/youtube_yt.py`
- **What ships:** Adds `video.get("chapters")` to the `youtube_yt.py` upload parse so creator chapters flow through. `chapterize_episode(upload, transcript) -> list[Chapter]` (each `Chapter`: `title`, `start_seconds`, `deep_link`): if `upload.duration <= 1200` → returns `[]` (short item, no chapters); elif creator `chapters` present → maps them verbatim to `Chapter`s with deep-links from `start_time`; else → segments the transcript cues via the LLM (prompt in `references/chapterize.md`) detecting topic shifts, labeling each segment, attaching the segment's first cue `start_seconds`. Long-form stays ONE episode unit — never shredded (design decision 7). Every chapter resolves to a `watch?v=ID&t=Ns` deep-link built from cue offsets.
- **Definition of done:** A test with a `duration=1800` upload carrying creator `chapters` asserts they're used verbatim and each `Chapter.deep_link` ends in the correct `&t=Ns` from `start_time` (deep-link survival intent). A test with `duration=600` (under threshold) asserts `chapterize_episode` returns `[]` (short-item rule). A test with `duration=1800`, no creator chapters, mocked LLM segmentation asserts chapters carry the transcript cue's `start_seconds` (the timestamp must trace back to a real cue, not be invented). LLM mocked.
- **Dependencies:** Sub-phase 2 (needs cue-preserving transcript), Sub-phase 1 (needs duration + chapters in the parse)

## Phase-level definition of done
`pytest tests/` for this phase passes. Given Phase 1's populated `sources`, a mocked end-to-end run over a fixture channel: detects new `video_id`s not in `seen`, fetches a VTT transcript whose cue timestamps are intact, classifies each item on both axes with overrides respected, and chapterizes a >20-min fixture into chapters whose deep-links resolve to the correct `watch?v=ID&t=Ns`. The cue-timestamp invariant (a deep-link's `t=` equals a real transcript cue offset) holds across the chain.

## Out of scope
- No clustering / overlap / trending (M3).
- No ranking into density tiers or HTML render (Phase 3).
- No X source (M2).
- No live LLM or yt-dlp calls in tests — all mocked at the boundary.

## Open questions
- The exact classify/chapterize prompt wording in `references/*.md` is drafted in this phase; tuning happens during the maintainer's real-day usage (M1's built-in tuning loop per master-plan riskiest-assumption test). Not blocking.
- Transcript-segmentation chunking strategy for very long episodes (token budget vs cue density) — default to feeding cue-grouped windows; refine if a real 3-hour podcast overflows. Noted, not blocking.

## Self-critique

**Product lens:** PASS. Traces directly to brief Stages 1-3 and to the headline feature (deep-links into the exact moment) via the cue-retention invariant. Design decisions 4 (retain VTT cues), 5 (two-axis + prior, never drop), and 7 (long-form stays a unit) each map to a sub-phase DoD. No out-of-brief features. The riskiest assumption (ranking earns the daily open) is set up here — classification feeds the rank in Phase 3 — and is correctly tested in the first phases of M1, not deferred to M2/M3.
**Engineering lens:** PASS. All within stack (Python, yt-dlp subprocess, host LLM for judgment only). DoDs are structural/behavioral and fresh-context checkable (cue offsets equal expected floats; override skips the LLM). The only LLM use is classify + chapterize-segmentation — both genuine judgment calls (Rule 5); delta detection, deep-link URL building, and the duration threshold are deterministic code. Sub-phase 4 does not cement an external API shape; it consumes Sub-phases 1-2's data shapes which are internal.
**Risk lens:** Findings + fixes. (1) **File-boundary conflict:** Sub-phases 1 and 4 BOTH edit `youtube_yt.py` (1 adds the upload parse, 4 adds `chapters` to that same parse). Resolved by making Sub-phase 4 depend on Sub-phase 1 explicitly (sequential, same-region edit ordered) — flagged so `/run-phase` does not parallelize these two in worktrees. (2) Test coverage: each sub-phase DoD has a test that fails on wrong business logic (delta no-resurface, cue-timestamp survival, override-no-LLM, short-item-no-chapters) per Rule 9. (3) Painting-into-a-corner: 1→2→3→4 — Sub-phase 3 (classify) does not need cues, Sub-phase 4 (chapterize) does, and Sub-phase 2 produced them before 4 runs; order holds.
**Irreversible sub-phases:** None. (Writes to `classifications`/`seen` are data rows on the existing schema, not migrations or destructive ops; re-runnable. `seen` is marked only post-success to avoid dropping items on crash.)
