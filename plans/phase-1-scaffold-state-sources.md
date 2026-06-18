# Phase 1: Scaffold, state store, and source loading

**Milestone:** M1 — YouTube half, end to end
**Status:** Not started
**Estimated effort:** M

## Goal
A fresh-cloned Orbit skill scaffold exists with a working SQLite state store (Orbit's five tables on the lifted migration framework) and a YouTube subscriptions loader that, given browser cookies, persists the user's subscription list into `sources` — i.e. Stage 0 for YouTube runs end to end.

## Resolved open questions folded into this phase
- **Python floor → 3.12.** The reference `pyproject.toml` requires `>=3.12`; the brief says "3.11+". Inspection of every lift target (`store.py`, `youtube_yt.py`, `cluster.py`, `dedupe.py`, `fusion.py`, `signals.py`, `rerank.py`, `relevance.py`) found NO 3.12-only syntax (no PEP 695 `type`/generics, no `itertools.batched`, no `@override`; the only `match` tokens are `re.search` variable names). Decision: **raise Orbit's floor to 3.12** to match the reference exactly so lifts drop in unchanged — 3.12 satisfies the brief's "3.11+" (a floor). Pinned in Sub-phase 1's `pyproject.toml`.
- **store.py schema adaptation (master-plan Q3).** Decision: **reuse the lifted migration framework in place** (`schema_version` table + numbered `MIGRATIONS` dict + `_run_migrations` + WAL-on-every-connect + `get_setting`/`set_setting`), but define Orbit's `sources/seen/classifications/carryforward/interests` as a **new schema module** (`SCHEMA_ORBIT_V1`) rather than keeping the reference's `topics/research_runs/findings` domain (its `store_findings`/`compute_topic_delta`/FTS API is tightly coupled to that model and does not transfer). The reference DB path is hardcoded to `~/.local/share/last30days/research.db` with only a test override — add an env/XDG-aware path so Orbit lands at `~/.local/share/orbit/orbit.db` per-user.

## Sub-phases

### Sub-phase 1: Lay down the plugin/skill scaffold
- **Files touched:** `pyproject.toml`, `.claude-plugin/marketplace.json`, `skills/orbit/SKILL.md`, `skills/orbit/scripts/orbit.py`, `skills/orbit/scripts/lib/__init__.py`, `skills/orbit/scripts/lib/log.py`, `skills/orbit/scripts/lib/subproc.py`, `.env.example`, `.gitignore`
- **What ships:** The directory tree from `reference/conventions.md` exists. `pyproject.toml` declares `requires-python = ">=3.12"`, `name = "orbit"`, `dependencies = []` (stdlib-first, mirrors reference). `SKILL.md` has valid frontmatter (name `orbit`, user-invocable) and a Bash-orchestration body stub. `orbit.py` is an argparse entrypoint exposing `--depth {quick,default,deep}` and `--setup` flags that currently print a "not yet implemented" notice per stage. `lib/log.py` is the lifted structured-JSON logger (snake_case event names, `fix_suggestion` on errors, redaction helper that never logs `auth_token`/`ct0`/cookie values). `lib/subproc.py` is the lifted `run_with_timeout` wrapper (process-group kill on timeout).
- **Definition of done:** `python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); assert d['project']['requires-python']=='>=3.12'"` passes; `python skills/orbit/scripts/orbit.py --help` exits 0 and lists `--depth` and `--setup`; `marketplace.json` parses as JSON and names the `orbit` skill; `.gitignore` contains `.env`; importing `lib.log` and `lib.subproc` raises no error under Python 3.12.
- **Dependencies:** none

### Sub-phase 2: Build the Orbit state store
- **Files touched:** `skills/orbit/scripts/store.py`, `skills/orbit/scripts/lib/paths.py`
- **What ships:** `store.py` carrying the lifted migration framework (`schema_version` table, `MIGRATIONS` dict, `_run_migrations`, WAL + `foreign_keys=ON` + `row_factory=Row` on every `_connect`) with a NEW `SCHEMA_ORBIT_V1` defining the five tables exactly per `reference/api-contracts.md`: `sources` (source_id PK, platform, external_id, display_name, category, priority_weight, last_refreshed_at), `seen` (seen_id PK, source_id FK, item_external_id, first_seen_at), `classifications` (classification_id PK, item_external_id, axis_a_signal, axis_b_on_topic, is_user_override, classified_at), `carryforward` (carryforward_id PK, item_external_id, density_tier, surfaced_count, created_at), `interests` (interest_id PK, keyword UNIQUE, is_seeded, created_at). Public API: `init_db`, `upsert_source`, `list_sources`, `get_seen_ids(source_id)`, `mark_seen(source_id, item_external_id)`, `set_classification`/`get_classification`, `add_interest`/`list_interests`, `get_setting`/`set_setting`. `lib/paths.py` resolves the DB path: env override `ORBIT_DB_PATH` → `XDG_DATA_HOME/orbit/orbit.db` → `~/.local/share/orbit/orbit.db`, creating parent dirs.
- **Definition of done:** A test (`tests/test_store.py`) using a temp `ORBIT_DB_PATH` asserts: `init_db()` creates all five tables (`SELECT name FROM sqlite_master WHERE type='table'` contains each); `PRAGMA journal_mode` returns `wal`; `mark_seen` then `get_seen_ids` round-trips an id and a second `mark_seen` of the same id does not duplicate (delta-engine intent — a re-seen item must NOT re-appear as new); `upsert_source` twice with the same `(platform, external_id)` updates rather than inserts a duplicate row.
- **Dependencies:** Sub-phase 1

### Sub-phase 3: YouTube subscriptions loader (Stage 0)
- **Files touched:** `skills/orbit/scripts/lib/youtube_yt.py`
- **What ships:** A `load_youtube_subscriptions(cookie_source: str) -> list[Subscription]` function that invokes `yt-dlp --cookies-from-browser <b> --flat-playlist --dump-json https://www.youtube.com/feed/channels` through `lib/subproc.run_with_timeout`, parses one JSON object per line into typed `Subscription` records (`channel_id`, `display_name`), and a `persist_subscriptions(subscriptions)` that upserts each into `sources` with `platform="youtube"`, `category` defaulted to `signal`, `last_refreshed_at=now`. The subprocess wrapper redacts the cookie-source argument from any error log (never logs cookie values). On "no cookies"/auth failure it raises a clear, actionable error pointing at README §8.6 troubleshooting, not a stack trace.
- **Definition of done:** A test mocks `subproc.run_with_timeout` to return canned `--dump-json` lines (from a `fixtures/youtube_subs.jsonl` sample) and asserts `load_youtube_subscriptions` returns the right `channel_id`s; a second test asserts `persist_subscriptions` writes rows queryable via `store.list_sources(platform="youtube")`; a third (failure) test makes the mock return a "no cookies found" stderr and asserts the raised error message mentions logging into the browser / README troubleshooting (intent: auth failure is loud and actionable, never silent). No test invokes real `yt-dlp`.
- **Dependencies:** Sub-phase 2

### Sub-phase 4: Wire Stage 0 into the pipeline driver + cookie-source config
- **Files touched:** `skills/orbit/scripts/orbit.py`, `skills/orbit/scripts/lib/config.py`
- **What ships:** `lib/config.py` loads `orbit.config.json` into a typed `OrbitConfig` (cookie_source, creator_weights, interests, depth, delivery, schedule) with defaults and validation (`cookie_source` ∈ {chrome,firefox,safari,edge,brave,env}; `depth` ∈ {quick,default,deep}), and writes a `.env`-deferred path for `cookie_source: "env"`. `orbit.py` gains a real Stage 0 step: load config → `init_db()` → if `sources` is empty OR `last_refreshed_at` older than 7 days, call the YouTube subscriptions loader and persist; otherwise log `sources_cache_hit` and skip (weekly refresh per brief §3 Stage 0). LLM is NOT used here (deterministic — Rule 5).
- **Definition of done:** A test drives `orbit.py`'s Stage 0 entry with a mocked subscriptions loader and a temp DB: first run calls the loader and populates `sources`; an immediate second run logs `sources_cache_hit` and does NOT call the loader (weekly-cache intent — daily runs must not re-hit yt-dlp); `load_config` rejects an invalid `depth` value with a clear error. `python skills/orbit/scripts/orbit.py --depth quick` with stubbed cookies runs Stage 0 and exits 0 without touching network.
- **Dependencies:** Sub-phase 3

## Phase-level definition of done
From a clean checkout under Python 3.12, `pytest tests/` for this phase passes, and running `orbit.py` Stage 0 against a mocked yt-dlp populates the `sources` table at `~/.local/share/orbit/orbit.db` (or `ORBIT_DB_PATH`), with WAL enabled and the weekly-cache skip working on the second run. The scaffold imports cleanly and `--help` lists the depth/setup flags.

## Out of scope
- No delta fetch, transcripts, classification, chapterization, ranking, or render (Phases 2-3).
- No X / bird client (M2).
- No live cookie reading in CI — all external boundaries mocked.
- No `--setup` wizard logic beyond the flag stub (M4).

## Open questions
- None blocking. Python floor (→3.12) and store schema (reuse-migration-framework + new schema module + XDG/env path) are resolved above and pinned into Sub-phases 1-2. The cookie-DB-lock "No cookies found" message wording should match README §8.6 once that README ships in M4 — tracked, not blocking.

## Self-critique

**Product lens:** PASS. This phase delivers the foundation the brief's riskiest assumption (signal ranking earns the daily open) must stand on; ranking itself is tested in Phase 3, which is correct because ranking needs real items, which need delta+classify (Phase 2) which need sources (Phase 1). No scope creep: the `--setup` wizard, X, and delivery are explicitly deferred. Every M1 Stage-0 capability (subs load, weekly cache, per-user state) traces to a sub-phase.
**Engineering lens:** PASS. All files sit under the master-plan tech stack (Python skill, SQLite, yt-dlp subprocess; no web/agent/queue framework). Each DoD is fresh-context verifiable via structural/DB checks, not "works end to end". Sub-phase 4 wires Stage 0 but does not lock the delta/classify contract (those are Phase 2), so it is not a premature cement. Sub-phases 2 and 3 touch different files (`store.py` vs `youtube_yt.py`) — no secret duplication.
**Risk lens:** PASS with one flag. File boundaries are disjoint except Sub-phase 4 editing `orbit.py` (created in Sub-phase 1) and adding `lib/config.py` — dependency is explicit and sequential, no conflict. Each DoD includes a test that fails if business logic is wrong (delta no-dup, weekly-cache skip, auth-failure loudness) per Rule 9. **Irreversible:** Sub-phase 2 creates the on-disk SQLite schema — a real user DB migration. Marked below; `/run-phase` should treat schema DDL with care, but since this is schema *creation* (v1) on a fresh per-user file, risk is low. Painting-into-a-corner check: 1→2→3→4 leaves a populated `sources` table and a config loader; Phase 2's delta reads `seen` (created in 2) and `sources` — consistent.
**Irreversible sub-phases:** Sub-phase 2 (creates the SQLite DB schema — `⚠ irreversible` first-write of the per-user state file).
