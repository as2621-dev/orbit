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
        # The second load is a plain (non-user) refresh that also proposes a new category.
        second_source_id = store.upsert_source("youtube", "UC1", "Name B", category="noise")

        assert first_source_id == second_source_id, "upsert created a new row instead of updating"

        youtube_sources = store.list_sources(platform="youtube")
        matching = [s for s in youtube_sources if s["external_id"] == "UC1"]
        assert len(matching) == 1, f"expected 1 row for UC1, got {len(matching)}"
        assert matching[0]["display_name"] == "Name B", "upsert did not update display_name"
        # Blind spot this test previously left open (why the category-clobber bug shipped): a
        # plain refresh must update a merely-seeded category freely AND must NOT mark the row
        # as user-set. Only an explicit user override (is_user_override=1) freezes the category.
        assert matching[0]["category"] == "noise", "seeded category must update freely on refresh"
        assert matching[0]["category_is_user_override"] == 0, "a plain refresh must not mark the row user-set"


def test_get_source_returns_row_or_none() -> None:
    """get_source returns the matching source row, or None when absent.

    WHY: the setup wizard's re-run path reads the stored category per (platform, external_id)
    to present it as the default and to skip re-classifying user-set channels. That read must
    key on BOTH platform and external_id (a YouTube channel and an X handle could collide on
    the external_id string), and must return None cleanly for a never-seen creator so the
    wizard falls back to auto-classify rather than crashing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        store.upsert_source("youtube", "UC_get", "Gettable", category="noise", is_user_override=1)

        row = store.get_source("youtube", "UC_get")
        assert row is not None
        assert row["category"] == "noise"
        assert row["category_is_user_override"] == 1

        # A different platform with the same external_id is a different source (or absent).
        assert store.get_source("x", "UC_get") is None
        assert store.get_source("youtube", "UC_missing") is None


def test_new_source_defaults_to_signal_and_not_user_override() -> None:
    """A brand-new channel (first appearance via refresh) defaults to signal, not-user-set.

    WHY (edge: new channel): a channel the user has never confirmed must enter the digest as
    ``signal`` (Orbit shows subscriptions by default) and must NOT be treated as a user
    override — otherwise a later correction/refresh could never touch it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        store.upsert_source("youtube", "UC_new", "Fresh Channel")  # refresh-path defaults

        row = store.get_source("youtube", "UC_new")
        assert row["category"] == "signal", "a new channel must default to signal"
        assert row["category_is_user_override"] == 0, "a new channel must not be marked user-set"


def test_upsert_source_preserves_user_set_category_across_refresh() -> None:
    """A user-set category survives a later refresh that proposes a different category.

    WHY (the bug that actually bites): the weekly YouTube refresh and the DAILY X refresh both
    re-upsert every source with a hardcoded ``category="signal"``. Before this fix that
    clobbered a channel the user had marked ``noise`` — weekly on YouTube, daily on X. The
    override must win over the refresh's proposed value, and the override flag must persist so
    it keeps winning on every subsequent refresh.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        # The user marks a channel as noise during setup (is_user_override=1).
        store.upsert_source("youtube", "UC_ovr", "Marked Noise", category="noise", is_user_override=1)

        # The refresh path re-upserts with the hardcoded signal category, no override flag.
        store.upsert_source("youtube", "UC_ovr", "Marked Noise", category="signal")

        row = store.get_source("youtube", "UC_ovr")
        assert row["category"] == "noise", "refresh clobbered a user-set noise category"
        assert row["category_is_user_override"] == 1, "the override flag must persist across refresh"

        # And it keeps winning on a second refresh (not a one-shot).
        store.upsert_source("youtube", "UC_ovr", "Marked Noise", category="signal")
        assert store.get_source("youtube", "UC_ovr")["category"] == "noise"


def test_refresh_updates_display_name_but_preserves_user_category() -> None:
    """An upstream rename updates display_name while the user's category is preserved.

    WHY (edge: renamed upstream): the override is keyed on ``external_id``, not the name. When a
    creator renames their channel, the digest must show the new name (display_name updates
    freely) yet still honor the user's noise mark (category frozen). Pin BOTH halves so a future
    change can't silently freeze the name or thaw the category.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        store.upsert_source("youtube", "UC_rename", "Old Name", category="noise", is_user_override=1)

        # Upstream rename arrives on the next refresh (new name, hardcoded signal, no override).
        store.upsert_source("youtube", "UC_rename", "New Name", category="signal")

        row = store.get_source("youtube", "UC_rename")
        assert row["display_name"] == "New Name", "display_name must update on an upstream rename"
        assert row["category"] == "noise", "the user category must survive a rename"


