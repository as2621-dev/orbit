# Sub-phase 3 execution report — YouTube subscriptions loader (Stage 0)

## What
Implemented `lib/youtube_yt.py`: a typed `Subscription` dataclass, `load_youtube_subscriptions(cookie_source)` (runs yt-dlp against the authenticated `/feed/channels` feed via `subproc.run_with_timeout`, parses NDJSON defensively, fails loud+actionable on auth errors), and `persist_subscriptions(subscriptions)` (upserts each channel into `sources` as `platform="youtube"`, `category="signal"`, `last_refreshed_at=UTC now`). Added a `YouTubeAuthError` exception. Added a 4-line fixture + 3 DoD tests.

## Files touched (absolute)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/youtube_yt.py` (new, ~290 LOC, < 500)
- `/Users/asheshsrivastava/frommyfeed/tests/test_youtube_yt.py` (new)
- `/Users/asheshsrivastava/frommyfeed/tests/fixtures/youtube_subs.jsonl` (new)

Touched nothing else (store.py, log.py, subproc.py, orbit.py untouched). No commit.

## Divergences + why
- **Timeout/FileNotFoundError also raise `YouTubeAuthError`.** The spec said "raise a clear error" for timeout. To keep callers handling one error type and to stay loud+actionable, both a `SubprocTimeout` and a missing-yt-dlp `FileNotFoundError` are surfaced as `YouTubeAuthError` with distinct, actionable messages (network/install guidance) rather than a separate class. Low risk; can split later if Sub-phase 4 wants distinct handling.
- **Extra stderr scrubber `_scrub_cookie_surface`.** `log.redact()` only redacts credential-NAMED fields; raw stderr is free text it can't inspect. Added a boundary scrubber that drops cookie-mentioning stderr lines and strips the `cookie_source` token before logging. The browser name itself is logged (spec permits) but the whole cookie surface is scrubbed per the hard rule.

## Review findings + fixes
- **Self-review (git diff):** (1) argv built as a list, not a shell string — injection-safe (high-value, correct). (2) Every `log.*` call audited: the only raw-stderr log path routes through `_scrub_cookie_surface` first; no code path logs a cookie value. (3) Error paths are loud (raise) not silent. (4) One-bad-line resilience: parser skips blank/unparseable/no-id lines and logs a single warning count, never crashes. No critical/high issues found; no fixes needed.

## Validation outputs
- AST parse: `AST OK` (both files).
- Import clean: `IMPORT OK <class 'lib.youtube_yt.Subscription'> <class 'lib.youtube_yt.YouTubeAuthError'>` from scripts dir under venv Python 3.12.
- pytest: **`3 passed in 0.05s`** (Python 3.12.13, pytest-9.1.0).
- No `*.db` in repo (temp DBs only, via tempfile). `git status` shows only new untracked files.

## DoD per bullet (PASS/FAIL)
- PASS — `load_youtube_subscriptions` invokes yt-dlp with the exact argv via `subproc.run_with_timeout`, parses one JSON object per line into `Subscription(channel_id, display_name)`.
- PASS — `persist_subscriptions` upserts each into `sources` (`platform=youtube`, `category=signal`, `last_refreshed_at=now`).
- PASS — cookie-source/stderr redacted from any error log; never logs cookie values.
- PASS — auth failure raises a clear, actionable error pointing at README §8.6, not a stack trace.
- PASS — test 1: mocked NDJSON → asserts exact channel_ids + count.
- PASS — test 2: `persist_subscriptions` → rows queryable via `store.list_sources(platform="youtube")`.
- PASS — test 3: "no cookies found" stderr → raises `YouTubeAuthError` mentioning sign in / README.
- PASS — no test invokes real yt-dlp or network (subproc patched).

## Concerns
- README §8.6 wording is referenced by constant `_README_TROUBLESHOOTING_POINTER`; actual README ships in M4 (tracked, non-blocking — matches plan's open question).
- Auth-signal substring list is deliberately broad ("login", "consent"); a non-zero exit already triggers failure, so the list mainly catches auth signals on a zero-exit edge case. False-positive risk is low and fails safe (loud).

## Exact public API (for Sub-phase 4 / Phase 2 & 4 wiring)
```python
@dataclass
class Subscription:
    channel_id: str
    display_name: str

class YouTubeAuthError(Exception): ...

def load_youtube_subscriptions(cookie_source: str) -> list[Subscription]: ...
def persist_subscriptions(subscriptions: list[Subscription]) -> int: ...
```
Import path: `from lib import youtube_yt` (scripts dir on sys.path, as orbit.py/store.py do).
