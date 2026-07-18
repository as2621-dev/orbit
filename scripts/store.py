#!/usr/bin/env python3
"""SQLite state store for Orbit.

Carries the lightweight, dependency-free state pattern lifted from last30days:
- WAL mode + ``synchronous=NORMAL`` + ``foreign_keys=ON`` + ``row_factory=Row`` on
  every connection.
- A ``schema_version`` table plus a numbered ``MIGRATIONS`` dict and
  :func:`_run_migrations`, so future schema changes apply cleanly on top of v1.
- A ``settings`` key/value table backing :func:`get_setting` / :func:`set_setting`.

The Orbit domain schema (``SCHEMA_ORBIT_V1``) is NEW — it defines the five state
tables from ``reference/api-contracts.md`` (``sources``, ``seen``,
``classifications``, ``carryforward``, ``interests``). The reference's
topics/research_runs/findings/FTS domain is intentionally NOT carried over.

The DB path is resolved per-user via :func:`lib.paths.resolve_db_path`
(``ORBIT_DB_PATH`` → ``$XDG_DATA_HOME/orbit/orbit.db`` → ``~/.local/share/orbit/orbit.db``),
with a ``_db_override`` test hook.

All SQL uses parameterized queries — user values are never interpolated into SQL
strings (injection guard). Credential values are never logged.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make ``lib`` importable whether this module is imported as a package member or
# run directly from the scripts dir. Mirrors orbit.py's sys.path pattern so the
# ``from lib import ...`` line below resolves in both cases.
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from lib import log, paths  # noqa: E402  (import must follow the sys.path insert above)

# Test/power-user override for the DB path. When set (a Path), it wins over
# :func:`lib.paths.resolve_db_path`. Mirrors the reference's ``_db_override``.
_db_override: Optional[Path] = None


def _get_db_path() -> Path:
    """Resolve the active DB path, honoring the test override then env/XDG rules.

    Returns:
        The override Path if :data:`_db_override` is set, else the per-user path
        from :func:`lib.paths.resolve_db_path` (which also honors ``ORBIT_DB_PATH``).
    """
    return _db_override or paths.resolve_db_path()


# Orbit v1 baseline schema. The five domain tables match reference/api-contracts.md
# exactly. UNIQUE constraints encode delta-engine / upsert intent (see DoD).
SCHEMA_ORBIT_V1 = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- sources: channels/handles followed. UNIQUE(platform, external_id) so a repeat
-- subscription load upserts the same channel rather than duplicating it.
CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    display_name TEXT,
    category TEXT DEFAULT 'signal',
    priority_weight REAL DEFAULT 1.0,
    last_refreshed_at TEXT,
    UNIQUE(platform, external_id)
);

-- seen: the delta engine's per-source memory. UNIQUE(source_id, item_external_id)
-- so re-marking the same item is idempotent — a re-seen item must NOT reappear as new.
CREATE TABLE IF NOT EXISTS seen (
    seen_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(source_id),
    item_external_id TEXT NOT NULL,
    first_seen_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_id, item_external_id)
);

-- classifications: item-level overrides of channel priors. item_external_id is
-- UNIQUE so set_classification upserts per item.
CREATE TABLE IF NOT EXISTS classifications (
    classification_id INTEGER PRIMARY KEY,
    item_external_id TEXT NOT NULL UNIQUE,
    axis_a_signal INTEGER,
    axis_b_on_topic INTEGER,
    is_user_override INTEGER DEFAULT 0,
    classified_at TEXT DEFAULT (datetime('now'))
);

-- carryforward: top-tier items the user hasn't opened, resurfaced once.
CREATE TABLE IF NOT EXISTS carryforward (
    carryforward_id INTEGER PRIMARY KEY,
    item_external_id TEXT NOT NULL,
    density_tier TEXT,
    surfaced_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- interests: the persisted topic profile driving Axis B. keyword UNIQUE so
-- add_interest is idempotent.
CREATE TABLE IF NOT EXISTS interests (
    interest_id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    is_seeded INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
"""

