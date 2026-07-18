---
title: launchd LaunchAgent install gotchas (beyond the stack-notes basics)
tags: [launchd, launchctl, plist, plistlib, LaunchAgent, scheduler, bootout, bootstrap]
problem_type: pattern
symptoms: "plistlib.loads raises ExpatError 'not well-formed (invalid token)'; a re-install intermittently fails Bootstrap 5; the headless agent can't find claude/yt-dlp; bootout returns non-zero on a fresh install"
root_cause: "several non-obvious launchd/launchctl/plistlib behaviors hit while building scripts/lib/scheduler.py for issue #3"
date: 2026-07-18
---

`scripts/lib/scheduler.py` installs `com.orbit.daily`. stack-notes.md already covers the
BASICS (StartCalendarInterval fires a missed 07:00 run on wake, install is idempotent by
label via `launchctl bootout` before re-`bootstrap`, plist in `~/Library/LaunchAgents`).
The non-obvious things beyond that:

- **An XML comment CANNOT contain a literal `--`.** A plist comment mentioning
  `--dangerously-skip-permissions` (double-hyphen) makes `plistlib.loads` raise
  `xml.parsers.expat.ExpatError: not well-formed (invalid token)`. Refer to the flag by name
  WITHOUT the leading dashes in any comment; the flag itself still appears verbatim in the
  `<string>` ProgramArguments below the comment, so a human still sees it. (plistlib can't
  emit comments at all — splice one in after `<plist version="1.0">`; XML parsers ignore it.)
- **bootout and bootstrap take DIFFERENT targets.** bootout uses the SERVICE target
  `gui/<uid>/com.orbit.daily`; bootstrap uses the DOMAIN target `gui/<uid>` + the plist path.
  uid = `os.getuid()`.
- **A non-zero `bootout` on a fresh install is EXPECTED (nothing loaded) — never treat it as
  fatal.** Only inspect the `bootstrap` return code. Only a raised OSError from the runner
  (missing/sandboxed `launchctl`) or a non-zero bootstrap is a real failure → fail soft
  (return False, print manual `launchctl bootstrap gui/<uid> <plist>` instructions).
- **`bootout` returns before teardown completes → an immediate `bootstrap` of a
  currently-loaded agent can intermittently fail `Bootstrap failed: 5: Input/output error`.**
  It fails soft here; the concrete fix (retry once after ~0.5s on non-zero bootstrap) is
  deferred in `docs/residual-review-findings/issue-3.md`.
- **launchd hands the job a MINIMAL environment.** Resolve `claude` to an absolute path via
  `shutil.which("claude")` at plist-generation time (don't rely on PATH to find it), AND set
  `EnvironmentVariables.PATH` (homebrew + system bins) so the downstream `yt-dlp`/`node`
  subprocesses resolve.
- **Wake-catch-up is pinned by ABSENCE, not just presence.** Assert `StartCalendarInterval`
  is set AND `StartInterval` is absent AND `Disabled` is not true — any of those two would
  silently reintroduce cron's skip-on-sleep. Parse with `plistlib.loads` and assert the dict,
  not string matches.
- **Migrate cron only AFTER launchd is confirmed live.** Retiring the legacy
  `# orbit-daily-digest` crontab line before the agent is bootstrapped can leave the user with
  NO scheduler if bootstrap fails.

See [[flock-single-run-lock]] for the run-concurrency guard and
[[ruff-check-is-the-gate-not-ruff-format]] for the lint gate.
