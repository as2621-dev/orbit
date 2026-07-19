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

`scripts/orbit.py` is the entrypoint. Run it daily (via the launchd agent `--setup` installs)
to produce the digest, or run it once with `--setup` to configure Orbit for the first time.

## Entrypoint

Run the pipeline driver, passing through any arguments the user gave after `/orbit`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orbit.py" $ARGUMENTS
```

`${CLAUDE_PLUGIN_ROOT}` is the absolute path of this plugin's root (the directory
containing this `SKILL.md`), set by Claude Code; `scripts/orbit.py` lives directly under it.
`$ARGUMENTS` is the text the user typed after the skill name. With no arguments
(a bare `/orbit`), the driver defaults to `--depth default`, so the line above is the daily
run. With arguments it forwards them — e.g. `/orbit --setup` becomes
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/orbit.py" --setup`.

Flags (passed through via `$ARGUMENTS`):

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
   (HTML path + optional email address). The schedule is not asked — it is fixed at 7am daily.
5. Writes a validated `orbit.config.json`, then **installs a launchd LaunchAgent**
   (`com.orbit.daily`, 7am) that runs `python3 scripts/orbit.py` directly from the repo
   (output appends to `~/Library/Logs/orbit.daily.log`) and retires any legacy
   `# orbit-daily-digest` crontab line. If `launchctl` is unavailable it prints manual
   plist instructions instead.

Scheduling is a local launchd agent (no cloud scheduler), because the cookie-based feed reads
must run where your browser sessions live. launchd fires a run missed at 7am on the next wake.
The scheduled job invokes the pipeline directly — not `claude -p "/orbit"` — so it always runs
the repo's current code and survives long runs; the pipeline's LLM stages still shell out to
`claude -p` per prompt. Email delivery reads `ORBIT_EMAIL_FROM` + `GMAIL_APP_PASSWORD` from
`.env` (see `SETUP.md`).