# Future migrations keyed by version number (>1). The baseline schema is created by
# SCHEMA_ORBIT_V1 (which stays at v1); each numbered migration evolves it on top. A migration
# runs on BOTH a fresh DB (right after the v1 baseline) and an existing populated DB, so there
# is a single schema-evolution path — never a fresh-only branch that would skip live databases.
#
# 2: add ``sources.category_is_user_override`` — the explicit, per-source flag that separates a
#    category the USER confirmed (frozen against the weekly/daily refresh) from a merely-seeded
#    prior (still free to be re-judged). Named for the ``classifications.is_user_override``
#    precedent, prefixed because ``sources`` has several user-settable columns. ``ADD COLUMN``
#    with a constant ``DEFAULT 0`` is lossless: existing rows back-fill to not-user-set.
MIGRATIONS: Dict[int, str] = {
    2: "ALTER TABLE sources ADD COLUMN category_is_user_override INTEGER NOT NULL DEFAULT 0;",
}


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a SQLite connection with Orbit's standard PRAGMAs and row factory.

    Applies WAL journaling, ``synchronous=NORMAL``, ``foreign_keys=ON``, and a
    ``Row`` row factory on every connect (lifted verbatim from last30days).

    Args:
        db_path: Explicit DB path. Defaults to :func:`_get_db_path`.

    Returns:
        An open ``sqlite3.Connection`` (caller is responsible for closing).
    """
    path = db_path or _get_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _split_sql_statements(script: str) -> List[str]:
    """Split a migration body into individual executable statements.

    Migration bodies are project-controlled (never user input) and use ``;`` only as a
    statement terminator — no embedded semicolons — so a plain split is sufficient. Blank
    fragments (e.g. trailing whitespace after the last ``;``) are dropped.

    Args:
        script: The migration SQL — one or more ``;``-terminated statements.

    Returns:
        The non-empty statements, in order.
    """
    return [fragment.strip() for fragment in script.split(";") if fragment.strip()]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending numbered migrations greater than the current max version.

    Each migration's statements AND its ``schema_version`` bump run inside ONE explicit
    transaction and commit together. This is the safety contract for migrating a LIVE
    populated DB: a mid-migration failure rolls BOTH back, so the DB can never end up with a
    changed schema but a stale version number — the state that would re-run the migration on
    the next :func:`init_db` and crash with ``duplicate column name``. SQLite DDL is
    transactional, so an ``ALTER TABLE`` inside the ``BEGIN`` is covered too.

    ``executescript`` is deliberately NOT used here: it forces an intermediate COMMIT, which
    would split the DDL and the version bump across two transactions and reopen that window.

    Concurrency: the version is re-read INSIDE the write transaction (``BEGIN IMMEDIATE`` takes
    the write lock up front), so two overlapping :func:`init_db` calls — which this project has
    a history of — serialize. The loser sees the already-bumped version and skips rather than
    re-applying the migration and raising ``duplicate column name``.

    Args:
        conn: An open connection. Each pending migration is committed atomically here.
    """
    pending_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for version in sorted(MIGRATIONS.keys()):
        # Cheap fast-path: skip without taking the write lock when clearly already applied.
        if version <= pending_version:
            continue
        # Guard against an already-open transaction so the explicit BEGIN can't raise
        # "cannot start a transaction within a transaction".
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # Re-read under the write lock: a concurrent init_db may have applied this version
        # between the fast-path read and acquiring the lock. If so, skip it — never re-apply.
        applied_version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        if version <= applied_version:
            conn.commit()
            continue
        for statement in _split_sql_statements(MIGRATIONS[version]):
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()


