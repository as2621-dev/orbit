---
name: orbit
version: "0.1.0"
description: "Load your YouTube and X subscriptions, then surface a ranked daily digest of what's actually worth your attention from the feeds you already follow."
user-invocable: true
allowed-tools: Bash, Read, Write
---

# Orbit

Orbit reads the feeds you already follow (YouTube subscriptions, X follows), tracks
what you have already seen, and surfaces a ranked digest of the new, on-topic, high-signal
items — so you open one digest instead of ten feeds.

`scripts/orbit.py` is the entrypoint. Run it daily (via cron) to produce the digest, or
run it once with `--setup` to configure Orbit for the first time.

## Entrypoint

Run the pipeline driver:

```bash
SKILL_DIR="<absolute path of the directory containing this SKILL.md>"
python3 "${SKILL_DIR}/scripts/orbit.py" --depth default
```

Flags:

- `--depth {quick,default,deep}` — how much work the pipeline does per run (default: `default`).
- `--setup` — run the first-time setup wizard (see below).

## `/orbit`

The daily run. Loads your subscriptions/follows (riding a weekly cache), delta-fetches new
items, classifies them on the signal/noise and on/off-topic axes, ranks them, and renders a
ranked HTML digest to your configured `delivery.html_path`. Reads `orbit.config.json` for
your cookie source, creator weights, interests, depth, delivery target, and schedule.

## `/orbit --setup`

The first-run wizard. It:

1. Asks which browser holds your logins (cookie source).
2. Reads your YouTube subscriptions and X follows (X is best-effort — if X auth is
   unconfigured, setup continues YouTube-only).
3. Auto-classifies each creator into signal/noise using the same classify path the daily
   run uses, then lets you confirm or flip each category and pick priority creators
   (which become `creator_weights`).
4. Seeds your `interests` from subscription titles, then asks for the delivery target
   (HTML path + optional iMessage number) and the cron schedule.
5. Writes a validated `orbit.config.json` and prints the exact OS cron entry
   (`<cron_expr> cd <repo> && claude -p "/orbit"`) for you to paste into `crontab -e`.

Scheduling is OS cron on your own machine (no cloud scheduler), because the cookie-based
feed reads must run where your browser sessions live.
