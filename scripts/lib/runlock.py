"""Single-run lock so two overlapping Orbit pipeline runs never race on SQLite state.

Both launchd (the 7am agent, which fires a MISSED run on wake) and a manual ``/orbit`` run
enter the same :func:`orbit.run_pipeline`. If a wake-catch-up run fires while yesterday's
long run is still in flight, two pipelines would write the same per-user SQLite ``seen``
state concurrently. This module provides an exclusive, non-blocking lock the pipeline takes
for its whole duration: the second run raises :class:`RunLockHeld` and exits early with a
clear log rather than racing.

Why ``flock`` (not a PID file): the lock is a BSD ``flock`` on a lock file, held for the
life of the run.

  * It is keyed to the OPEN FILE DESCRIPTION, so a second run (even, in tests, a second
    ``open`` in the same process) genuinely conflicts and gets ``EWOULDBLOCK``.
  * The kernel releases it automatically when the fd closes or the process dies — so a
    crashed / ``kill -9``'d run leaves NO stale lock, with no dead-PID bookkeeping.

The lock fd is close-on-exec (CPython's ``os.open`` sets ``O_CLOEXEC`` since PEP 446) and is
never routed through ``subprocess``'s ``pass_fds``, so the ``claude`` / ``yt-dlp`` / ``node``
children the pipeline spawns do NOT inherit it and cannot hold the lock past the parent's
exit. Do not change either of those without re-checking that invariant.

Caveat: ``flock`` is reliable on local macOS filesystems (APFS/HFS+) but best-effort on
network mounts (NFS/SMB) — only relevant if the user's Orbit data dir lives on one.
"""

from __future__ import annotations

import errno
import fcntl
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Make ``lib`` importable whether imported as ``lib.runlock`` (via orbit.py's sys.path
# insert of the scripts dir) or from the scripts dir directly. Mirrors config.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import paths  # noqa: E402

# The lock file lives beside the SQLite DB (same per-user Orbit data dir), so it moves with
# the DB under ``ORBIT_DB_PATH`` / ``XDG_DATA_HOME`` and stays run-vs-run scoped for that DB.
ORBIT_RUN_LOCK_FILENAME: str = "orbit.run.lock"


class RunLockHeld(Exception):
    """Raised when another Orbit run already holds the single-run lock."""


def _default_lock_path() -> Path:
    """Resolve the run-lock path beside the per-user SQLite DB.

    Reuses :func:`lib.paths.resolve_db_path` (which honors ``ORBIT_DB_PATH`` /
    ``XDG_DATA_HOME``) so the lock is per-user and test-overridable the same way the DB is.

    Returns:
        The absolute path to ``orbit.run.lock`` in Orbit's data directory.
    """
    return paths.resolve_db_path().parent / ORBIT_RUN_LOCK_FILENAME


@contextmanager
def acquire_run_lock(*, lock_path: Optional[Path] = None) -> Iterator[Path]:
    """Hold an exclusive, non-blocking run lock for the duration of the ``with`` block.

    Opens (creating if needed) the lock file and takes a non-blocking ``flock(LOCK_EX)``. If
    another run already holds it, raises :class:`RunLockHeld` immediately (never blocks). The
    lock is released — and the fd closed — on exit from the block, INCLUDING when the body
    raises, so a crashing run never leaves the lock held.

    Any OS error OTHER than lock contention (e.g. ``ENOLCK``, ``EBADF``) propagates as-is
    (Rule 12 fail-loud) rather than being mistaken for "another run is active".

    Args:
        lock_path: Override for the lock file path (tests point it at a tmp dir). Defaults
            to :func:`_default_lock_path`.

    Yields:
        The resolved lock path (for logging/inspection).

    Raises:
        RunLockHeld: If another run currently holds the lock.
    """
    resolved_lock_path = lock_path if lock_path is not None else _default_lock_path()
    # Ensure the parent dir exists BEFORE os.open — the lock is taken before the stages that
    # create the DB dir, so on a first-ever run the dir may not exist yet (ENOENT otherwise).
    resolved_lock_path.parent.mkdir(parents=True, exist_ok=True)

    # os.open returns an O_CLOEXEC (non-inheritable) fd on CPython 3.4+, so spawned children
    # never inherit the lock. Do not route this fd through subprocess pass_fds.
    lock_fd = os.open(resolved_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise RunLockHeld(f"Another Orbit run holds the lock at {resolved_lock_path}.") from exc
            raise
        try:
            yield resolved_lock_path
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
