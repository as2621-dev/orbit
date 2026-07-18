"""DoD tests for the single-run lock (``lib.runlock``) and its wiring into ``run_pipeline``.

Per Rule 9, each test encodes WHY the behavior matters. The lock's whole job is to stop two
overlapping Orbit runs (a launchd wake-catch-up firing while a manual/previous run is still
in flight) from BOTH writing the shared per-user SQLite ``seen`` state. These tests pin:

  * sequential runs are NOT blocked (the lock frees on exit),
  * a concurrent holder makes a second acquire raise ``RunLockHeld`` (the exclusion),
  * a crash inside the locked run still frees the lock (no stale lock, no PID bookkeeping),
  * ``run_pipeline`` short-circuits (exit 0 + clear log) when the lock is held, never entering
    the stages.

The concurrent-holder tests hold a real ``flock`` on an INDEPENDENT fd to the same path — a
genuine second open-file-description, which conflicts even within one process (flock is
per-OFD), so no second process is needed.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable so ``import orbit`` / ``from lib import runlock`` resolve
# regardless of the working directory. Mirrors tests/test_orbit_pipeline.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
from lib import runlock  # noqa: E402


def test_run_lock_allows_sequential_runs(tmp_path: Path) -> None:
    """A lock freed on exit lets the NEXT run take it — sequential runs never block.

    WHY: the lock must exclude only CONCURRENT runs. If it leaked past a completed run, every
    subsequent 7am run would wrongly skip forever — worse than no lock. We acquire+release,
    then acquire again; reaching the second body proves the lock was freed.
    """
    lock_path = tmp_path / "orbit.run.lock"

    with runlock.acquire_run_lock(lock_path=lock_path):
        pass
    with runlock.acquire_run_lock(lock_path=lock_path):
        pass  # no RunLockHeld raised == the first run's lock was released


def test_run_lock_blocks_a_concurrent_holder(tmp_path: Path) -> None:
    """A second acquire while the lock is held raises ``RunLockHeld`` — the core exclusion.

    WHY: this IS the guarantee — two overlapping runs must not both proceed to write the
    shared SQLite state. We hold the flock on an independent fd (a real concurrent holder) and
    assert acquire REFUSES rather than handing out a second "held" lock.
    """
    lock_path = tmp_path / "orbit.run.lock"
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(runlock.RunLockHeld):
            with runlock.acquire_run_lock(lock_path=lock_path):
                pass
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_run_lock_is_released_when_the_locked_body_crashes(tmp_path: Path) -> None:
    """A crash INSIDE the locked run still frees the lock, so the next run isn't wedged.

    WHY: this pins the stale-lock guarantee WITHOUT a PID file — an exception in the body must
    still run the release (the context manager's ``finally``), so a subsequent acquire
    succeeds. If release were skipped on error, one crashed 7am run would block every future
    run. We raise inside the ``with`` and then assert a fresh acquire succeeds.
    """
    lock_path = tmp_path / "orbit.run.lock"

    with pytest.raises(ValueError):
        with runlock.acquire_run_lock(lock_path=lock_path):
            raise ValueError("simulated pipeline crash")

    # The lock must be free now: a fresh acquire must NOT raise RunLockHeld.
    with runlock.acquire_run_lock(lock_path=lock_path):
        pass


def test_run_pipeline_skips_early_when_a_previous_run_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_pipeline exits early (0) with a clear log when another run holds the lock — no double-run.

    WHY: both a launchd wake-catch-up and a manual run enter ``run_pipeline``; if one is
    mid-flight the other must NOT proceed into the stages (which would race the SQLite state).
    We point Orbit's data dir at a tmp path, hold the run lock on an independent fd, spy on
    ``load_config`` (the first real stage step), and assert ``run_pipeline`` returns 0, logs
    ``pipeline_skipped_already_running``, and NEVER calls ``load_config`` — proving it
    short-circuited before touching any state, not merely that it returned 0.
    """
    monkeypatch.setenv("ORBIT_DB_PATH", str(tmp_path / "orbit.db"))
    load_config_spy = MagicMock()
    monkeypatch.setattr(orbit, "load_config", load_config_spy)

    lock_path = tmp_path / "orbit.run.lock"
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        exit_code = orbit.run_pipeline(depth="quick")
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    assert exit_code == 0
    assert "pipeline_skipped_already_running" in capsys.readouterr().out
    load_config_spy.assert_not_called()  # never entered the stages
