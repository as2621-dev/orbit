# Phase 4 — Sub-phase 1: Extend vendored bird client with the `Following` op

## What was implemented
1. **Lift (Step 0):** Copied the entire reference `bird-search` vendor dir verbatim into the orbit tree (it did not exist before): `bird-search.mjs`, `package.json`, `LICENSE`, and all of `lib/`. No lifted file was modified except the three explicitly extended below.
2. **New mixin** `lib/twitter-client-following.js` — `withFollowing(Base)` adding `following(userId, {count|limit, cursor, pageDelayMs, maxPages})` + `getAllFollowing` + internal `followingPaged`. Mirrors `withSearch`/`searchPaged` exactly in shape: builds `buildFollowingFeatures()`, walks cursor pagination, POSTs `{features, queryId}` with `this.getHeaders()`, handles 404/`GRAPHQL_VALIDATION_FAILED` via the same refresh-on-404 (`fetchWithRefresh`) pattern.
3. **`bird-search.mjs`** — composed `withFollowing(withSearch(TwitterClientBase))`; added a `--following <userId>` (and `--following-user-id <id>`) CLI branch with `--count`/`--json`, mapping users to `{creator_handle, display_name, rest_id}`.
4. **`lib/twitter-client-base.js`** — additive `getFollowingQueryIds()` helper (primary from `getQueryId('Following')` + baked fallbacks `BEkNpEt5pNETESoqMsTEGA` and the query-ids.json value `mWYeougg_ocJS2Vr1Vt28w`). Analogous to the existing `getSearchTimelineQueryIds()`.
5. **Test** `test/following.test.mjs` (new `test/` dir) — `node:test` + `node:assert`, fully offline.

## Decisions
- **userId vs screen_name:** The Following GraphQL op requires a numeric `userId`. The search-only base client lifted here has **no** screen_name→id lookup (`getCurrentUser` is referenced in `ensureClientUserId` but the method is not present in this read-only build). Per the prompt's pragmatic guidance, `following(arg, ...)` treats `arg` as the userId **only when numeric**; a non-numeric arg returns a clear, non-success error: `"Following requires a numeric userId; resolve the screen_name to an id first (see --whoami)."` screen_name→id resolution is deferred to the Python wrapper (Sub-phase 2 / `--whoami`). The CLI accepts both `--following <id>` and `--following-user-id <id>`.
- **Following `variables` shape:** `{ userId: String(userId), count, includePromotedContent: false, [cursor] }`. `features` are also passed both as a URL query param (matching X's GraphQL convention) and in the POST body `{features, queryId}` (matching the search analog's body).
- **Instruction path parsed:** `data.data.user.result.timeline.timeline.instructions`, with graceful fallbacks to `...result.timeline_v2.timeline.instructions` and `...result.timeline.instructions`. User records extracted via the existing **`parseUsersFromInstructions`** (NOT `parseTweetsFromInstructions` — the Following timeline returns User entries). Cursor via existing `extractCursorFromInstructions(instructions)` (Bottom cursor).
- **Cursor-walk approach: inline while-loop**, not `paginateCursor`. Reason: `searchPaged` (the explicit template) inlines its own loop with `seen`-dedup and the `added===0`/same-cursor termination guards; inlining is the cleaner mirror and lets me reuse the identical `fetchWithRefresh` structure. The inter-page delay is still honored: `if (pagesFetched > 0 && pageDelayMs > 0) await this.sleep(pageDelayMs)` (default 1000ms, matching `paginate-cursor.js`).

## Files created/modified
- NEW `skills/orbit/scripts/lib/vendor/bird-search/` (entire dir lifted)
- NEW `skills/orbit/scripts/lib/vendor/bird-search/lib/twitter-client-following.js`
- NEW `skills/orbit/scripts/lib/vendor/bird-search/test/following.test.mjs`
- MOD `skills/orbit/scripts/lib/vendor/bird-search/bird-search.mjs`
- MOD `skills/orbit/scripts/lib/vendor/bird-search/lib/twitter-client-base.js`

## Divergences from the plan
- Plan signature is `following(screenName, {count})`. Implemented as `following(userId, {count|limit, ...})` treating the arg as the id (numeric) — the documented userId decision above. The CLI flag remains `--following <screen_name>` as specified but currently requires a numeric value (or `--following-user-id`); screen_name resolution is Sub-phase 2's job. Non-blocking, surfaced here per the prompt.
- `--count` defaults to unbounded (`Infinity`) for `--following` so the full follow list paginates by default; pass `--count N` to cap. (Search keeps its 20 default.)

