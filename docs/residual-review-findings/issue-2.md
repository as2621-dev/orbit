# Residual review findings — issue #2 (deliver_email)

Deferred (non-blocking) findings from the multi-agent review panel on the M5 email-delivery
slice. Every **critical/high/medium** finding with a concrete fix was **applied in the slice
commit** (build/read failure guard, encoded-size 25MB cap, SMTP socket timeout, empty-pages
skip, test-cruft cleanup). The items below are advisory or cross-slice — recorded here for a
human call rather than fixed in this slice.

## Deferred

- **`build_message_body` is now orphaned (low, simplicity).** Slice #1 added
  `lib.deliver.build_message_body` (summary + link) anticipating the email body would reuse
  it. This slice did **not** — the M5 email body is the TL;DR summary with no `file://` link
  (dead cross-device), so `_build_email_message` composes its own body. The helper + its two
  tests (`test_build_message_body_*`) are now unused by production.
  **Deferred, not deleted (Rule 3 — surgical):** it is pre-existing, tested code plausibly
  reused by the **M7** "Chat about this digest" link (a summary + `claude.ai/new?q=` link
  body). Decision for M7: either wire it into the M7 body or delete it then.

- **`log.redact()` does not recurse into list/tuple values (low, informational).**
  `scripts/lib/log.py:62` recurses into `dict` values but not `list`/`tuple`, so a secret
  nested inside a logged list would pass through unredacted. **Not triggered by this slice**
  (the only list logged, `missing_config`, holds field *names*, never values; the app
  password is never routed to a log field at all). Flagged as a latent gap in the shared
  logging defense layer — a candidate hardening for whoever next touches `log.py`.

## Informational — human sign-off (no code change)

- **First outbound network egress + first handled credential in the pipeline.** This slice
  adds Orbit's first SMTP send and first secret handling. The security lens confirmed the
  hard rule holds (app password read from env, passed only to `login`, never logged / never a
  header; auth-failure logs `smtp_code` only; skip log lists field names only; Subject CR/LF
  sanitized). Nothing to fix — noted because a new egress/credential surface warrants a human
  eyeball regardless of green tests.

- **A full-day SMTP outage means no email that day (by design).** `mark_seen` is committed in
  Stage 1; delivery is Stage 7 and holds no store handle, so a send failure never re-marks or
  un-marks `seen` (a test spies on `orbit.store` and asserts zero calls). The intended
  consequence: on a total outage the items are already `seen`, so nothing re-sends and the
  user simply gets no email that day (the digest still sits on disk at `written_paths`). This
  matches PRD story #7 ("never corrupts digest state or re-sends old items"); recorded for
  sign-off since the only failure signal is a log line an unattended-cron user may not read.
