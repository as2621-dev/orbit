# Sub-phase 2 execution report — VTT transcript fetch retaining cue timestamps (Stage 1b)

Phase: Phase 2 — delta-transcripts-classify-chapterize
Status: SUCCESS

## Implemented
New Orbit module `transcribe.py` implementing the cue-RETENTION transcript path (the
opposite of the reference's `_clean_vtt` flattener), plus its DoD tests and a VTT fixture.

- Module docstring explains the cue-retention design decision (timestamps survive because
  they power chapterize + `watch?v=ID&t=Ns` deep-links — the headline feature).
- Constants lifted verbatim: `TRANSCRIPT_LIMITS = {"quick":0,"default":2,"deep":8}`,
  `TRANSCRIPT_MAX_WORDS = 5000`.
- `TranscriptCue` dataclass: `cue_start_seconds: float`, `cue_end_seconds: float`, `text: str`.
- `Transcript` dataclass: `video_id: str`, `cues: list[TranscriptCue]`, `plain_text()` (joins
  cue text without mutating cues), `word_count()` helper.
- `parse_vtt_cues(vtt_text)`: cue-preserving parser. Tolerates `HH:MM:SS.mmm` and `MM:SS.mmm`,
  ignores trailing position/align settings, skips cue-number-only lines, strips inline `<...>`
  tags, converts each timestamp to total seconds (hours*3600 + minutes*60 + seconds.millis).
- `fetch_transcript_with_cues(video_id, depth="default")`: quick=0 gate (returns None, logs
  `transcript_skipped_quick_depth`, no yt-dlp call); else builds the lifted argv into a
  `tempfile.TemporaryDirectory`, runs via `subproc.run_with_timeout` (45s), reads produced
  `.vtt`, parses via `parse_vtt_cues`, applies `TRANSCRIPT_MAX_WORDS` cap (drops whole trailing
  cues, kept cues retain timestamps). SubprocTimeout / FileNotFoundError / no-captions all log
  with `fix_suggestion` and return None.
- `build_deep_link(video_id, start_seconds)`: returns `https://www.youtube.com/watch?v=<id>&t=<int(seconds)>s`.

## Files touched (absolute)
- /Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/transcribe.py (NEW, ~430 lines)
- /Users/asheshsrivastava/frommyfeed/tests/test_transcribe.py (NEW)
- /Users/asheshsrivastava/frommyfeed/tests/fixtures/sample.vtt (NEW)

## Divergences + why
- yt-dlp argv flags lifted verbatim per the prompt (`--write-auto-subs --sub-lang en,es,pt
  --sub-format vtt --skip-download -o <tmp>/%(id)s`). I did NOT lift the reference's extra
  `--ignore-config --no-cookies-from-browser --no-warnings` flags — the prompt specified the
  four verbatim flags and the public uploads/transcript path in Orbit is cookie-free; kept it
  minimal to match the prompt's exact list. Flag for reviewer if the extra flags are desired.
- Timeout set to 45s (prompt allowed ~30-60s); reference used 30s. Slight headroom for slow
  caption fetch.
- Per-RUN numeric count cap (default=2, deep=8) is NOT enforced here — only the quick=0 gate is.
  The numeric cap across many videos is the pipeline driver's loop concern (per prompt). This
  function transcribes a single video and gates quick=0.
- No retry/backoff (the reference has transient-error retries). Out of scope for this sub-phase's
  DoD; kept simple per Rule 2. Failures return None loudly-but-gracefully with fix_suggestion.

## Review findings + fixes
- Checked: `result` is referenced after the `with tempfile` block in the no-captions path. Both
  except branches `return None` early, so `result` is guaranteed bound when reached. No bug.
- Walrus-operator awkwardness in the deep-link test was simplified to a plain equality assert.
- Argv is a list (no shell string); video_id cannot be shell-reinterpreted. No secrets logged.

## Validation
- Import check: `ok <function parse_vtt_cues> <function build_deep_link>`
- `pytest tests/test_transcribe.py -q` → `7 passed in 0.04s`
- Full suite `pytest tests/ -q` → `24 passed in 0.10s` (no regression)

## DoD check — PASS
- parse_vtt_cues on fixtures/sample.vtt: cues[0].cue_start_seconds == 5.0 (text "Welcome to the
  show"); cue at 90.0 exists with cue_end 94.0 and text "Now we get to the good part" (inline
  `<00:01:30.500>` / `<c>` tags stripped). Timestamps SURVIVE. PASS.
- build_deep_link("abc", 90.0) == "https://www.youtube.com/watch?v=abc&t=90s"; 90.7 -> t=90s. PASS.
- depth="quick" returns None and run_with_timeout asserted NOT called. PASS.
- Happy path: fixture VTT flows through fetch_transcript_with_cues (yt-dlp subprocess + .vtt read
  both mocked) to a Transcript with intact 5.0 and 90.0 cues; plain_text does not destroy cues. PASS.

## Concerns
- The extra reference flags (`--ignore-config --no-cookies-from-browser --no-warnings`) were
  intentionally omitted to match the prompt's verbatim four-flag list. If real-world yt-dlp runs
  pick up a user config or stray cookies, revisit.
- Numeric per-run cap must be wired by the pipeline driver (this module only enforces quick=0).

## Sub-phase 4 handoff (CRITICAL)
Import: `from lib import transcribe`.

- `transcribe.TranscriptCue` fields: `cue_start_seconds: float`, `cue_end_seconds: float`,
  `text: str`.
- `transcribe.Transcript` fields: `video_id: str`, `cues: list[TranscriptCue]`; methods
  `plain_text() -> str` and `word_count() -> int`. Cues are in document order.
- To get a cue's start offset for a deep-link: `cue.cue_start_seconds` (a float in total seconds).
  Chapterize should attach a segment's FIRST cue's `cue_start_seconds`.
- Deep-link builder to REUSE (do not reinvent): `transcribe.build_deep_link(video_id: str,
  start_seconds: float) -> str` → `https://www.youtube.com/watch?v=<video_id>&t=<int(start_seconds)>s`.
  It int-truncates the seconds.
