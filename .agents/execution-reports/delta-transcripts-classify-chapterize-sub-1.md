# Phase 2 / Sub-phase 1 — Delta detection of new uploads (Stage 1a)

**Status:** SUCCESS

## What I implemented
Added to `skills/orbit/scripts/lib/youtube_yt.py`:
- `Upload` dataclass (verbose, fully type-hinted) — the Phase-2 metadata contract.
- `YouTubeFetchError` exception — loud/actionable, distinct from `YouTubeAuthError` (the cookie-free uploads listing has no auth surface; its failure modes are timeout / missing binary).
- `fetch_new_uploads(source: dict, depth: str) -> list[Upload]` — runs `yt-dlp --flat-playlist --dump-json https://www.youtube.com/channel/<external_id>/videos` (argv list, never a shell string, **cookie-free** per scope), parses NDJSON, and returns only uploads whose `video_id` is NOT in `store.get_seen_ids(source["source_id"])`.
- Helpers: `_build_uploads_command`, `_parse_uploads_ndjson` (defensive skip + skipped-line warning, mirrors `_parse_subscriptions_ndjson`), `_coerce_optional_int`.
- Constants: `_YT_DLP_UPLOADS_TIMEOUT_SECONDS=120`, `_YOUTUBE_CHANNEL_UPLOADS_URL_TEMPLATE`.

Created `tests/test_delta_uploads.py` (5 tests, matches `tests/test_youtube_yt.py` pattern: temp DB via `ORBIT_DB_PATH` + `store._db_override`, patches `lib.subproc.run_with_timeout`, standalone `__main__` runner).

## Files modified (absolute)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/youtube_yt.py` (added to; now 635 lines, under the 1000 cap)
- `/Users/asheshsrivastava/frommyfeed/tests/test_delta_uploads.py` (new)

## Divergences from brief (+why)
- **New `YouTubeFetchError` rather than reusing `YouTubeAuthError`** — the brief allowed either; chose a new class because this path is cookie-free and has no auth failure mode, so labeling timeouts/missing-binary as "auth" errors would mislead. Matches existing error style (actionable message, README pointer, `from exc`).
- **No auth/non-zero-exit branch.** Unlike `load_youtube_subscriptions`, this function does not treat `returncode != 0` as a hard failure — it parses whatever NDJSON came back. Rationale: per scope the command is cookie-free and the only loud failures we own are timeout + missing binary (both raised). A partial/empty stdout safely yields `[]`. Flagging for orchestrator awareness; if a non-zero exit should also raise, that is a quick follow-up.
- Did **not** call `store.mark_seen` here (per brief) — marking is the pipeline driver's job, post-success, to avoid dropping items on a mid-pipeline crash.
- Did **not** add `chapters` to the parse — left for Sub-phase 4. The parse is a flat dataclass construction; adding `entry.get("chapters")` is a one-line, non-conflicting extension.
- `depth` is accepted, logged, and passed through but does NOT gate the listing (transcription cap = `TRANSCRIPT_LIMITS` is Sub-phase 2).

## Review findings + fixes (Step B/C)
Self-reviewed the diff. No critical/high issues. Notes addressed inline:
- Argv list (no shell string) — injection-safe. `bool` excluded from int coercion (bool is an int subclass; a stray True/False is not a real count). No cookies constructed/logged/transmitted anywhere in this path. Defensive NDJSON parse never crashes the feed on one bad line.

## Validation (Step D)
- Import check: `ok <function fetch_new_uploads> <class Upload> <class YouTubeFetchError>`.
- `pytest tests/test_delta_uploads.py -q` → **5 passed in 0.07s**
- Full suite `pytest tests/ -q` → **17 passed in 0.10s** (no regression)

## Definition of done (Step E): PASS
- 5 listed uploads, 2 pre-seeded via `store.mark_seen` → asserts EXACTLY the 3 unseen `video_id`s returned (specific ids, not just count). PASS
- Empty channel → returns `[]` AND a captured `delta_fetch_completed` log with `count=0`. PASS
- Extra trio: malformed-line-skipped (happy/edge), timeout-raises-`YouTubeFetchError` (failure). PASS
- No real yt-dlp call (boundary patched).

## Concerns for the orchestrator
- **Non-zero-exit handling** (see divergence) — decide whether a non-zero yt-dlp exit on the uploads listing should raise. Currently it does not; only timeout / missing-binary raise.
- **Cookie note:** kept cookie-free per the phase file. If real-world private/age-gated/region-locked uploads later need auth, that is a deliberate future change, not done here.

## Sub-phase 4 handoff — exact `Upload` dataclass field list
```python
@dataclass
class Upload:
    video_id: str
    title: str
    description: str
    upload_date: str          # yt-dlp YYYYMMDD (or "")
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    duration: int | None      # seconds — Sub-phase 4 long-form check: duration > 1200
    channel_name: str
```
- **`duration` lives on `Upload.duration`** (int seconds or None), parsed via `_coerce_optional_int(entry.get("duration"))`.
- **Room left for `chapters`:** the parse in `_parse_uploads_ndjson` builds `Upload(...)` from the same per-line `entry` dict, so Sub-phase 4 adds an Upload field (e.g. `chapters: list[dict] | None`) + `entry.get("chapters")` with no structural conflict. Both sub-phases edit `youtube_yt.py`; Sub-phase 4 depends on Sub-phase 1 (sequential, do not parallelize in worktrees).
