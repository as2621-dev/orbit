# CSO findings — issue #7 (chat bridge + archive push)

Slice diff: scripts/lib/chat_bridge.py (new), scripts/lib/archive.py (new),
scripts/lib/deliver.py, scripts/lib/tiles.py (render_footer), scripts/lib/render.py
(page-1 footer wiring), scripts/orbit.py (stage-7 seams), README.md, tests.

## Critical / High
None.

## Medium / Low (logged, not fixed)

- **LOW — archive.py trusts `gh` stdout when composing follow-up endpoints.** The sha
  returned by `gh api ... --jq .object.sha` is interpolated into the next endpoint path
  (`repos/<repo>/git/commits/<sha>`). `gh` is the authenticated boundary itself and
  subprocess argv (no shell) means no injection into the local machine; a hostile value
  could only misroute a GitHub API call made with the owner's own token. Validating
  `^[0-9a-f]{40}$` would add a failure mode for zero realistic risk. Accepted.

## Verified-clean surfaces
- No secrets in code or fixtures (fake creds only; repo name is not a secret).
- Chat link: static prompt, fully percent-encoded (`quote(safe="")`), rendered through
  the `safe_href` allowlist + html escape — no XSS/attr-breakout surface.
- Subprocess: list argv, `stdin=DEVNULL`, never `shell=True`.
- Logging: no token/credential-shaped fields; `lib.log.redact` still wraps all fields;
  gh stderr excerpts truncated to 300 chars.
- Privacy guard: repo visibility verified before every push; non-private refuses loudly
  (tested).
- No new package dependencies (stdlib + the already-required `gh` CLI, fail-soft if absent).
