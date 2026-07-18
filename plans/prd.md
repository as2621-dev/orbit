# PRD — Orbit

**Date:** 2026-07-18
**Source:** documents/product-brief.md (incl. §11 delta addendum, 2026-07-17/18 rulings)
**Status:** Ready for /to-issues
**Supersedes:** plans/master-plan.md (M1–M4 shipped; this doc is now the single technical source of truth)

## Problem Statement

A busy person follows ~40 YouTube channels and a few hundred X accounts. Checking feeds means opening two apps, scrolling past noise, and still missing the one video or thread that mattered. They want to open **one email each morning** and know everything worth their attention from the people they already follow — without handing credentials to a third-party service.

Today Orbit produces that digest and delivers it over iMessage with a `file://` link — which only works on the one Mac, and only if the machine was awake at 7am.

## Solution

Every morning, an email arrives: a short text summary of the day's top items, with the full "Tiles" newspaper-layout digest attached as a single self-contained HTML file that opens beautifully in any browser on any device. Everything runs on the user's own Mac — browser-cookie auth, subscription LLM, local SQLite — and if the Mac was asleep at 7am, the run fires on next wake and the email arrives then. Later, one link in the digest opens a Claude conversation with the digest loaded, for chat or voice.

### Shipped baseline (Phases 1–8, working — NOT to be re-sliced)

Source ingestion (YouTube subs via yt-dlp cookies, X follows via vendored bird client), delta fetch with SQLite `seen` state, LLM classification (signal/noise + category taxonomy, alpha/category gates), transcripts with cue timestamps + chapter deep-links, blurb + verdict summarization, density ranking (creator weights, baseline-relative engagement, quote down-weight, X top-8 cap), the Tiles HTML renderer (1–2 pages), the `/orbit --setup` wizard, cron auto-install, iMessage delivery, and single-skill plugin packaging.

This PRD covers the **delivery pivot + scheduling + fixes** on top of that baseline.

## Technical Foundation

### Tech stack

