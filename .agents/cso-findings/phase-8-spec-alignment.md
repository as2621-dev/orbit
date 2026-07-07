# CSO findings — phase-8-spec-alignment

Scope: the full Phase 8 diff (clickable timestamps, long-form/category gating, X
virality selection, cron auto-install). No critical or high findings; nothing blocked
the commit.

## Surfaces reviewed
- **HTML href injection (new chip anchors):** chip URLs gate on `is_safe_link_url` and
  escape via `safe_href`; a `javascript:` chapter link degrades to an inert `<span>`
  (pinned by `test_chapter_chip_with_javascript_url_is_neutralized_to_span`). PASS.
- **Subprocess (crontab install):** `_default_crontab_runner` uses an argv list (never
  `shell=True`), a 15s timeout, and refuses to overwrite a crontab it failed to read.
  Tests drive all crontab I/O through the injected runner. PASS.
- **Model-output trust (category axis):** the parsed category is coerced against the
  fixed five-value allowlist; anything else falls open to a keep-sentinel — the model
  cannot inject an arbitrary label into gating logic. PASS.
- **Secrets/logging:** no secret, token, or `.env` value is read or logged by the new
  code; the cron line logged is repo path + `claude -p "/orbit"` only. PASS.
- **Dependencies:** none added (stdlib `subprocess` + existing `lib.subproc`). PASS.

## Low (logged, not fixed)
1. **`render._card_deep_link` can return an unsanitized first-chapter `deep_link`.**
   Safe today: every render-time consumer (title link, chip link, more-chapters link)
   re-validates via `safe_href`, and `Chapter.deep_link` is code-constructed from real
   cue offsets. Risk only materializes if a FUTURE caller uses the raw return in
   markup without `safe_href`. Fix if it recurs: sanitize at the source or add a
   docstring warning on `_card_deep_link`. (Render-time `safe_href` remains the
   authoritative boundary; double-sanitizing now would blur where the guarantee lives.)