def test_resubscribed_channel_retains_user_set_category() -> None:
    """A channel that disappears from a refresh and returns keeps its prior user-set category.

    WHY (edge: unsubscribe then re-subscribe): sources are never deleted, so a channel absent
    from one refresh is simply not re-upserted that run — its row (and override) is untouched.
    When it returns, the refresh upserts it again with the hardcoded signal category; the
    override must still win, so a temporary unsubscribe never silently resets the user's mark.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        store.upsert_source("x", "creator_re", "Re Sub", category="noise", is_user_override=1)

        # Refresh #1: the channel is absent (not upserted) — nothing touches the row.
        store.upsert_source("x", "someone_else", "Other", category="signal")
        assert store.get_source("x", "creator_re")["category"] == "noise"

        # Refresh #2: the channel is back — re-upserted via the refresh path.
        store.upsert_source("x", "creator_re", "Re Sub", category="signal")
        assert store.get_source("x", "creator_re")["category"] == "noise", "re-subscribe reset a user override"


def test_migration_2_preserves_rows_on_populated_v1_db() -> None:
    """Applying migration 2 to a POPULATED v1 DB adds the column with no data loss.

    WHY (the user has a LIVE database): migration 2 cannot be a fresh-DB-only feature. Run it
    against a v1 DB that already holds real sources rows and assert every row survives, each
    back-fills to ``category_is_user_override = 0`` (not-user-set — the correct default for
    rows that predate the override concept), and the schema advances to version 2. A rewrite or
    row-drop here would silently wipe the user's followed channels.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "orbit.db"
        import os

        os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
        store._db_override = db_path

        # Build a genuine v1 DB: the v1 baseline schema only (NO migrations), then seed rows
        # the way the old code would have — no category_is_user_override column exists yet.
        seed_conn = sqlite3.connect(str(db_path))
        try:
            seed_conn.executescript(store.SCHEMA_ORBIT_V1)
            seed_conn.execute(
                "INSERT INTO sources (platform, external_id, display_name, category) VALUES (?, ?, ?, ?)",
                ("youtube", "UC_live", "Live Channel", "noise"),
            )
            seed_conn.execute(
                "INSERT INTO sources (platform, external_id, display_name, category) VALUES (?, ?, ?, ?)",
                ("x", "live_handle", "Live Handle", "signal"),
            )
            seed_conn.commit()
            # Precondition: the column does NOT exist on the v1 DB.
            v1_columns = {row[1] for row in seed_conn.execute("PRAGMA table_info(sources)")}
            assert "category_is_user_override" not in v1_columns, "test setup did not build a true v1 DB"
            v1_version = seed_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert v1_version == 1, f"expected a v1 DB, got version {v1_version}"
        finally:
            seed_conn.close()

        # Now run the real init_db — it must apply migration 2 on top of the populated v1 DB.
        store.init_db()

        conn = sqlite3.connect(str(db_path))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
            assert "category_is_user_override" in columns, "migration 2 did not add the column"

            conn.row_factory = sqlite3.Row
            surviving = list(conn.execute("SELECT * FROM sources ORDER BY source_id"))
            assert len(surviving) == 2, f"migration lost rows: {len(surviving)} of 2 survived"
            by_ext = {row["external_id"]: row for row in surviving}
            assert by_ext["UC_live"]["category"] == "noise", "existing category value was not preserved"
            assert by_ext["live_handle"]["category"] == "signal"
            # Pre-migration rows back-fill to not-user-set (0), the safe default.
            assert by_ext["UC_live"]["category_is_user_override"] == 0
            assert by_ext["live_handle"]["category_is_user_override"] == 0

            new_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert new_version == 2, f"schema_version did not advance to 2 (got {new_version})"
        finally:
            conn.close()

        # Idempotent: a second init_db on the now-v2 DB is a clean no-op (no duplicate-column crash).
        store.init_db()
        assert store.get_source("youtube", "UC_live")["category"] == "noise"


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
