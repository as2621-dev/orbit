# Orbit — onboarding & permissions

Orbit is a personal daily intelligence digest of your own YouTube subscriptions and X
following. This document is the full setup, permissions, risk, and troubleshooting guide.
It is honest and plain. Read §8.5 in particular — it is not softened.

---

## Install (quick reference)

**Claude Code (recommended):**

```
/plugin marketplace add as2621-dev/orbit
/plugin install orbit@orbit
```

**Manual / developer (install the local clone as a plugin):**

Orbit is a **single-skill plugin** — the repo root *is* the skill (a `SKILL.md` lives at the
root, with no `skills/` subdirectory). So you install the clone itself as the plugin, not a
nested directory. Add the local checkout as a marketplace and install from it:

```bash
git clone https://github.com/as2621-dev/orbit.git
# Inside Claude Code, point the marketplace at the local clone, then install:
#   /plugin marketplace add ./orbit
#   /plugin install orbit@orbit
```

Because the plugin is the single root `SKILL.md`, it installs as the bare `/orbit` command
(not `/orbit:orbit`).

Then run `/orbit --setup` once (see §8.3); it installs your daily launchd agent for you (no
manual crontab step). Three things must be in place first: **Python 3.12+**, **Node 22+**, and
**`yt-dlp`** on your `PATH`, plus a browser logged into YouTube and X (see §8.2). The repo is
private by default — the marketplace command works because you're authenticated to your own
account; make it public when you want to share it.

---

## 8.1 What Orbit does & what to expect

Orbit produces **one HTML page per day** that summarizes the new items from the YouTube
channels you subscribe to and the X accounts you follow. The page is organized by **creator
and topic**, ranks items by signal, and gives you **one-click deep-links** into the exact
moment of a video (`watch?v=ID&t=Ns`) and into individual tweets.

At the very top of the page there is a **one-line TL;DR** that tells you, in one sentence,
whether the day is worth reading further. Below it: the day's scoops, what's trending across
the people you follow, the top long-form episodes with chapter lists, and a bottom "they also
posted" strip for everything that did not clear the bar. When a day overflows, Compact and
Index items spill to a linked page 2 (capped at 2 pages/day).

What to expect: it runs on **your own machine**, on a daily schedule — a launchd agent at 7am.
If the Mac was asleep at 7am, the run fires on the **next wake**, so you don't lose a day. It
reads your feeds using your existing browser logins and **emails you the digest**: a one-line
summary as the body, the full HTML page(s) attached so they open in any browser on any device.
Nothing is sent to any Orbit server, because there isn't one — the only place your digest goes
is your own inbox.

---

## 8.2 Prerequisites

- **Python 3.12+.** (The product brief says 3.11+, but this codebase is pinned to 3.12+ —
  use 3.12 or newer to match what Orbit is actually built and tested against.)
- **Node 22+** — required for the X client (the vendored `bird-search` GraphQL client).
- **`yt-dlp`** installed and on your `PATH` — Orbit shells out to it for YouTube.
- **Logged into YouTube and X** in a supported browser: **Chrome, Firefox, Safari, Edge, or
  Brave**. Orbit reads those sessions' cookies to authenticate as you.
  - Alternative: instead of a browser login for X, you can paste cookies into `.env`
    (`AUTH_TOKEN` / `CT0`) and set `"cookie_source": "env"` in your config.
- **A Gmail account with 2-Step Verification (2FA) and an app password** — only if you want the
  digest emailed to you (the default, recommended delivery). Orbit sends over Gmail SMTP using
  an **app password**, never your normal password. §8.3 step 4 covers minting one. Skip this
  only if you're happy reading the digest as a local HTML file instead.

---

## 8.3 Setup (5 steps)

1. **Install the plugin** (see the Install quick-reference above):

   ```
   /plugin marketplace add as2621-dev/orbit
   /plugin install orbit@orbit
   ```

   (The manifest is `.claude-plugin/marketplace.json`.)

2. **Confirm you're logged in.** Open your browser and make sure you are logged into both
   **YouTube** and **X**. Orbit reads those sessions; it cannot read what you are not logged
   into.

3. **Run the setup wizard:**

   ```bash
   /orbit --setup
   ```

   It reads your YouTube subscriptions and X follows, **auto-classifies** each channel into
   signal/noise from recent titles, then asks you to **confirm the categories** and **pick a
   few priority creators** (these become `creator_weights`). It also seeds your `interests`
   from your subscriptions.

