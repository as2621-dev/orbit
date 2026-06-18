# CSO + slop findings — Phase 4 (x-source-into-pipeline)

**Date:** 2026-06-18
**Scope:** the Phase 4 diff only (vendored bird-search lift + Following op, bird_x.py, classify/rerank/render/orbit wiring).

## CSO (security) — result: PASS, no critical/high, no medium/low open

This is the highest-risk phase (auth_token = full X account access). Every required invariant was verified against the phase diff:

- **Secrets in code/fixtures:** none. Test fixtures use obvious dummy values
  (`dummy_auth_token_THIS_MUST_NEVER_BE_LOGGED`, `dummy_ct0_THIS_MUST_NEVER_BE_LOGGED`).
  No real token, key, or connection string anywhere in the diff.
- **Cookies/ct0 read at runtime ONLY:** resolved via the vendored `cookies.js` recipe
  (CLI args → env `AUTH_TOKEN`/`CT0` → browser store) and the injected `_credentials`;
  passed to the Node subprocess via env (`_subprocess_env`), never as a CLI arg, never
  persisted to disk (`grep` for cookie/token disk writes: none).
- **Never logged:** `bird_x.py` passes no credential value to any `log_*` call; `lib/log.py`
  additionally auto-redacts any field keyed cookie/auth_token/ct0/token/secret/password/
  credential. A dedicated test (`test_no_credential_value_appears_in_logs`, Sub-phase 2)
  runs both the happy and auth-failure paths under `redirect_stdout` and asserts neither
  dummy token appears in the JSON log stream.
- **Never transmitted except x.com:** the new `twitter-client-following.js` mixin targets
  only `x.com` (via `TWITTER_API_BASE`); no other network egress in the diff.
- **Never in exception messages:** `XAuthError` and all raises name the failing var
  (`X_USER_ID`, `AUTH_TOKEN`/`CT0`) and point at README §8.6 troubleshooting — never the
  value. `grep` for `raise .*auth_token|ct0|cookie`: none.
- **Never committed:** no `.env`, no `*.sqlite`/`*.db`, no `node_modules/`, no cookie/cred
  file is staged; `__pycache__/` is gitignored. The vendored `bird-search/` dir carries
  only source + LICENSE + a zero-runtime-dependency `package.json` (no supply-chain surface).
- **Dependency additions:** none. Vendored `package.json` declares no runtime deps; Python
  uses stdlib only (subprocess/json/concurrent.futures).

## Slop scan — result: PASS, one accepted exception logged below

- No `TODO`/`FIXME`/`print(`/`localhost`/`console.log` debug leftovers in the new Python or
  the new JS mixin/CLI branch.
- No swallowed `except: pass`, no `# type: ignore`, no vacuous restate-the-code comments.
- The Node test mocks `globalThis.fetch` (no live X); fixtures are canned GraphQL JSON.

### Accepted exception (low severity) — `bird_x.py` is 756 lines

CLAUDE.md targets agent/tool files at < 500 lines. `bird_x.py` is 756 raw lines, but ~290
of those are the mandated Google-style docstrings (with examples); logic + blanks are ~466,
under the target. The file is already well-factored (12 single-responsibility functions +
the `Follow`/`Tweet` dataclasses), so a split now would add module surface for no clarity
gain (Rule 3 — don't refactor what isn't broken). **Action:** accept as-is this phase;
revisit if Phase 5/M3 grows `bird_x.py` further (a `bird_x_following.py` / `bird_x_delta.py`
split is the obvious seam if it does).
