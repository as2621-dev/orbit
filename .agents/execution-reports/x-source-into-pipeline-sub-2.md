# Phase 4 — Sub-phase 2: Python wrapper for Following — X source loading (Stage 0)

## What was implemented
`skills/orbit/scripts/lib/bird_x.py` — the X Following loader (Stage 0 for X):

- `@dataclass Follow` — `creator_handle`, `display_name`, `rest_id` (exactly Sub-phase 1's `--following --json` shape).
- `class XAuthError(Exception)` — loud, actionable auth error; message points the user at re-logging-in to X and the README troubleshooting section (§8.6). Never contains a credential value.
- Borrowed from the reference `bird_x.py`: `set_credentials`, `_has_injected_credentials`, `_has_process_credentials`, `_subprocess_env` (merges injected creds + sets `BIRD_DISABLE_BROWSER_COOKIES=1`), `is_bird_installed`, `DEPTH_CONFIG = {quick:12, default:30, deep:60}`, `_BIRD_SEARCH_MJS` path. NOT carried: `search_x`, relevance, mentions, topic-search machinery (Orbit is subscription-model).
- `load_x_following(cookie_source) -> list[Follow]` — resolves cookies at runtime (injected → env → Node browser-store fallback), resolves the numeric self-id, shells `node bird-search.mjs --following <id> --json` via `subproc.run_with_timeout`, parses stdout JSON to `Follow` records. Raises `XAuthError` on auth failure / timeout / spawn failure / error payload.
- `persist_following(follows) -> int` — upserts each into `sources` with `platform="x"`, `external_id=creator_handle`, `display_name`, `category="signal"`, `last_refreshed_at=datetime.now(timezone.utc).isoformat()`. Returns count. Queryable via `store.list_sources(platform="x")`.
- Helpers: `_parse_stdout` (JSON-or-None), `_parse_follows` (skips malformed entries, strips leading `@`), `_is_auth_failure` (covers all three documented signals).

## Numeric-userId resolution decision (honest, not a silent no-op)
The Following GraphQL op requires a **numeric** `userId`. Per Sub-phase 1's report, the vendored CLI's `--whoami` returns the cookie SOURCE string (e.g. `"Chrome"`), NOT the user id, and the read-only Node client has no screen_name→id lookup. I took the prompt's **option (a), the simplest honest path**: the numeric self-id is supplied as config via a new `X_USER_ID` env var (`X_USER_ID_ENV_VAR`), and `load_x_following` passes that numeric id straight to `--following`.

`_resolve_self_user_id()` reads `X_USER_ID`; if unset or non-numeric it raises `XAuthError` **loud** with a message naming the env var and pointing at §8.6 — it does NOT hand the CLI a screen_name (which Sub-phase 1's mixin would reject) and does NOT silently no-op. This is honest: the constraint (numeric id needed, CLI can't self-resolve it) is surfaced to the user as a config requirement, and the key testable behaviors (parse canned JSON; raise loud on auth failure; never log credentials) hold regardless. Sub-phase 3 / 4 / the README (§8.6, M4) should document setting `X_USER_ID`; a future enhancement could add an in-Node `getCurrentUser` lookup (out of scope — Sub-phase 1 owns the CLI).

## Files created/modified
- NEW `skills/orbit/scripts/lib/bird_x.py`
- NEW `tests/test_bird_x_following.py`
(No other files touched — Rule 3.)

## Divergences from the plan + why
- Plan says `bird-search.mjs --following <me>` where `<me>` is resolved. Implemented: `<me>` = the numeric `X_USER_ID` env value (the documented decision above). The CLI takes a numeric id (Sub-phase 1); resolving screen_name→id is impossible in the vendored client, so config supplies it. Surfaced, not silent.
- Auth-error message points at "the README troubleshooting section (§8.6)" — the brief's §8.6 reference; the README itself is an M4 deliverable.

## Code-review findings + fixes
- **[fixed, low] unused `import time`** — removed (the reference's JSON-decode-retry-with-sleep was not carried over; a single subprocess call here).
- **[low] `_is_auth_failure` matches `"auth"` substring broadly** — kept. The prompt enumerates the auth-failure signals; a false-positive auth error (loud) is strictly safer than a silent empty load. No fix.
- **[info] partial/malformed entry handling** — `_parse_follows` skips entries missing `creator_handle`/`rest_id` rather than crashing the whole load. Matches the "fail loud at the boundary, be resilient inside" posture.
- **[info] cookie value never in any throw** — every `XAuthError` message is a literal/constant or wraps an `OSError`/X error string; no credential is ever interpolated. Verified by reading every `raise`.
- No critical/high findings.

## Validation results (exact commands)
- `python3 -c "import ast; ast.parse(...)"` → `ast ok`
- `cd skills/orbit/scripts && python3 -c "from lib import bird_x"` → `import ok`
- `uv run --with pytest pytest tests/test_bird_x_following.py -q` → **3 passed** in 0.06s
- `uv run --with pytest pytest tests/ -q` (full suite, no-regression) → **75 passed** in 0.14s

Subprocess boundary mocked via `patch.object(bird_x.subproc, "run_with_timeout", return_value=<canned SubprocResult>)`. NO live X call, NO real cookies.

## Definition of done — PASS (all 3 required tests)
1. **Parse + persist** — `test_load_following_parses_and_persists`: canned `--following --json` → `creator_handle`s `["alice","bob","carol"]` (leading `@` stripped), `rest_id`/`display_name` correct, subprocess invoked with the NUMERIC self-id (`777000`) + `--json`; `persist_following` returns 3 and the three are queryable via `store.list_sources(platform="x")` as `signal`-category X rows with a `last_refreshed_at`. **PASS.**
2. **Loud auth failure** — `test_auth_failure_raises_loud_error`: subprocess returns `{"error":"No Twitter credentials found","items":[]}` exit 1 → raises `XAuthError` whose message mentions logging into X / re-run AND the README/troubleshooting/§8.6 pointer. **PASS.**
3. **No-credential-logging security invariant** — `test_no_credential_value_appears_in_logs`: dummy `auth_token`/`ct0` injected via `set_credentials`, both the happy path AND the auth-failure error path run under `redirect_stdout`; asserts neither dummy value appears in any captured JSON log line, and that the stream was actually captured (`x_following_load_started` present). **PASS.**

## SECURITY — confirmed (yes)
- Cookies read **only at runtime**: injected via `set_credentials` (held in `_credentials`), merged into the Node subprocess **env** by `_subprocess_env`, never passed as CLI args, never written to disk.
- **Never logged**: proven by `test_no_credential_value_appears_in_logs` (the dummy token is absent from the captured stdout log stream, including the error path). `lib.log` also auto-redacts credential-keyed fields, and no log call passes a raw token as a field.
- **Never in exception messages**: every `XAuthError` message is a constant or wraps a non-credential OS/X error string.
- **No real tokens in fixtures**: only `dummy_auth_token_...` / `dummy_ct0_...` placeholders.

## Concerns for the orchestrator + Sub-phase 3 handoff
- **Config requirement to document (README §8.6, M4):** users must set `X_USER_ID` (numeric self-id) in addition to `AUTH_TOKEN`/`CT0`. Without it, `load_x_following` raises `XAuthError` loud. Orchestrator should ensure Stage-0 wiring (Sub-phase 4 / `orbit.py`) surfaces this requirement.
- **`Follow` shape for Sub-phase 3:** `Follow(creator_handle: str, display_name: str, rest_id: str)`. Sub-phase 3's `fetch_new_tweets` selects from X sources — query them back via `store.list_sources(platform="x")` (rows carry `external_id`=handle, `display_name`, `source_id` for the seen/delta engine). The `rest_id` is NOT persisted to `sources` (only `external_id`=handle is) — if Sub-phase 3 needs numeric ids per handle, either re-load via `load_x_following` or note that `from:handle` SearchTimeline keys on the handle (no numeric id needed for the per-handle delta).
- **DEPTH_CONFIG for the deep-pull budget:** `bird_x.DEPTH_CONFIG = {"quick":12, "default":30, "deep":60}` — Sub-phase 3 derives the per-run round-robin window size from `DEPTH_CONFIG[depth]`.
- **Pacing:** Sub-phase 1's `following` honors `pageDelayMs` (default 1000ms) between cursor pages; Sub-phase 3 wires the inter-request delay between handles + bounded concurrency.

## Return to orchestrator
1. **STATUS: SUCCESS**
2. Files touched: `skills/orbit/scripts/lib/bird_x.py`, `tests/test_bird_x_following.py`
3. Validation: **PASS** — `uv run --with pytest pytest tests/test_bird_x_following.py -q` → 3 passed; full suite `uv run --with pytest pytest tests/ -q` → 75 passed (no regression)
4. Definition of done: **PASS** (all 3 required tests)
5. SECURITY: **confirmed (yes)** — proven by `test_no_credential_value_appears_in_logs` (dummy token absent from captured log stream incl. error path)
6. Concerns + handoff: `X_USER_ID` config requirement to document (§8.6); `Follow` shape + `store.list_sources(platform="x")` query-back; `DEPTH_CONFIG` for Sub-phase 3's budget (above)