4. **Set up email delivery.** Still inside the wizard, you choose where the HTML is written
   (`delivery.html_path`) and the address the digest is emailed to (`delivery.email_to` —
   leave the email prompt blank to skip email and just write the HTML locally). To actually
   send mail, add the sender credentials to `.env` (gitignored):

   ```
   ORBIT_EMAIL_FROM=you@gmail.com
   GMAIL_APP_PASSWORD=your-16-char-app-password
   ```

   `GMAIL_APP_PASSWORD` is a Gmail **app password**, not your normal password. App passwords
   require **2-Step Verification (2FA)** enabled on the Google account; mint one at
   <https://myaccount.google.com/apppasswords>. Google shows it as four space-separated groups —
   enter the 16 characters **without the spaces**. Orbit reads both from `.env` at send time and
   never logs, echoes, or stores them. The recipient (`delivery.email_to`) lives in
   `orbit.config.json`, not `.env`. The schedule is **not** asked — it is fixed at 7am daily
   (installed in step 5).

5. **Nothing to paste — the wizard installs your scheduler.** When the wizard finishes it
   writes a validated `orbit.config.json` and installs a **launchd LaunchAgent**
   (`com.orbit.daily`, at `~/Library/LaunchAgents/com.orbit.daily.plist`) scheduled for **7am
   daily**. Unlike cron, launchd runs a **missed 7am run on the next wake** — if your Mac was
   asleep at 7am, the digest still arrives once it wakes (cron silently skips it; that
   wake-catch-up is the whole reason Orbit uses launchd, not cron). Setup also **retires any
   old `# orbit-daily-digest` crontab line** from a previous version, so you never end up with
   two schedulers racing. If `launchctl` is unavailable (e.g. a sandboxed shell), setup prints
   the plist and the `launchctl bootstrap` command for you to run by hand, and still completes.

   The scheduled run executes the pipeline directly — `python3 scripts/orbit.py` with the
   repo as its working directory — and appends stdout/stderr to
   `~/Library/Logs/orbit.daily.log`, so a failed headless run always leaves a trace. (It
   deliberately does NOT go through `claude -p "/orbit"`: that wrapper ran the plugin-cache
   copy of the pipeline — stale until a plugin version bump — and, being a one-shot session,
   exited before a long run finished, killing it. The pipeline's own LLM stages still call
   `claude -p` per prompt; those short calls need no permission bypass.)

Config reference: see `orbit.config.example.json` (a documented template of every field) and
`.env.example` (placeholders for env-based X cookies and the Gmail email-delivery credentials —
copy it to `.env`, which is gitignored, and never commit real values).

---

## 8.4 Permissions — what we ask and why

Orbit asks for six things. For each: why it is needed, and what we do / don't do with it.

| Permission | Why we need it | What we do / don't do |
|---|---|---|
| **Read browser cookies** (YouTube, X) | In cookie mode there is no API; we authenticate **as you** to read your own subscriptions and following. | Read at runtime only. Never logged, never transmitted, never stored outside your machine. |
| **Filesystem write** | To save the HTML digest, the page-2 overflow file, the local SQLite state database, and the launchd agent plist. | Writes the digest to the paths you configure (`delivery.html_path` and the local state DB) and the agent plist to `~/Library/LaunchAgents`. Nothing else. |
| **Network access** (youtube.com, x.com) | To fetch new videos, transcripts, and tweets from the people you follow. | Talks only to YouTube and X, plus the LLM endpoint used for summarizing. Cookies are never sent to anyone but YouTube/X. |
| **Send email** (Gmail SMTP, optional) | To deliver the digest to your inbox so you can read it on any device, not just this Mac. | Only if you set `delivery.email_to` plus `ORBIT_EMAIL_FROM` + `GMAIL_APP_PASSWORD` in `.env`. Connects to `smtp.gmail.com` to send the summary + attached HTML. The app password is read at send time, passed straight to SMTP login, and never logged, echoed, or written into a header. Skipped entirely if unconfigured. |
| **LLM usage** (your plan) | To classify, cluster, chapterize, and summarize items. | Runs on your own Claude Code / Codex usage, controlled by `depth`. Orbit manages no separate API key. |
| **Schedule a headless daily run** (launchd) | To run Orbit unattended at 7am, with catch-up on wake. | Installs a launchd agent (`com.orbit.daily`) that runs `python3 scripts/orbit.py` directly from the repo and appends its output to `~/Library/Logs/orbit.daily.log`. Remove the agent with `launchctl bootout gui/$(id -u)/com.orbit.daily`. |

> Note: email delivery is opt-in. If you leave `delivery.email_to` (or the two `.env`
> credentials) unset, Orbit skips the send entirely and just writes the HTML digest to disk —
> the "Send email" permission is never exercised.

---

## 8.5 Honest risk disclosure (do not soften)

Read this. It is the truth about what you are running.

- **`auth_token` is full account access.** Your X `auth_token` cookie is, in effect, your
  whole account. **Treat your cookies like a password.** Orbit keeps them local, but you are
  pasting/holding sensitive credentials, and anyone who gets them gets your account.