## Code-review findings + fixes
- **[low] features in both URL param and body** — kept both: URL param matches X's real GraphQL request convention for Following; body `{features, queryId}` matches the search analog. No fix needed; harmless redundancy that maximizes compatibility.
- **[low] partial-page failure** — mirrored `paginateCursor`: if a later page fails but users were already collected, returns `{success:false, error, users, nextCursor}` so the caller can decide. Matches existing vendor behavior.
- **[info] no `seen` resurrection across cursor loops** — dedup by `user.id` via `seen` Set, identical to `searchPaged`.
- No critical/high findings.

## Validation (Node v25.5.0)
- `node --check` on `twitter-client-following.js`, `twitter-client-base.js`, `bird-search.mjs` → all pass.
- `node -e "import('.../twitter-client-following.js')..."` → `import ok, withFollowing: function`.
- CLI flag recognized: `node bird-search.mjs` → search usage; `node bird-search.mjs --following` → `Usage: ... --following <userId> ...` (the new branch is reached, distinct usage line). PASS.
- Test: `NODE_ENV=test node --test "skills/orbit/scripts/lib/vendor/bird-search/test/following.test.mjs"` → **2 tests, 2 pass, 0 fail** (5 grouped DoD assertions in test 1 + the non-numeric guard in test 2). The glob form `".../test/**/*.test.mjs"` also runs. NOTE: on Node 25 `node --test <dir>` (bare dir) errors with MODULE_NOT_FOUND — must pass a file path or glob; documented here. No fallback to a plain script was needed.

## Definition of done — PASS
- **(a) parses creator_handle/username:** asserts handles `['alice','bob','carol','dave']` parsed out of canned user entries; `users[0]` has `name:'Alice'`, `id:'1001'`. PASS.
- **(b) follows cursor across BOTH pages:** asserts `fetch` called exactly twice, page-2 cursor (`CURSOR_PAGE_2`) carried in the 2nd request's variables, and union (4) > page-1-only count (2) with carol+dave present — a single page would silently truncate. PASS.
- **(c) request shape:** asserts URL contains `/Following` + a baked Following queryId, POST body has `features`(object)+`queryId`(string), Following feature `responsive_web_graphql_timeline_navigation_enabled===true`, and headers `x-csrf-token===ct0`, `authorization` starts `Bearer `, `x-twitter-auth-type===OAuth2Session`. PASS.
- queryId resolution: `NODE_ENV=test` skips live refresh; baked fallback used. No live X call.

## SECURITY — confirmed
- Grepped the touched/new files for any log/print/throw of a credential value (`grep -nE "console.(log|error)|process.std|writeStdout|writeStderr|throw" ... | grep -i "authToken|ct0|cookie|csrf|token|authorization|bearer"`): the **only** hit is the pre-existing lifted base-file `throw new Error('Both authToken and ct0 cookies are required')` — that throws the credential *names*, never values. My new mixin and CLI branch never log, print, or throw a cookie / auth_token / ct0 / authorization / csrf **value**. The cookie header is built only into request headers via the existing `getHeaders()`.
- Fixtures contain **no real tokens** — only dummy `{authToken:'x', ct0:'y'}`. Confirmed by grep. YES.

## Concerns for the orchestrator (Sub-phase 2 inputs)
- **`--following ... --json` output shape:** a JSON array of `Follow` records:
  ```json
  [{"creator_handle": "alice", "display_name": "Alice", "rest_id": "1001"}, ...]
  ```
  On error: `{"error": "<msg>", "items": []}`. The Python wrapper should parse the array; a top-level object with `error` signals failure (e.g. auth failure / non-numeric id).
- **`Follow` record fields available:** `creator_handle` (= username/screen_name), `display_name` (= name), `rest_id` (= numeric user id string). The underlying user object also carries description/followersCount/followingCount/isBlueVerified/profileImageUrl if Sub-phase 2 wants more (currently dropped in the CLI mapping).
- **userId resolution is Sub-phase 2's responsibility:** the CLI currently needs a numeric id. Sub-phase 2 must resolve the logged-in user's screen_name→rest_id (the prompt names `--whoami` as the supplier). The base lifted here has no `getCurrentUser`; if Sub-phase 2 needs an in-Node lookup it must add one or resolve via another path. The mixin returns an actionable error for a non-numeric arg rather than failing silently.
- **Pacing:** `following` honors `pageDelayMs` (default 1000ms) between pages — relevant to Sub-phase 3's ToS-gray pacing posture.