def init_db(db_path: Optional[Path] = None) -> Path:
    """Create the Orbit DB and its v1 schema if absent, then apply pending migrations.

    Idempotent: every table uses ``IF NOT EXISTS`` and the version insert uses
    ``INSERT OR IGNORE``, so re-running on an existing DB is a no-op beyond
    applying any new migrations.

    Args:
        db_path: Explicit DB path. Defaults to :func:`_get_db_path`.

    Returns:
        The resolved absolute path to the initialized DB.

    Example:
        >>> import os
        >>> os.environ["ORBIT_DB_PATH"] = "/tmp/orbit_demo/orbit.db"
        >>> path = init_db()
        >>> path.name
        'orbit.db'
    """
    path = db_path or _get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(path)
    try:
        conn.executescript(SCHEMA_ORBIT_V1)
        _run_migrations(conn)
        conn.commit()
    finally:
        conn.close()

    log.log_info("store_initialized", db_path=str(path))
    return path


# --- Sources ---------------------------------------------------------------


def upsert_source(
    platform: str,
    external_id: str,
    display_name: str,
    category: str = "signal",
    priority_weight: float = 1.0,
    last_refreshed_at: Optional[str] = None,
    is_user_override: int = 0,
) -> int:
    """Insert a source or update it in place on a repeat (platform, external_id).

    Dedups on the ``UNIQUE(platform, external_id)`` constraint: a second call with the same
    pair UPDATEs the existing row rather than inserting a duplicate.

    Category-preservation contract (the reason this is not a plain overwrite): ``display_name``,
    ``priority_weight`` and ``last_refreshed_at`` always update from the incoming call, but
    ``category`` is protected once the USER has set it. The weekly YouTube refresh and the DAILY
    X refresh both re-upsert every source with a hardcoded ``category="signal"`` and
    ``is_user_override=0``; without protection that clobbers a channel the user marked
    ``noise``. So on conflict:

      * an incoming **user override** (``is_user_override=1`` — the setup wizard) wins: it sets
        ``category`` and stamps the row user-set;
      * an incoming **refresh** (``is_user_override=0``) updates ``category`` ONLY when the
        stored row is not already user-set, and NEVER clears an existing override.

    A merely-seeded category (``is_user_override=0``) therefore stays free to be re-judged,
    while a user-confirmed one is frozen against every subsequent refresh.

    Args:
        platform: ``youtube`` or ``x``.
        external_id: ``channel_id`` (YouTube) or ``creator_handle`` (X).
        display_name: Human-readable creator name (always updated).
        category: Channel-level Axis-A prior — ``signal`` or ``noise``.
        priority_weight: Ranking weight (mirrors config ``creator_weights``).
        last_refreshed_at: ISO timestamp of the last Stage-0 refresh, or None.
        is_user_override: ``1`` when ``category`` is a user-confirmed choice that must survive
            future refreshes (mirrors the ``classifications.is_user_override`` precedent);
            ``0`` (default) for the seeding/refresh path.

    Returns:
        The ``source_id`` of the inserted-or-updated row.

    Example:
        >>> source_id = upsert_source("youtube", "UC123", "Some Channel")
        >>> isinstance(source_id, int)
        True
    """
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO sources
                   (platform, external_id, display_name, category, priority_weight,
                    last_refreshed_at, category_is_user_override)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, external_id) DO UPDATE SET
                   display_name = excluded.display_name,
                   priority_weight = excluded.priority_weight,
                   last_refreshed_at = excluded.last_refreshed_at,
                   category = CASE
                       WHEN excluded.category_is_user_override = 1 THEN excluded.category
                       WHEN sources.category_is_user_override = 1 THEN sources.category
                       ELSE excluded.category
                   END,
                   category_is_user_override = CASE
                       WHEN excluded.category_is_user_override = 1 THEN 1
                       ELSE sources.category_is_user_override
                   END""",
            (platform, external_id, display_name, category, priority_weight, last_refreshed_at, is_user_override),
        )
        conn.commit()
        row = conn.execute(
            "SELECT source_id FROM sources WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        return int(row["source_id"])
    finally:
        conn.close()


def list_sources(platform: Optional[str] = None) -> List[Dict[str, Any]]:
    """List sources, optionally filtered to a single platform.

    Args:
        platform: If given, return only sources on this platform; else all.

    Returns:
        A list of source rows as plain dicts, ordered by ``source_id``.
    """
    conn = _connect()
    try:
        if platform is not None:
            rows = conn.execute(
                "SELECT * FROM sources WHERE platform = ? ORDER BY source_id",
                (platform,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_source(platform: str, external_id: str) -> Optional[Dict[str, Any]]:
    """Return a single source row by its (platform, external_id) key, or None if absent.

    Keyed on BOTH columns because ``external_id`` alone is not unique across platforms (a
    YouTube ``channel_id`` and an X ``creator_handle`` could collide as strings). The setup
    wizard's re-run path uses this to read a stored category — presenting it as the default and
    skipping re-classification of channels the user already confirmed (``category_is_user_override
    = 1``).

    Args:
        platform: ``youtube`` or ``x``.
        external_id: ``channel_id`` (YouTube) or ``creator_handle`` (X).

    Returns:
        The source row as a plain dict (including ``category`` and
        ``category_is_user_override``), or None when no such source exists.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sources WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Seen (delta engine) ---------------------------------------------------


