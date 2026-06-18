# Conventions — Orbit

**Why this doc exists:** the single place that says how Orbit code is named, structured, logged, and errors. `/run-phase` reads it before writing any code so every phase matches. It refines the global rules in `~/.claude/CLAUDE.md` and the 14 rules in `./CLAUDE.md` for Orbit's specific shape (a Python+Node skill, not a web app).

**When to update:** when a naming or structure decision is made that future phases must follow, or when a global rule needs an Orbit-specific carve-out. Keep it short — it is a checklist, not an essay.

## Language & tooling
- **Python 3.11+** for all skill scripts and pipeline stages. Type hints on every function signature and class attribute. Format/lint with **Ruff** (line length 120, double quotes).
- **Node.js 22+** only for the vendored `bird-search` X client (JS). Do not add new Node code outside `lib/vendor/` unless extending the X client.
- **`yt-dlp`** invoked as a subprocess (via `lib/subproc.py`-style wrapper lifted from last30days), never reimplemented.
- No web framework, no agent framework, no job queue (see master-plan tech stack).

## File structure (mirror last30days)
```
skills/orbit/
  SKILL.md                # user-invocable skill; frontmatter + Bash orchestration only
  scripts/
    orbit.py              # pipeline driver / entrypoint — wiring, not business logic
    store.py              # SQLite state (lifted)
    lib/                  # one module per pipeline stage + per source
      vendor/bird-search/ # vendored Node X client — DO NOT rewrite, only extend
  references/             # LLM prompt templates (classify, label, chapterize, summarize)
  README.md               # onboarding + permissions (brief §8) — primary deliverable
.claude-plugin/marketplace.json
pyproject.toml
```
- **File size:** never exceed 1000 lines; pipeline/stage modules should stay well under 500. Split by stage/source responsibility, not arbitrary line count.
- **One module per stage** (`lib/classify.py`, `lib/chapterize.py`, `lib/cluster.py`, …) and **one per source** (`lib/youtube_yt.py`, `lib/bird_x.py`). `orbit.py` only sequences them.

## Naming (verbose, intention-revealing)
- Python: `snake_case` functions/variables/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants.
- Use full names: `channel_id`, `video_id`, `tweet_id`, `creator_handle`, `last_seen_id`, `priority_weight`, `density_tier` — never `id`, `name`, `w`.
- Stage functions read as verbs: `fetch_new_uploads`, `chapterize_episode`, `classify_item`, `derank_items`, `render_digest_html`.

## Logging (structured JSON, AI-friendly)
- Use structured logging via the lifted `lib/log.py` pattern (or `structlog` if introduced). Descriptive snake_case event names: `loading_sources`, `delta_fetch_completed`, `classification_failed`.
- Include contextual fields (`source`, `video_id`, `count`, `depth`). Error logs MUST include a `fix_suggestion`.
- **Never log cookie values, `auth_token`, `ct0`, or any credential.** This is a hard security rule (brief §4, §8.5). Redact at the boundary.

## Error handling
- Fail loud (Rule 12): a stage that partially fails surfaces what was skipped; it does not silently report success.
- Auth failure (expired cookies, no cookies found) must produce a clear, actionable message pointing the user at the README troubleshooting steps (brief §8.6) — never a silent death or stack trace.
- X 404 (stale queryId) is auto-recovered by the client's refresh-on-404; only surface if it persists after refresh.

## Config & secrets
- Non-secret config in `orbit.config.json`; secrets (explicit cookies if used) in `.env` (gitignored). Provide `.env.example` with placeholders.
- Never hardcode paths, handles, or credentials. All user-specific values come from config — per-user architecture from day one.

## Testing (when requested)
- Tests mirror source: `tests/.../test_<module>.py`, verbose names (`test_chapterize_uses_creator_chapters_when_present`).
- Mock all external boundaries: `yt-dlp` subprocess, the Node X client, LLM calls, filesystem. Never hit real YouTube/X in tests (use the `fixtures/` pattern from last30days).
- Per Rule 9, tests encode *why* behavior matters, not just *what* — e.g. a chapterize test asserts deep-link timestamps survive, not merely that a list is returned.
