# Orbit

Orbit is a personal daily intelligence digest of your own YouTube subscriptions and X
following. It pulls what's new, ranks it by signal, and renders a single HTML one-pager —
so you open one digest instead of ten feeds.

**Full onboarding, the permissions and why we ask for them, the honest risk disclosure,
troubleshooting, and a cost estimate live in [`SETUP.md`](SETUP.md).**
Read that before running Orbit.

## What's shipped

- **YouTube half** — subscriptions → delta uploads (long-form only, ≥10 min) → transcripts
  (with timestamps) → chapterize → classify (signal/on-topic + a fixed
  `{ai, business, tech, sports, other}` category, `other` dropped) → derank → density-laddered
  HTML with clickable `watch?v=ID&t=Ns` chapter deep-links and page-2 overflow.
- **X half** — following → per-handle timelines → the same classify/rank path, then a **top-8
  by virality** cut (quote-tweets down-weighted) into the digest.
- **Overlap, trending & scoop passes** — merge short-form reactions, cross-link long-form on a
  shared topic, detect internal/external trending, and flag dormant-account scoops.
- **Delivery & onboarding** — the digest emailed to you (Gmail SMTP: a summary body + the
  self-contained HTML page(s) attached), the `/orbit --setup` wizard, and a wake-proof
  **launchd** agent the wizard installs for you (a run missed at 7am fires on next wake).

## Quick start

1. Install the plugin in Claude Code (manifest: `.claude-plugin/marketplace.json`):

   ```
   /plugin marketplace add as2621-dev/orbit
   /plugin install orbit@orbit
   ```

   Prerequisites: **Python 3.12+**, **Node 22+**, and **`yt-dlp`** on your `PATH`.
2. Confirm you're logged into YouTube and X in a supported browser
   (Chrome/Firefox/Safari/Edge/Brave).
3. Run `/orbit --setup` — it reads your subs/follows, auto-classifies channels, has you
   confirm categories and pick priority creators, seeds interests, and sets your email
   delivery target. It writes a validated `orbit.config.json` and **installs a wake-proof
   launchd agent for you** (`com.orbit.daily`) at a fixed **7am**, idempotent by label so
   re-running setup replaces it rather than duplicating. Unlike cron, launchd runs a **missed
   7am run on the next wake**. If `launchctl` is unavailable (e.g. a sandboxed shell), setup
   still completes and prints the plist + `launchctl bootstrap` command to install by hand.

   The schedule is fixed (not a prompt). The scheduled job runs the pipeline directly —
   `python3 scripts/orbit.py` from the repo — and appends its output to
   `~/Library/Logs/orbit.daily.log` (see [`SETUP.md`](SETUP.md) §8.3 step 5 and §8.4).

The full 5-step setup is in [`SETUP.md`](SETUP.md) §8.3.

## Delivery

**Email is the delivery channel.** Each morning Orbit emails you the digest over Gmail SMTP
under the searchable subject `Orbit Digest — YYYY-MM-DD: <TL;DR>`: the body is the TL;DR, a
**"Chat about this digest"** link, and the full digest markdown; the self-contained Tiles HTML
page(s) ride as attachments so they open in a real browser on any device. The chat link opens
a prefilled claude.ai conversation that reads the digest out of that very email via your Gmail
connector — one tap from inbox to a chat (or voice) session about today's items. Delivery is
opt-in — set `delivery.email_to` in the config and `ORBIT_EMAIL_FROM` + `GMAIL_APP_PASSWORD`
(a Gmail app password; 2FA required) in `.env`. Leave them unset and Orbit just writes the
HTML locally. The earlier local-only delivery channels were removed (2026-07-18) in favor of
one email path. See [`SETUP.md`](SETUP.md) §8.3-§8.4.

**Archive.** After each render, `digest.md` + the Tiles HTML are also pushed (one commit per
run, fail-soft) to the private `as2621-dev/orbit-digests` repo under `YYYY/MM/DD/`, via the
`gh` CLI. Every run first verifies the repo is still private; any push failure only costs the
archive — the email still sends.

## Configuration & environment

- **Config:** copy `orbit.config.example.json` to `orbit.config.json` (the `--setup` wizard
  writes this for you). It documents every field: `cookie_source`, `creator_weights`,
  `interests`, `depth` (`quick`/`default`/`deep`), `delivery` (`html_path` + `email_to`), and
  `schedule`.
- **Environment:** copy `.env.example` to `.env` (gitignored). It carries placeholders for
  env-based X cookies (`AUTH_TOKEN`/`CT0`, only used with `cookie_source: "env"`) and the Gmail
  email-delivery credentials (`ORBIT_EMAIL_FROM`/`GMAIL_APP_PASSWORD`). **Never commit real
  secrets.**

## Security note

Orbit is **cookies-only and local**. It authenticates as you using your existing browser
sessions, reads those cookies at runtime only, and **never logs, transmits, or stores them
off your machine**. There is no Orbit server — if your machine is off, nothing runs and
nothing leaks. Your X `auth_token` is full account access, so treat your cookies like a
password; logging out of that browser revokes access immediately. See
[`SETUP.md`](SETUP.md) §8.5 for the full risk disclosure.
