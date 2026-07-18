---
title: Email/MIME delivery gotchas for lib.deliver.deliver_email (stdlib smtplib/email)
tags: [email, smtp, mime, deliver_email, EmailMessage, attachments, gotcha]
problem_type: pattern
symptoms: "attachment byte-match assertions fail; a video title with a newline crashes the send; an unattended cron hangs forever; the 25MB cap lets an over-cap message through"
root_cause: "Several non-obvious stdlib email/smtplib behaviors, each of which bit during the M5 deliver_email slice (#2)."
date: 2026-07-18
---

`lib.deliver.deliver_email` (M5) sends the digest over Gmail SMTP with stdlib `smtplib` +
`email.message.EmailMessage`. Four non-obvious traps at that boundary — hit all of these when
touching email again (e.g. the M7 chat-link body). See also [[ruff-check-is-the-gate-not-ruff-format]].

1. **Attach raw BYTES, and read them back with `get_payload(decode=True)` — NOT
   `get_content()`.** `message.add_attachment(page_path.read_bytes(), maintype="text",
   subtype="html", filename=...)` preserves the file byte-for-byte (CTE base64). A test that
   asserts "attachment == file on disk" must use `attachment.get_payload(decode=True)` (returns
   `bytes`); `get_content()` returns a **decoded `str`** and normalizes line endings, so a
   byte-equality check against it fails. This is the assertion the happy-path + page-2 tests use.

2. **`EmailMessage` RAISES `ValueError` on a CR/LF in any header value.** Good news: it blocks
   header injection (you cannot smuggle a `Bcc:` via a crafted Subject). Bad news: the Subject is
   built from **external creator titles**, so a title containing a newline makes the *build* raise
   — and if the build is outside your failure guard, that crashes the whole pipeline. Fix applied:
   `_sanitize_header_value` collapses CR/LF to spaces on the Subject (defense-in-depth + crash
   guard), AND the whole build is wrapped so any residual `ValueError`/`OSError` is a logged,
   non-fatal refusal, not a crash. Read attachment bytes **inside** that same guard so a vanished
   render file (`OSError`) is non-fatal too.

3. **`smtplib.SMTP_SSL(host, port)` has NO socket timeout by default → hangs forever.** On a
   network black-hole an unattended cron run blocks indefinitely — neither loud nor non-fatal, and
   invisible to faked-transport tests. Bind the real default transport to a timeout:
   `functools.partial(smtplib.SMTP_SSL, timeout=30)`. A stall then raises `TimeoutError` (an
   `OSError`) the transient-retry path handles.

4. **The 25MB Gmail cap is on the ENCODED message, not raw attachment bytes.** base64 inflates
   attachments ~1.35×, so summing `read_bytes()` under-counts by ~35%. Check
   `len(message.as_bytes())` (what Gmail actually weighs), not the raw byte sum.

Pattern that made all four testable: the SMTP transport is an **injected boundary** — a keyword
`transport` defaulting to the real `SMTP_SSL` factory, faked in tests as a `(host, port) ->
connection` callable where **one factory call == one send attempt**. That lets tests pin the
retry posture by counting attempts (auth=1, transient-both-fail=2) without opening a socket —
the same seam style as `crontab_runner` / `llm_classifier` / the HTML `writer`.
