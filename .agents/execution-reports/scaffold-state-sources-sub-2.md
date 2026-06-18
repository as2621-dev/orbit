# Execution Report — Phase 1, Sub-phase 2: Orbit SQLite state store

**Status:** SUCCESS
**Sub-phase:** ⚠ irreversible (defines on-disk v1 schema)
**Committed:** NO (per instructions — code only)

## What shipped
The Orbit per-user SQLite state store: the lifted last30days migration framework
(`schema_version` + numbered `MIGRATIONS` + `_run_migrations` + WAL/synchronous/
foreign_keys/Row on every connect + `get_setting`/`set_setting` + `_db_override`/
`_get_db_path` test hook) carrying a NEW `SCHEMA_ORBIT_V1` that defines the five
Orbit domain tables from api-contracts.md. Plus a per-user DB path resolver.

## Files touched (absolute)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/store.py` (new)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/paths.py` (new)
- `/Users/asheshsrivastava/frommyfeed/tests/test_store.py` (new — DoD test)

No other files touched. No `tests/__init__.py` created (the test is self-contained
and standalone-runnable; not needed). No real `*.db` created in the repo.

## Divergences from the spec (+ why)
1. **No `tests/__init__.py`.** The test inserts the scripts dir on `sys.path` and
   runs standalone; a package init adds nothing. Rule 2 (minimum code). Spec said
   "if needed" — it isn't.
2. **pytest is NOT installed** under `python3.12` OR `python3` (3.14) in this env.
   Per the fallback clause in the prompt, the test file is pytest-compatible AND
   carries an `if __name__ == "__main__"` standalone runner (`_run_all_standalone`)
   that discovers and runs every `test_*` function and reports pass/fail. I did NOT
   install any package. Ran via `python3.12 tests/test_store.py`.
3. **`SCHEMA_ORBIT_V1` omits the reference's `cache_size` PRAGMA and seeded
   `settings` defaults.** Those were last30days-domain (budget/delivery defaults);
   the prompt said lift the framework, not the reference's domain rows. `settings`
   table itself IS carried for get/set_setting. Low risk.

## Self-review findings (git diff lens) + fixes
- **SQL injection surface:** all queries are parameterized; no f-string/`%`
  interpolation of user values anywhere. The only dynamic SQL in the reference
  (`update_run`/`update_finding` column-name interpolation) was NOT carried over.
  PASS.
- **FK correctness:** `seen.source_id REFERENCES sources(source_id)`; `foreign_keys=ON`
  on every connect. `mark_seen`/`upsert_source` ordering in tests respects the FK.
  PASS.
- **UNIQUE constraints match DoD intent:** `sources UNIQUE(platform, external_id)`,
  `seen UNIQUE(source_id, item_external_id)`, `classifications item_external_id UNIQUE`,
  `interests keyword UNIQUE`. `carryforward` has no UNIQUE (spec did not require one —
  resurfacing is count-tracked, not dedup-keyed). PASS.
- **Secret logging:** only `db_path` is logged (a path, allowed). No cookie/token
  fields. PASS.
- **File size:** store.py ~360 LOC, paths.py ~70 LOC — well under 500. PASS.
- No critical/high issues found; no fixes required.

## Validation outputs
- **AST parse** (`ast.parse` on all 3 files): `parse OK`.
- **DoD test** (`python3.12 tests/test_store.py`, standalone — pytest unavailable):
  `4/4 passed` (init creates 5 tables; journal_mode==wal; mark_seen idempotent;
  upsert dedups+updates).
- **Smoke** (tmp `ORBIT_DB_PATH`, `init_db()`): `journal_mode = wal`,
  `all_tables_present = True` (carryforward, classifications, interests,
  schema_version, seen, settings, sources), `schema_version = 1`.
- **Import both ways:** `store` imports as a module and resolves `from lib import
  log, paths` (sys.path insert mirrors orbit.py). PASS.
- **No repo .db:** `find . -name '*.db'` (excl .git) → none.

## DoD check (per Sub-phase 2 bullet) — all PASS
- [PASS] `init_db()` creates all five tables (sqlite_master contains sources, seen,
  classifications, carryforward, interests).
- [PASS] `PRAGMA journal_mode` returns `wal`.
- [PASS] `mark_seen` then `get_seen_ids` round-trips an id; second `mark_seen` of the
  same id does NOT duplicate (count==1) — delta-engine intent encoded in test.
- [PASS] `upsert_source` twice with same `(platform, external_id)` updates rather than
  inserts a duplicate (one row, display_name == "Name B", same source_id).

## Concerns / notes for orchestrator
- **pytest is not installed** in this environment under either interpreter. The
  Phase-level DoD says "`pytest tests/` passes". Sub-phase 3/4 tests will need pytest;
  the orchestrator should install it (e.g. `python3.12 -m pip install pytest`) before
  the phase-level DoD can run via pytest, OR keep the standalone-runner pattern. I did
  not install it (no-network/no-package-install discipline). FLAG.
- `_db_override` and `ORBIT_DB_PATH` both work; tests set both to be safe.
- Migration framework is live but empty (`MIGRATIONS = {}`); Phase 2+ adds keys 2,3,…

## EXACT final public API surface (store.py) for Phase 2/3
```python
init_db(db_path: Optional[Path] = None) -> Path
upsert_source(platform: str, external_id: str, display_name: str,
              category: str = "signal", priority_weight: float = 1.0,
              last_refreshed_at: Optional[str] = None) -> int   # returns source_id
list_sources(platform: Optional[str] = None) -> List[Dict[str, Any]]
get_seen_ids(source_id: int) -> set[str]
mark_seen(source_id: int, item_external_id: str) -> None         # INSERT OR IGNORE
set_classification(item_external_id: str, axis_a_signal: int, axis_b_on_topic: int,
                   is_user_override: int = 0) -> None            # upsert on item_external_id
get_classification(item_external_id: str) -> Optional[Dict[str, Any]]
add_interest(keyword: str, is_seeded: int = 0) -> None           # INSERT OR IGNORE
list_interests() -> List[Dict[str, Any]]
get_setting(key: str, default: Optional[str] = None) -> Optional[str]
set_setting(key: str, value: str) -> None
```
Path resolver: `lib.paths.resolve_db_path() -> Path`
(ORBIT_DB_PATH → $XDG_DATA_HOME/orbit/orbit.db → ~/.local/share/orbit/orbit.db).
