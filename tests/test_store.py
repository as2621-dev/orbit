"""DoD tests for the Orbit SQLite state store (Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:
the delta engine and the source upsert are the two correctness contracts Phase 2
depends on, so the tests assert the *intent* (a re-seen item is not new; a repeat
source load updates, never duplicates) — not just that a call returns.

These tests mock the external boundary by pointing the store at a temp DB via
``ORBIT_DB_PATH`` + ``store._db_override`` (no real ``~/.local/share`` write, no
network). They run under pytest if available, and also standalone via the
``__main__`` block below (the project has no pytest installed in this env).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

# Make ``scripts`` importable so ``import store`` and ``from lib import
# paths`` resolve regardless of the working directory the test runs from.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths  # noqa: E402


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    # Honor both the env override (paths.py) and the in-module test hook so the
    # store can never touch the real per-user DB during tests.
    import os

    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def test_init_db_creates_all_five_orbit_tables() -> None:
    """init_db must create the five domain tables the whole pipeline reads/writes.

    WHY: every later stage assumes these tables exist; a missing table would fail
    silently mid-pipeline rather than at init.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _fresh_store(Path(tmp))
        conn = sqlite3.connect(str(db_path))
        try:
            table_names = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        for required_table in ("sources", "seen", "classifications", "carryforward", "interests"):
            assert required_table in table_names, f"missing table: {required_table}"


def test_journal_mode_is_wal_for_safe_concurrent_access() -> None:
    """The DB must be in WAL mode.

    WHY: cron + interactive runs may touch the DB concurrently; WAL is the lifted
    durability contract, and losing it would silently reintroduce write-lock stalls.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _fresh_store(Path(tmp))
        conn = sqlite3.connect(str(db_path))
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert journal_mode.lower() == "wal", f"expected wal, got {journal_mode}"


def test_mark_seen_is_idempotent_so_reseen_item_is_not_new() -> None:
    """Re-marking the same item must not create a second seen row.

    WHY (delta-engine intent): Stage 1 treats anything not in ``seen`` as new. If a
    duplicate row could exist, a re-seen item could leak back into the "new" set and
    the user would be shown something they've already had — the core delta promise.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        source_id = store.upsert_source("youtube", "UC_delta", "Delta Channel")

        store.mark_seen(source_id, "vid1")
        assert "vid1" in store.get_seen_ids(source_id)

        store.mark_seen(source_id, "vid1")  # second mark of the same item
        conn = sqlite3.connect(str(store._get_db_path()))
        try:
            seen_row_count = conn.execute(
                "SELECT COUNT(*) FROM seen WHERE source_id = ? AND item_external_id = ?",
                (source_id, "vid1"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert seen_row_count == 1, f"re-seen item duplicated: {seen_row_count} rows"


def test_upsert_source_dedups_on_platform_external_id_and_updates() -> None:
    """Re-loading the same (platform, external_id) updates the row, never duplicates.

    WHY (upsert intent): the weekly Stage-0 refresh re-loads subscriptions every run.
    Without dedup, ``sources`` would grow unbounded and ranking weights would split
    across duplicate rows. The second load must overwrite display_name in place.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        first_source_id = store.upsert_source("youtube", "UC1", "Name A")
        second_source_id = store.upsert_source("youtube", "UC1", "Name B")

        assert first_source_id == second_source_id, "upsert created a new row instead of updating"

        youtube_sources = store.list_sources(platform="youtube")
        matching = [s for s in youtube_sources if s["external_id"] == "UC1"]
        assert len(matching) == 1, f"expected 1 row for UC1, got {len(matching)}"
        assert matching[0]["display_name"] == "Name B", "upsert did not update display_name"


def _run_all_standalone() -> int:
    """Run every ``test_*`` function in this module without pytest. Returns exit code."""
    test_functions = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for test_function in test_functions:
        try:
            test_function()
            print(f"PASS {test_function.__name__}")
        except Exception as exc:  # noqa: BLE001 — standalone runner surfaces any failure
            failures.append(f"FAIL {test_function.__name__}: {exc!r}")
            print(failures[-1])
    print(f"\n{len(test_functions) - len(failures)}/{len(test_functions)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all_standalone())
