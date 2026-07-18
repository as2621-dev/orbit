# Residual review findings — issue #4 (category persistence)

From the B9 multi-agent review panel (and the two pre-build risk lenses). The one HIGH
finding (the `--setup` path never calling `init_db`) and the one LOW/MEDIUM concurrency
finding (migration TOCTOU) were FIXED in the slice. The items below are LOW, fail-safe, and
scope-adjacent; deferred here rather than expanding the slice. Each has a concrete fix.

## 1. LOW — `priority_weight` has the same refresh-clobber class as `category`

`store.upsert_source` still overwrites `priority_weight = excluded.priority_weight`
unconditionally, so a refresh resets it to the 1.0 default. Harmless today: nothing writes a
non-default `priority_weight` into the `sources` table — user ranking weights live in
`config.creator_weights`, not `sources`. This is the identical clobber class this slice fixed
for `category`.

- **Why it's low:** no live data loss — the column is never populated with a user value.
- **Concrete fix (only if per-source weights ever become user-settable):** give
  `priority_weight` the same override guard (`is_user_override`-style preservation on
  conflict), or keep weights authoritative in config and never let refresh touch the column.

## 2. LOW — no `PRAGMA busy_timeout` in `store._connect`

`_connect` relies on `sqlite3.connect`'s default 5s `timeout` and sets no explicit
`busy_timeout`. A genuinely concurrent writer during the (metadata-only, microsecond-scale)
`ADD COLUMN` could surface `SQLITE_BUSY` after the default wait.

- **Why it's low:** the new `BEGIN IMMEDIATE` + re-check serializes overlapping migrations, the
  DDL window is tiny, and the failure is self-healing (loser aborts, next run sees v2 and skips).
- **Concrete fix:** add `conn.execute("PRAGMA busy_timeout=5000")` in `_connect`. Deferred
  because it changes fail-fast behavior for ALL store operations, beyond this slice's scope.

## 3. LOW — `_split_sql_statements` is a naive `;` splitter (future-migration footgun)

Correct for every migration that exists (migration 2 is a single `ALTER`). A FUTURE migration
with a `;` inside a string literal, a `CREATE TRIGGER ... BEGIN ... END;` body, or a comment
would mis-split. It runs inside the single `BEGIN`, so a mis-split raises and rolls back
(fail-loud, no corruption) — but it would block that migration.

- **Concrete fix (when a multi-statement migration is first needed):** make each `MIGRATIONS`
  value a `list[str]` of author-pre-split statements (no parsing), or gate the splitter with an
  assertion that rejects `BEGIN...END` bodies. The docstring already documents the constraint.

## 4. LOW/cosmetic — `_confirm_categories` return map keyed by `external_id` only

The returned `{external_id: category}` map drops `platform`; it is used solely for the
`noise_creator_count` completion-log summary. Persistence itself is correctly keyed on
`(platform, external_id)` via `upsert_source`. Worst case is an off-by-one in a log count iff a
YouTube `channel_id` string ever equals an X handle string (practically impossible — `UC…`
24-char ids vs short handles). Not worth changing; noted for completeness.

## 5. (cross-ref) Setup persists outside the run-lock

Already logged as item #2 of `docs/residual-review-findings/issue-3.md`: the wizard's
classify/persist (now including the `sources` override upserts this slice added) runs without
`runlock.acquire_run_lock`. Still LOW / arguably out of scope — SQLite serializes the writes and
the upserts are idempotent per-source, not the read-all→write-all `seen` delta the lock protects.
