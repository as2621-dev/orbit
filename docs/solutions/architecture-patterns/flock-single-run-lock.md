---
title: flock single-run lock so overlapping pipeline runs never race SQLite state
tags: [runlock, flock, concurrency, fcntl, launchd, subprocess, cloexec]
problem_type: pattern
symptoms: "a launchd wake-catch-up run fires while a previous/manual run is still writing the SQLite seen-state; two pipelines interleave read-all -> write-all on the same DB"
root_cause: "no cross-run mutual exclusion existed; SQLite's own locks don't stop two Orbit runs from interleaving the delta engine's read-then-write on seen state"
date: 2026-07-18
---

`scripts/lib/runlock.py` (issue #3) guards `orbit.run_pipeline` with a BSD `flock` held for
the whole run: the second overlapping run raises `RunLockHeld`, logs
`pipeline_skipped_already_running`, and exits **0** (an intended skip, not a crash — a
non-zero would make launchd treat it as failure). Pattern + the traps that bit:

- **`flock` is keyed to the OPEN FILE DESCRIPTION, not the process.** A second `os.open` +
  `flock(LOCK_EX|LOCK_NB)` on the same path conflicts even **in the same process** — which is
  exactly how the tests simulate a concurrent holder (`os.open` an independent fd, `flock` it,
  then assert `acquire_run_lock` raises) without spawning a second process. (POSIX `fcntl`
  F_SETLK does NOT have this property — it would see the same-process lock as its own.)
- **Kernel auto-releases on crash — no PID file, no dead-PID detection.** The lock frees when
  the fd closes or the process dies. Release in a `finally` (LOCK_UN) inside another `finally`
  (os.close) so a body exception still frees it. Pin this with a test that raises inside the
  `with` then re-acquires successfully.
- **Gate `RunLockHeld` on the contention errno.** `fcntl.flock` raises `BlockingIOError` (an
  `OSError`) with `errno.EWOULDBLOCK`/`EAGAIN` on contention. Catch bare `OSError` and you
  mislabel `ENOLCK`/`EBADF` as "already running" (a silent skip of a real bug). Re-raise
  anything else (Rule 12 fail-loud).
- **`mkdir(parents=True)` the lock dir BEFORE `os.open`.** The lock is taken before the stages
  that create the DB dir, so on a first-ever run the parent may not exist → a raw `ENOENT`
  `FileNotFoundError` escapes as an unhandled crash (it is NOT `RunLockHeld`).
- **The lock fd must not leak into subprocess children.** CPython's `os.open` sets `O_CLOEXEC`
  (PEP 446) and `subprocess.Popen` defaults `close_fds=True`, so `claude -p` / `yt-dlp` / the
  Node client never inherit it. Do NOT route the lock fd through `pass_fds` and do NOT switch
  to a `fork` multiprocessing start method, or a grandchild would hold the lock past the
  parent's exit.
- **Lock path = per-user, beside the DB.** `paths.resolve_db_path().parent / "orbit.run.lock"`
  so it honors `ORBIT_DB_PATH`/`XDG_DATA_HOME` and tests point it at a tmp dir.

Known gap (see [[launchd-scheduler-install-gotchas]] and
`docs/residual-review-findings/issue-3.md`): the setup wizard's classify/persist path runs
OUTSIDE this lock — low risk (SQLite serializes idempotent upserts), scoped out of the
pipeline-vs-pipeline guarantee.