def get_seen_ids(source_id: int) -> set[str]:
    """Return the set of item external ids already seen for a source.

    Stage 1 uses this to fetch only items not already in ``seen``.

    Args:
        source_id: The source whose seen items to return.

    Returns:
        A set of ``item_external_id`` strings (empty if none seen).
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT item_external_id FROM seen WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        return {row["item_external_id"] for row in rows}
    finally:
        conn.close()


def mark_seen(source_id: int, item_external_id: str) -> None:
    """Record that an item has been seen for a source (idempotent).

    Uses ``INSERT OR IGNORE`` against ``UNIQUE(source_id, item_external_id)`` so
    re-marking the same item does NOT create a duplicate row — a re-seen item must
    never re-appear as new to the delta engine.

    Args:
        source_id: The source the item belongs to.
        item_external_id: ``video_id`` (YT) or ``tweet_id`` (X).
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen (source_id, item_external_id) VALUES (?, ?)",
            (source_id, item_external_id),
        )
        conn.commit()
    finally:
        conn.close()


# --- Classifications -------------------------------------------------------


def set_classification(
    item_external_id: str,
    axis_a_signal: int,
    axis_b_on_topic: int,
    is_user_override: int = 0,
) -> None:
    """Upsert an item's classification, keyed on ``item_external_id``.

    A re-classification of the same item UPDATEs its row (axes / override flag)
    rather than inserting a duplicate, so the latest classification wins.

    Args:
        item_external_id: The classified item.
        axis_a_signal: 1 = signal, 0 = noise (item-level, overrides channel prior).
        axis_b_on_topic: 1 = on-topic, 0 = off-topic.
        is_user_override: 1 if the user corrected it (persists across runs).
    """
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO classifications
                   (item_external_id, axis_a_signal, axis_b_on_topic, is_user_override, classified_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(item_external_id) DO UPDATE SET
                   axis_a_signal = excluded.axis_a_signal,
                   axis_b_on_topic = excluded.axis_b_on_topic,
                   is_user_override = excluded.is_user_override,
                   classified_at = excluded.classified_at""",
            (item_external_id, axis_a_signal, axis_b_on_topic, is_user_override),
        )
        conn.commit()
    finally:
        conn.close()


def get_classification(item_external_id: str) -> Optional[Dict[str, Any]]:
    """Return an item's classification row, or None if it has never been classified.

    Args:
        item_external_id: The item to look up.

    Returns:
        The classification row as a dict, or None.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM classifications WHERE item_external_id = ?",
            (item_external_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Carryforward (resurface-once) -----------------------------------------

# A top-tier item the user did not open is eligible for ONE resurface. The cap is
# enforced in code (SQLite has no native "increment-but-clamp"): the first
# :func:`record_carryforward` for an item inserts the row with ``surfaced_count = 1``;
# every subsequent call is a no-op increment (stays at the cap). One resurface only.
CARRYFORWARD_SURFACED_COUNT_CAP: int = 1


def record_carryforward(item_external_id: str, density_tier: str) -> None:
    """Record (or re-affirm) that a top-tier item is carried forward — capped at one resurface.

    The first call for an ``item_external_id`` inserts a ``carryforward`` row with
    ``surfaced_count = 1``. Every subsequent call leaves ``surfaced_count`` at the
    :data:`CARRYFORWARD_SURFACED_COUNT_CAP` (1) — calling repeatedly NEVER exceeds the
    cap (resurface-once intent, api-contracts ``carryforward`` table). The
    ``density_tier`` is refreshed to the most recent tier the item held.

    Reason: ``carryforward`` has no UNIQUE on ``item_external_id`` in the v1 schema, so
    the cap is enforced here — we look the item up and either insert (count = 1) or
    update the existing row (count clamped to the cap), never inserting a second row.

    Args:
        item_external_id: The item being carried forward (``video_id`` / ``tweet_id``).
        density_tier: The tier the item held when carried forward —
            ``hero`` | ``standard`` | ``compact`` | ``index``.

    Example:
        >>> record_carryforward("vid_abc", "hero")  # doctest: +SKIP
        >>> record_carryforward("vid_abc", "hero")  # second call stays capped  # doctest: +SKIP
        >>> get_carryforward("vid_abc")["surfaced_count"]  # doctest: +SKIP
        1
    """
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT carryforward_id, surfaced_count FROM carryforward WHERE item_external_id = ?",
            (item_external_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO carryforward (item_external_id, density_tier, surfaced_count)
                   VALUES (?, ?, ?)""",
                (item_external_id, density_tier, CARRYFORWARD_SURFACED_COUNT_CAP),
            )
        else:
            # Reason: clamp to the cap (resurface-once) and refresh the tier — never
            # let repeated calls push surfaced_count past CARRYFORWARD_SURFACED_COUNT_CAP.
            capped_count = min(int(existing["surfaced_count"]) + 1, CARRYFORWARD_SURFACED_COUNT_CAP)
            conn.execute(
                "UPDATE carryforward SET density_tier = ?, surfaced_count = ? WHERE carryforward_id = ?",
                (density_tier, capped_count, int(existing["carryforward_id"])),
            )
        conn.commit()
    finally:
        conn.close()
    log.log_info(
        "carryforward_recorded",
        item_external_id=item_external_id,
        density_tier=density_tier,
        surfaced_count_cap=CARRYFORWARD_SURFACED_COUNT_CAP,
    )


def get_carryforward(item_external_id: str) -> Optional[Dict[str, Any]]:
    """Return an item's carryforward row, or None if it was never carried forward.

    Args:
        item_external_id: The item to look up.

    Returns:
        The carryforward row as a dict (``carryforward_id``, ``item_external_id``,
        ``density_tier``, ``surfaced_count``, ``created_at``), or None.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM carryforward WHERE item_external_id = ?",
            (item_external_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Interests -------------------------------------------------------------


def add_interest(keyword: str, is_seeded: int = 0) -> None:
    """Add a topic keyword to the persisted interest profile (idempotent).

    Uses ``INSERT OR IGNORE`` against the ``UNIQUE`` keyword, so adding the same
    keyword twice does not duplicate it.

    Args:
        keyword: The topic keyword (drives Axis B).
        is_seeded: 1 if auto-seeded from subscriptions, 0 if user-added.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO interests (keyword, is_seeded) VALUES (?, ?)",
            (keyword, is_seeded),
        )
        conn.commit()
    finally:
        conn.close()


def list_interests() -> List[Dict[str, Any]]:
    """List the persisted interest profile.

    Returns:
        A list of interest rows as dicts, ordered by ``interest_id``.
    """
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM interests ORDER BY interest_id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# --- Settings --------------------------------------------------------------


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get a setting value by key.

    Args:
        key: The setting key.
        default: Value returned when the key is absent.

    Returns:
        The stored value, or ``default`` if the key is not present.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    """Set (insert or update) a setting value.

    Args:
        key: The setting key.
        value: The value to store.
    """
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = datetime('now')""",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