- **Frontend:** N/A — the only UI is the static, self-contained Tiles HTML the pipeline already renders. No re-render for email (Gmail's ~102KB clip, grid/flex/`@font-face` hostility, blocked `data:` images make inline email rendering a dead end).
- **Backend / data:** local SQLite (`store.py`, WAL) — unchanged. No server exists.
- **Agents:** none — LLM calls stay `claude -p` subprocess on the user's subscription (Rule 5: model for judgment calls only).
- **Jobs:** **launchd LaunchAgent** (`StartCalendarInterval` 07:00) replacing cron. Rationale: launchd runs a missed job on next wake; cron silently skips. No Trigger.dev — cookies and subscription LLM cannot leave the machine (documented deviation from global stack guidance).
- **Email transport:** **stdlib `smtplib` + `email.mime` over Gmail SMTP (SSL :465) with an app password** from `.env`. Rationale: zero new dependencies, no OAuth dance, credentials stay local. Runner-up flagged for revisit: Gmail API OAuth (needed only if app passwords are unavailable — they require 2FA enabled on the Google account).
- **Hosting:** none. Exception (M7 only): the `digest.md` twin is published as a private Claude artifact from the headless session — claude.ai hosts that one file.
- **Languages:** Python 3.12+ stdlib-first; Node 22+ only for the vendored X client — unchanged.

### Architecture

```
launchd (07:00, catch-up on wake)
      │
      ▼
claude -p "/orbit"  (headless, user's subscription)
      │
      ▼
scripts/orbit.py pipeline
  Stage 0  load sources (weekly)  ──  yt-dlp cookies / bird client
  Stage 1  delta fetch + classify (capped)
  Stage 2-6  transcripts → chapterize → cluster → trending → rank/tier
  Stage 7  render ── Tiles HTML (1-2 pages)  +  digest.md twin (M7)
      │                                            │
      │                                            ▼ (M7)
      │                                   publish artifact → chat link
      ▼                                            │
  deliver_email ◄──────────────────────────────────┘
  summary body + HTML attachment(s)
      │
      ▼
Gmail SMTP (app password, .env) ──► user's inbox, any device
```

### Key design decisions

1. **Summary email + HTML attachment, not inline HTML.** Sidesteps every Gmail rendering constraint; the digest renders in a real browser. Rules out: email-native Tiles re-render, `file://` links.
2. **iMessage and WhatsApp delivery are deleted, not deprecated.** One delivery path to maintain and test. Rules out: multi-channel delivery config in v1. Briefcast file-emit stays (no auth surface).
3. **launchd over cron.** `StartCalendarInterval` fires missed runs on wake; setup must migrate (remove) the existing orbit crontab entry. Rules out: server-side scheduling — cookie auth and subscription LLM are machine-bound (re-affirmed 2026-07-18).
4. **App-password SMTP over Gmail API.** Stdlib, no token refresh, one `.env` secret. Rules out (for now): OAuth flows, third-party senders (Resend/Twilio).
5. **Chat/voice bridge = fetch-on-open, not upload.** A link cannot upload files into a Claude session; the mechanism is a `claude.ai/new?q=` prefilled prompt pointing at the published artifact URL. Gated on a spike proving artifact publish works from the headless cron session. Rules out: "one-click with files pre-attached" claims.
6. **User category overrides are durable.** Wizard-confirmed per-channel categories persist to `sources` and must survive the weekly source refresh (upsert must not clobber user-set values).

### Module contracts

**Email delivery (`deliver_email`, replacing `deliver_imessage`)** — Responsibility: turn a finished digest (TL;DR summary + rendered HTML pages) into one sent email. Requirements: read recipient + app password from config/`.env`, never log or echo the credential; body is the existing delivery TL;DR as plain text/minimal HTML; attach page 1 and page 2 (when present); send over SMTP SSL; log structured success/failure with `fix_suggestion`; a delivery failure is loud but must not crash the pipeline or un-mark seen items. Edge cases: recipient or app password unset (skip with a clear log, not an exception); auth rejected (surface "app password invalid / 2FA required", no retry storm); transient SMTP failure (one retry, then surface); page 2 absent; attachment unexpectedly large (guard against Gmail's 25MB cap even though Tiles is ~318KB).

**Scheduler install (launchd, in setup wizard)** — Responsibility: install or replace the daily 7am LaunchAgent and retire the legacy cron entry. Requirements: idempotent by label (e.g. `com.orbit.daily`); plist under `~/Library/LaunchAgents`; loads/bootstraps immediately after write; removes any prior orbit-tagged crontab line; prints manual instructions instead of failing when `launchctl` is unavailable or sandboxed. Edge cases: agent already loaded (bootout/unload before re-bootstrap); prior cron entry present (migrate then remove); no prior scheduling at all; run fired while a previous run is still in flight.

**Setup wizard category persistence (bug fix)** — Responsibility: make the user's confirmed signal/noise flips durable. Requirements: confirmed categories are written to `sources` at wizard completion; weekly source refresh preserves user-set categories; re-running setup shows current (persisted) values as defaults. Edge cases: channel unsubscribed then re-subscribed; new channels default to `signal`; override exists but channel renamed upstream.

**Digest.md + chat link (M7)** — Responsibility: produce a markdown twin of the digest, publish it as a private Claude artifact, and embed a "Chat about this digest" link in the HTML and email body. Requirements: `digest.md` is self-contained text (no local file references); publish happens inside the headless session; the link is a `claude.ai/new?q=` prefilled prompt referencing the artifact URL; if publish fails, the digest and email still ship without the link (fail-soft). Edge cases: headless publish unsupported (spike gate — feature stays off), URL-encoding/length limits on the prefill, artifact visible only to the logged-in owner.

### Milestones

- **M5 — Email delivery:** iMessage + WhatsApp code deleted; `deliver_email` ships the summary + attachment; a real morning digest lands in the user's inbox and opens on a phone.
- **M6 — Wake-proof scheduling + wizard fix:** launchd agent installed by setup (cron migrated out); a run missed at 7am fires on wake; wizard-confirmed categories persist across refreshes.
- **M7 — Chat/voice bridge (spike-gated):** headless artifact publish proven; `digest.md` published; chat link in email + HTML opens a Claude session that loads the digest.

### Riskiest assumption + de-risk

The email experience ("open attachment") is pleasant enough to keep a daily-open habit — de-risked in M5 by dogfooding on a real inbox/phone before M6-M7 polish. The M7-specific risk (artifact publish may not work headless) is isolated behind its spike; failure leaves M5/M6 intact.

## User Stories

1. As a subscriber, I want the digest delivered to my email inbox each morning, so that I can read it on any device, not just the Mac that produced it.
2. As a subscriber, I want the email body to be a short summary (verdict + top items), so that I can triage from the inbox preview without opening anything.
3. As a subscriber, I want the full Tiles digest attached as one self-contained HTML file, so that it opens perfectly in a browser with fonts, thumbnails, and deep-links intact.
4. As a subscriber, I want page 2 attached alongside page 1 when it exists, so that the overflow content isn't stranded on the origin machine.
5. As a user, I want to configure my email recipient and Gmail app password once during setup (stored in `.env`), so that delivery works hands-off afterward.
6. As a user, I want a clear, actionable error when my app password is wrong or missing, so that I can fix auth without reading pipeline logs.
7. As a user, I want delivery failures to be loud in the log but non-fatal, so that a bad SMTP day never corrupts digest state or re-sends old items.
8. As a user, I want iMessage and WhatsApp delivery removed entirely, so that there is one delivery path to configure, permission, and debug.
9. As a user, I want the daily run scheduled via launchd instead of cron, so that a 7am run missed while my Mac slept fires on next wake and the email still arrives.
10. As a user, I want setup to migrate my existing orbit cron entry to the launchd agent automatically, so that upgrading doesn't leave two schedulers racing.
11. As a user, I want scheduler installation to be idempotent, so that re-running setup never duplicates agents or jobs.
12. As a user, I want a printed fallback (manual plist instructions) when launchctl is unavailable, so that setup degrades instead of failing.
13. As a user, I want the channel categories I confirm during setup to actually persist, so that my curation is honored on every subsequent run.
14. As a user, I want my persisted category overrides to survive the weekly source refresh, so that Orbit never silently reverts my choices.
15. As a returning user, I want re-running setup to show my previously confirmed categories as defaults, so that I adjust rather than redo.
16. As a subscriber (M7), I want a "Chat about this digest" link in the email and HTML, so that one tap opens a Claude conversation with today's digest loaded.
17. As a subscriber (M7), I want to use voice mode in that conversation, so that I can talk through my digest hands-free.
18. As a user (M7), I want the digest also produced as `digest.md` and published as a private artifact, so that a fresh Claude session can fetch it as context.
19. As a user (M7), I want the pipeline to ship the email without the chat link if artifact publishing fails, so that the bridge being down never blocks the digest.
20. As a plugin installer, I want the README's setup, permissions, and troubleshooting sections updated for email + launchd (and iMessage removed), so that onboarding matches reality.

## Implementation Decisions

- Delivery lives in the existing delivery module: `deliver_email` replaces `deliver_imessage`; the WhatsApp function and its config surface are deleted. The delivery TL;DR builder is reused as the email body.
- Delivery config moves to `delivery.email_to` (+ `GMAIL_APP_PASSWORD`, `ORBIT_EMAIL_FROM` in `.env`); `imessage_to` / `whatsapp_to` are removed from the config schema and example config.
- The SMTP client is injected the same way the pipeline injects other side-effectful boundaries (cron runner, LLM classifier), so tests fake the transport, never the business logic.
- The scheduler installer follows the existing cron-installer shape (generate entry → idempotent install → fail-soft print), swapping crontab for a plist + `launchctl`, plus a one-time cron-removal migration keyed on the existing orbit marker comment.
- Category persistence reuses the existing `sources` upsert; the refresh path gains "preserve user-set category" semantics (a persisted override wins over the default seeded on refresh).
- M7's artifact publish + prefill link is one module with a hard fail-soft boundary; nothing else may depend on its success.

## Testing Decisions

Mirror the existing pytest style: pure functions + injected fakes, tests pin behavior *counts and outcomes* (e.g. the existing test pinning LLM classify calls at the cap). Externals are always faked at the boundary: SMTP transport, `launchctl`/`crontab` runners, artifact publish. Priority intents: email built correctly (body from TL;DR, both attachments, no credential in headers/logs), delivery failure does not mark items seen or crash, scheduler install idempotency + cron migration, category override surviving refresh. No test may hit the network or a real Messages/SMTP session.

## Out of Scope

- Adjacent-creator suggestions / discovery of new creators (ruled out 2026-07-18).
- Server-side or cloud scheduling; any Orbit server; cookies or LLM keys leaving the machine.
- Email-native re-render of the Tiles layout; any HTML-in-body rendering work.
- iMessage and WhatsApp delivery (deleted, not maintained).
- Watch-history ranking, engagement-API scoring, multi-user hosted operation.

## Further Notes

- Old phase artifacts (`plans/phase-*.md`, progress files) and `plans/master-plan.md` are historical; do not slice from them.
- Known-but-unscheduled: `carryforward` resurfacing exists in `store.py` but is not wired into render — file as a candidate slice only if M5/M6 touch adjacent code.
- The uncommitted X classify cap work is already merged and pushed (`4bfd201`); the baseline includes it.