- **Reading X via session cookies is an unofficial, ToS-gray method.** It is not the
  sanctioned API. At high volume, X may rate-limit or flag the account. Orbit paces requests
  conservatively, but **use a sane `depth` and don't point it at thousands of handles
  aggressively.** This is your account and your risk.

- **The Gmail app password is a second stored credential.** If you enable email delivery,
  `GMAIL_APP_PASSWORD` sits in your local `.env`. It authorizes mail access to that Gmail
  account (not your full Google account), bypasses 2FA for that one use, and is independently
  revocable at <https://myaccount.google.com/apppasswords> without touching your Google
  password. Orbit reads it only to log in to Gmail's SMTP server and never logs, echoes, or
  transmits it anywhere else — but it is still a secret in a file, so keep `.env` gitignored
  (it is) and don't share it.

- **Revocation is simple.** Log out of X / YouTube in that browser and the cookies invalidate
  immediately. There is no separate Orbit access to revoke — pulling the browser login is the
  whole kill switch. The email app password is revoked separately, at the app-passwords page
  above.

- **Everything is local. No Orbit server exists.** If your machine is off, nothing runs and
  nothing leaks. There is no cloud component, no telemetry, no account on our side, because
  there is no "our side."

---

## 8.6 Troubleshooting

- **X returns 404.** This is almost always a stale GraphQL `queryId` — X rotates them. Orbit
  resolves them live from the x.com JS bundle and auto-refreshes on a 404. If it **persists**
  after the refresh, the bundle format changed and the client needs updating.

- **"No cookies found".** Either you are not actually logged into that browser, or the browser
  is **locking its cookie database** while it's open. Close the browser and retry.

- **Cookies expired.** Re-log-in to the affected site in your browser. Orbit **alerts on auth
  failure** rather than dying silently, so you'll know which credential went stale.

- **Rate-limited on X.** **Lower your `depth`** (e.g. `deep` → `default` → `quick`) or reduce
  the number of deep-pulled handles. Cookie-based reads are unofficial; pacing conservatively
  is the fix.

- **App password rejected / "2FA required".** Gmail refused the SMTP login. Two causes: (1) the
  sender account has no **2-Step Verification (2FA)** — app passwords cannot be minted without
  it, so enable 2FA first, then generate a fresh one at
  <https://myaccount.google.com/apppasswords>; (2) the app password in `.env` is wrong or was
  revoked — paste a fresh one into `GMAIL_APP_PASSWORD`. Orbit does **not** retry a rejected
  password (no retry storm); it logs the exact remedy and finishes the run.

- **Email not arriving.** Check the log first. If delivery was *skipped*, one of
  `delivery.email_to`, `ORBIT_EMAIL_FROM`, or `GMAIL_APP_PASSWORD` is unset — Orbit names the
  missing field, never its value. If the send was *attempted but failed*, check your network and
  <https://www.google.com/appsstatus>, then re-run — today's items stay marked seen, so nothing
  double-sends. Also check spam and confirm `delivery.email_to` is the address you expect. The
  digest is written to `delivery.html_path` regardless, so you can always open it locally.

- **launchd agent not firing (no digest at 7am).** Confirm the agent is loaded:

  ```bash
  launchctl print gui/$(id -u)/com.orbit.daily
  ```

  If that reports "Could not find service", the agent is not installed — re-run `/orbit --setup`
  (or load it by hand with
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orbit.daily.plist`). Remember
  launchd fires a **missed** 7am run on the next wake, so if the Mac was asleep the run appears
  only after it wakes, not at a strict 07:00.

---

## Cost & usage (rough estimates)

Orbit runs on **your own** Claude Code / Codex plan. The LLM work (classify, label,
chapterize, summarize, cluster) counts against **your** limits. The `depth` setting is the
throttle: it controls how many items get transcribed and deep-pulled, which is where almost
all the token cost goes.

The figures below are **rough first-cut estimates** from per-stage token assumptions, not
measured numbers. Treat them as order-of-magnitude only and refine them after your own real
runs. Actual cost depends heavily on how many channels/handles you follow and how much new
content there is on a given day.

| `depth` | What it does | Rough daily cost (estimate) |
|---|---|---|
| `quick` | Skips most transcription; classifies titles/metadata, summarizes lightly. | ~$0.05–0.20/day |
| `default` | Transcribes and chapterizes a capped set of top-ranked items. | ~$0.20–0.75/day |
| `deep` | Transcribes everything new; fullest clustering and chapterization. | ~$0.75–3.00+/day |

**Recommendation: start with `default`.** It gives a useful digest without transcribing
everything, and you can move to `deep` (or down to `quick`) once you've seen a few real runs
and know your own volume.
