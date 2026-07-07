# Orbit

Orbit is a personal daily intelligence digest of your own YouTube subscriptions and X
following. It pulls what's new, ranks it by signal, and renders a single HTML one-pager —
so you open one digest instead of ten feeds.

**Full onboarding, the five permissions and why we ask for them, the honest risk disclosure,
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
- **Delivery & onboarding** — iMessage delivery (AppleScript, opt-in), optional WhatsApp
  (Twilio) and Briefcast payload, the `/orbit --setup` wizard, and OS-cron scheduling the
  wizard installs for you.

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
   confirm categories and pick priority creators, seeds interests, and sets your delivery
   target. It writes a validated `orbit.config.json` and **installs the daily cron entry for
   you** at a fixed **7am** (`0 7 * * *`), tagged so re-running setup replaces it rather than
   duplicating:

   ```
   0 7 * * * cd /path/to/orbit && claude -p "/orbit" # orbit-daily-digest
   ```

   The schedule is fixed (not a prompt). If the crontab write fails (e.g. a sandboxed
   environment with no `crontab` binary), setup still completes and prints the line for you to
   add manually via `crontab -e`.

The full 5-step setup is in [`SETUP.md`](SETUP.md) §8.3.

## Delivery

iMessage (AppleScript, opt-in) is the delivery channel for the TL;DR, alongside the HTML
one-pager written locally. **Email delivery is explicitly out of scope** (decision 2026-07-06):
Orbit is a local, on-device tool and does not send mail. Optional WhatsApp (Twilio) and a
Briefcast payload remain available; see [`SETUP.md`](SETUP.md).

## Configuration & environment

- **Config:** copy `orbit.config.example.json` to `orbit.config.json` (the `--setup` wizard
  writes this for you). It documents every field: `cookie_source`, `creator_weights`,
  `interests`, `depth` (`quick`/`default`/`deep`), `delivery`, and `schedule`.
- **Environment:** copy `.env.example` to `.env` (gitignored). It carries placeholders for
  env-based X cookies (`AUTH_TOKEN`/`CT0`, only used with `cookie_source: "env"`) and the
  optional WhatsApp/Twilio credentials. **Never commit real secrets.**

## Security note

Orbit is **cookies-only and local**. It authenticates as you using your existing browser
sessions, reads those cookies at runtime only, and **never logs, transmits, or stores them
off your machine**. There is no Orbit server — if your machine is off, nothing runs and
nothing leaks. Your X `auth_token` is full account access, so treat your cookies like a
password; logging out of that browser revokes access immediately. See
[`SETUP.md`](SETUP.md) §8.5 for the full risk disclosure.
