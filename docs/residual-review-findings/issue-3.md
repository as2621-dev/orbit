# Residual review findings — issue #3 (launchd scheduler + run-lock)

From the B9 multi-agent review panel. Both items are LOW severity, fail-safe, and
scope-adjacent to the slice; deferred here rather than expanding the slice. Each has a
concrete fix for a future slice to pick up.

## 1. LOW — launchd `bootout` → `bootstrap` is a known-racy sequence on real macOS

`scheduler._install_launchd_agent` boots the existing agent out, then immediately
bootstraps the new plist. On real hardware, `launchctl bootout` can return *before* the
agent's teardown completes, so a re-install of a **currently-loaded** agent can
intermittently fail bootstrap with `Bootstrap failed: 5: Input/output error`.

- **Why it's low:** fails soft — a non-zero bootstrap logs `setup_launchd_install_failed`
  and the wizard prints manual `launchctl bootstrap` instructions, so setup still completes.
  The race only bites on re-install while the agent is actively loaded and teardown is slow;
  a fresh install (nothing loaded) never hits it. Not exercisable in the faked-runner tests.
- **Concrete fix:** on a non-zero `bootstrap` result, retry once after a short delay
  (~0.5s) before returning failure. Inject the sleep (or the delay seconds) so a test can
  script first-fail-then-succeed without a real wait.

## 2. LOW — the setup wizard's classify/persist runs outside the run-lock

`orbit.run_setup()` → `run_setup_wizard()` walks the classify path, which persists override
rows via the default `store`, WITHOUT taking `runlock.acquire_run_lock`. A launchd 7am run
overlapping an interactive setup would touch the same SQLite DB unguarded.

- **Why it's low / arguably out of scope:** the slice's stated concurrency scope is
  pipeline-vs-pipeline (a run firing while a previous *run* is in flight). SQLite serializes
  the writes (no corruption), and setup's classify writes are idempotent per-item upserts,
  not the read-all→write-all `seen` delta the lock exists to protect. Setup is a one-time
  interactive flow.
- **Concrete fix (if wanted):** wrap the wizard's classify/persist in `acquire_run_lock`
  too, OR document in the README that setup shouldn't be run while the daily agent is
  scheduled to fire.
