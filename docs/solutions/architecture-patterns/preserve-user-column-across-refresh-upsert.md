---
title: preserve a user-set column across an unconditional refresh upsert (+ atomic live-DB migration)
tags: [sqlite, upsert, on-conflict, migration, schema_version, is_user_override, store, alter-table, begin-immediate]
problem_type: pattern
symptoms: "a periodic refresh re-upserts rows with a hardcoded default and clobbers a value the user set (e.g. a channel marked 'noise' resets to 'signal' weekly/daily); adding a NOT NULL column to a live populated DB; a first-ever migration that must not brick init_db"
root_cause: "the refresh caller and the setup path share one upsert; a plain `col = excluded.col` overwrites unconditionally, and the schema had no way to tell a user-confirmed value from a merely-seeded one. Separately, the migration machinery committed the DDL and the schema_version bump in two transactions."
date: 2026-07-18
---

Issue #4. Two coupled patterns for evolving `scripts/store.py` (the shared SQLite store)
without losing user intent or bricking a live DB.

## 1. Preserve a user-set column across a refresh that shares the same upsert

`store.upsert_source` serves BOTH the setup wizard (writes a user's category choice) and the
weekly YouTube / daily X refresh (`persist_subscriptions` / `persist_following`, both pass a
hardcoded `category="signal"`). A plain `category = excluded.category` in the `ON CONFLICT DO
UPDATE` clobbered the user's choice on every refresh — invisibly, because nothing pinned the
category in the store tests (that test blind spot is why the bug shipped; close it).

The fix is an explicit per-row provenance flag + a CASE in the conflict clause — NOT a second
table, NOT app-side read-modify-write:

```sql
ON CONFLICT(platform, external_id) DO UPDATE SET
    display_name = excluded.display_name,          -- always update (renames flow through)
    category = CASE
        WHEN excluded.category_is_user_override = 1 THEN excluded.category  -- user write wins
        WHEN sources.category_is_user_override = 1 THEN sources.category    -- preserve prior user set
        ELSE excluded.category                                             -- seeded: update freely
    END,
    category_is_user_override = CASE
        WHEN excluded.category_is_user_override = 1 THEN 1
        ELSE sources.category_is_user_override      -- a refresh NEVER sets or clears the flag
    END
```

- In SQLite `ON CONFLICT DO UPDATE`, `sources.<col>` = the OLD (pre-update) row and
  `excluded.<col>` = the value that would have been inserted. Both CASEs read the OLD flag
  correctly regardless of SET order — no self-reference hazard. Verified.
- The incoming flag is passed as a normal bound parameter, so `excluded.category_is_user_override`
  IS the caller's `is_user_override` argument. Refresh callers pass the default `0` and need NO
  change — the fix is centralized in the upsert, not sprinkled across callers (surgical).
- Naming: column is `category_is_user_override` (prefixed — `sources` has several user-settable
  columns), echoing the `classifications.is_user_override` precedent; the param drops the prefix
  because it sits next to `category=` at every call site.
- The setup path must call `init_db()` itself before touching the table — the `--setup` path did
  NOT (only the pipeline's Stage 0 did), so the wizard crashed on a fresh DB and hit
  `no such column` on the un-migrated live DB. A test that pre-initializes the store MASKS this;
  drive the wizard against an un-initialized real store to catch it.

## 2. Make the FIRST migration atomic on a live populated DB

`MIGRATIONS` was empty until this slice, so migration 2 (`ALTER TABLE sources ADD COLUMN
category_is_user_override INTEGER NOT NULL DEFAULT 0`) was the first real exercise of
`_run_migrations` — and it was not atomic. `conn.executescript` auto-commits the DDL, but the
`INSERT INTO schema_version` committed later in `init_db`; a failure between them leaves the
column present but the version stale, so the next `init_db` re-runs the ALTER and dies with
`duplicate column name` (a permanent brick).

- `ADD COLUMN ... NOT NULL DEFAULT 0` IS legal and lossless in SQLite (metadata-only; existing
  rows read back the default). NOT NULL is allowed ONLY with a constant default. Verified on the
  real 802-row live DB: all rows survive, back-fill to 0, version → 2, idempotent second run.
- Fix: run each migration's statements AND its version bump inside ONE explicit transaction —
  `conn.execute("BEGIN IMMEDIATE")` … statements … `INSERT schema_version` … `conn.commit()`. Do
  NOT use `executescript` (it forces an intermediate COMMIT). Under Python's legacy isolation a
  bare `conn.execute("ALTER …")` also auto-commits, so the explicit `BEGIN` is required to keep
  DDL in the transaction.
- `BEGIN IMMEDIATE` + re-reading `MAX(version)` under the write lock closes a TOCTOU: two
  overlapping `init_db()` calls (this project has an overlapping-run history) otherwise both read
  version=1 and both apply the migration; the loser now sees the bumped version and skips.
- Keep the v1 baseline schema (`SCHEMA_ORBIT_V1`) UNCHANGED and add the column only via the
  numbered migration. A fresh DB then runs baseline (v1) → migration 2, the SAME single path a
  populated DB takes — never a fresh-only branch that would skip live databases (and never both
  create the column AND ALTER it, which would `duplicate column` on fresh).
