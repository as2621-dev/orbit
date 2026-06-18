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

Then run `/orbit --setup` once (see §8.3) and add the cron line it prints. Three things must
be in place first: **Python 3.12+**, **Node 22+**, and **`yt-dlp`** on your `PATH`, plus a
browser logged into YouTube and X (see §8.2). The repo is private by default — the marketplace
command works because you're authenticated to your own account; make it public when you want to
share it.

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

What to expect: it runs on **your own machine**, on a schedule **you** set (a daily cron
entry). It reads your feeds using your existing browser logins. Nothing is sent to any Orbit
server, because there isn't one.

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

4. **Set your delivery target and schedule.** Still inside the wizard, you choose where the
   HTML is written (`delivery.html_path`), optionally an iMessage number to receive the TL;DR
   (`delivery.imessage_to` — leave it unset to skip iMessage), and the cron schedule (when
   Orbit should run, e.g. `0 7 * * *` for 7am daily).

5. **Add the cron entry it prints.** When the wizard finishes it writes a validated
   `orbit.config.json` and prints an exact crontab line. Copy it into your crontab
   (`crontab -e`). It looks like:

   ```
   0 7 * * * cd /path/to/orbit && claude -p "/orbit"
   ```

   That is the whole scheduler: a plain OS cron entry on your own machine running
   `claude -p "/orbit"`. It runs locally so it can read your browser cookies and (on macOS)
   fire iMessage — without sending any credential anywhere. Cloud schedulers can't read local
   browser cookies, so Orbit does not use them.

Config reference: see `orbit.config.example.json` (a documented template of every field) and
`.env.example` (placeholders for env-based cookies and the optional WhatsApp/Twilio
credentials — copy it to `.env`, which is gitignored, and never commit real values).

---

## 8.4 Permissions — what we ask and why

Orbit asks for five things. For each: why it is needed, and what we do / don't do with it.

| Permission | Why we need it | What we do / don't do |
|---|---|---|
| **Read browser cookies** (YouTube, X) | In cookie mode there is no API; we authenticate **as you** to read your own subscriptions and following. | Read at runtime only. Never logged, never transmitted, never stored outside your machine. |
| **Filesystem write** | To save the HTML digest, the page-2 overflow file, and the local SQLite state database. | Writes only to the paths you configure (`delivery.html_path` and the local state DB). Nothing else. |
| **Network access** (youtube.com, x.com) | To fetch new videos, transcripts, and tweets from the people you follow. | Talks only to YouTube and X, plus the LLM endpoint used for summarizing. Cookies are never sent to anyone but YouTube/X. |
| **Run AppleScript** (macOS, optional) | To deliver the digest to iMessage. | Only if you set an iMessage target (`delivery.imessage_to`). Skipped entirely otherwise. Requires you to grant macOS Automation permission the first time. |
| **LLM usage** (your plan) | To classify, cluster, chapterize, and summarize items. | Runs on your own Claude Code / Codex usage, controlled by `depth`. Orbit manages no separate API key. |

> macOS note: the first time Orbit sends to iMessage, macOS will prompt you to allow the
> controlling process to control the Messages app (System Settings → Privacy & Security →
> Automation). You must approve it once. If you never set an iMessage target, this never
> happens.

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

- **Revocation is simple.** Log out of X / YouTube in that browser and the cookies invalidate
  immediately. There is no separate Orbit access to revoke — pulling the browser login is the
  whole kill switch.

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
