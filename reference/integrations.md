# Integrations — Orbit

**Why this doc exists:** every external surface Orbit touches, with its auth pattern, rate-limit posture, and failure mode. `/grab-issue` reads it before writing any code that calls out of process. All integrations are cookies-only or local — no OAuth, no Orbit server; the only stored credential is the Gmail app password in `.env`.

**When to update:** when an integration's auth recipe, endpoint, or rate-limit behavior changes, or when a new external surface is added.

## 1. YouTube — via `yt-dlp` (no API key)
- **Auth:** `yt-dlp --cookies-from-browser <chrome|firefox|safari|edge|brave>`. Reads the browser's logged-in YouTube session. No script beyond the wrapper.
- **Stage 0 (sources, weekly):** `yt-dlp --cookies-from-browser <b> --flat-playlist --dump-json https://www.youtube.com/feed/channels` → the subscription list. Cache it; subs barely change day to day.
- **Stage 1 (delta, per run):** per channel, list recent uploads; for new ones (not in `seen`), fetch the transcript as **VTT, retaining cue timestamps**. Cap transcription count by `depth` (`quick`/`default`/`deep`).
- **Deep-links:** `https://www.youtube.com/watch?v=<video_id>&t=<seconds>s` — built from retained VTT offsets / chapter start cues.
- **Rate limits / failure:** `yt-dlp` paces itself; the dominant failure is a **stale `yt-dlp` binary** (YouTube changes break extraction). Carry the last30days "stale yt-dlp" diagnostic nudge. Cookie failures surface as "No cookies found" (browser not logged in or DB locked).

## 2. X (Twitter) — vendored `bird-search` Node client (cookies-only GraphQL)
- **Auth recipe** (built exactly as `vendor/bird-search/lib/twitter-client-base.js`):
  - Resolve `auth_token` + `ct0`, priority: **CLI args → env `AUTH_TOKEN`/`CT0` → browser cookie store** (via `cookies.js` / the yt-dlp/sweet-cookie reader).
  - Headers: public web Bearer token; `x-csrf-token: <ct0>`; `x-twitter-auth-type: OAuth2Session`; `cookie: auth_token=…; ct0=…`; randomized client UUIDs (`clientUuid`, `clientDeviceId`); a desktop Chrome `User-Agent`.
- **Stage 0 (sources):** the `Following` GraphQL op, cursor-paginated, to enumerate accounts the user follows. (Confirm the vendored client exposes `Following`; if not, extend it — see master-plan open question 4.)
- **Stage 1 (delta):** per handle, `from:handle` **SearchTimeline** GraphQL op for recent tweets; cursor-paginated; rate-limit-aware. For large follow counts, **rotate which handles get deep-pulled** so coverage rotates over days.
- **queryId staleness:** X rotates GraphQL `queryId`s. The client resolves them live from the x.com JS bundle (`runtime-query-ids.js`) with baked-in fallbacks and **refreshes on 404**. Persistent 404 after refresh ⇒ bundle format changed; surface it.
- **Rate limits / failure:** cookie-based reads are **unofficial, ToS-gray**. Pace conservatively; honor `depth`; rotate handles. High volume risks X rate-limiting or flagging the account. Expired cookies ⇒ alert clearly, don't die silently. Revocation = user logs out of X in that browser.
- **Never transmit cookies** — read at runtime, used for local requests only, never logged or sent anywhere.

## 3. LLM — Anthropic / Claude via the host Claude Code/Codex session
- **No separate API key managed by Orbit.** Classification, chapterization, cluster-labeling, and summarization run through the Claude Code/Codex session that invokes the skill, so tokens count against the **user's own plan** (brief §7).
- **Usage discipline:** model is used only for judgment calls (Rule 5) — classify, label, summarize. Deterministic transforms (delta detection, scoring math, dedupe keys, deep-link URL building) stay in code.
- **Cost control:** `depth` (`quick|default|deep`) gates how many items get transcribed/deep-pulled and thus how much LLM work happens. Default to `default`; document a rough daily-cost estimate in the README.
- Prompt templates live in `skills/orbit/references/` as files, not inline (classify, label, chapterize, summarize).

## 4. Email delivery — Gmail SMTP with app password (M5, replaces iMessage)
- **Auth:** Gmail **app password** (requires 2FA enabled on the Google account) + sender address, both in `.env` (`GMAIL_APP_PASSWORD`, `ORBIT_EMAIL_FROM`); recipient in `delivery.email_to`. Never logged, never echoed.
- **Mechanism:** stdlib `smtplib` over SSL (`smtp.gmail.com:465`) + `email.mime`. Body = the delivery TL;DR as plain text/minimal HTML; the self-contained Tiles HTML page(s) ride as **attachments** — never inline (Gmail clips bodies at ~102KB, strips grid/flex/`@font-face`, and blocks base64 `data:` images; `file://` links are dead).
- **Failure posture:** unset recipient/password ⇒ skip with a clear log; auth rejection ⇒ surface "app password invalid / 2FA required" with `fix_suggestion`, no retry storm; transient SMTP error ⇒ one retry then surface. Delivery failure is loud but never crashes the pipeline or un-marks seen items.
- **Removed integrations (2026-07-18):** iMessage/AppleScript and WhatsApp/Twilio delivery are deleted from the pipeline and config schema — do not resurrect.

## 5. Claude artifact publish — chat/voice bridge (M7, spike-gated)
- **Mechanism:** the headless `claude -p "/orbit"` session publishes `digest.md` as a **private** claude.ai artifact; the HTML + email embed a `claude.ai/new?q=` prefilled-prompt link pointing at the artifact URL (fetch-on-open — links cannot upload files into a session).
- **Failure posture:** hard fail-soft — if publish fails or is unsupported headless, ship the digest/email without the link. Spike must prove headless publish before any dependent work.

## 6. Briefcast payload (optional)
- Emit the TL;DR + episode list as a Briefcast script payload (a file/format), not a live integration — no auth surface.

## Cross-cutting security posture (brief §4, §8)
- Cookies and credentials: **read at runtime, never logged, never transmitted, never stored off-machine.**
- Everything runs locally; no Orbit server exists. Machine off ⇒ nothing runs, nothing leaks.
- The README permissions table (§8.4) and honest risk disclosure (§8.5) are a first-class M4 deliverable, not boilerplate.
