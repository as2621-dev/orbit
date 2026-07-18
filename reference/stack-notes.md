# Stack Notes — Orbit

**Why this doc exists:** the "things future-you will forget" file — version pins, gotchas, and what is lifted from where. `/grab-issue` reads it to avoid relearning the reference implementation's quirks. It is the bridge between this fresh repo and `mvanhorn/last30days-skill`.

**When to update:** when a version is pinned, a non-obvious gotcha is discovered, or a module is lifted/adapted (note what changed from the original).

## Versions / runtime
- **Python 3.11+** (brief §8.2). Note: the reference `pyproject.toml` requires `>=3.12`; Orbit's stated floor is 3.11 — confirm no 3.12-only syntax is used in lifted modules, or raise the floor to 3.12 to match. Resolve in the M1 setup phase.
- **Node.js 22+** for the vendored X client (brief §8.2).
- **`yt-dlp`** must be installed and on PATH. It is the YouTube auth + transcript engine; keep it updatable — a stale `yt-dlp` is the most common YouTube breakage (last30days ships a "stale yt-dlp" quality nudge; carry that diagnostic forward).
- Python deps: keep minimal (reference ships with `dependencies = []` — stdlib-first). Prefer stdlib `sqlite3`, `urllib`, `concurrent.futures` over new packages.

## Reuse map — what is LIFTED/adapted from last30days
Reference clone location: `/Users/asheshsrivastava/last30days-skill`. Pipeline modules are lifted (often near-verbatim) and adapted for Orbit's subscription/following model (vs last30days' topic-search model).

| Orbit need | Lifted from (last30days path) | Adaptation required |
|---|---|---|
| SQLite state store | `skills/last30days/scripts/store.py` | Replace topic/research_runs/findings schema with Orbit tables: `sources`, `seen`, `classifications`, `carryforward`, `interests` (brief §5). Keep WAL + migration pattern. |
| YouTube subs / uploads / transcripts | `scripts/lib/youtube_yt.py` (+ `transcribe.py`) | **Retain VTT cue timestamps** (reference discards them after flattening). Switch from search-query input to subscriptions-feed + per-channel uploads. Keep `DEPTH_CONFIG` / `TRANSCRIPT_LIMITS` throttle shape. |
| X cookie auth + GraphQL client | `scripts/lib/bird_x.py` + `scripts/lib/vendor/bird-search/*` (Node) | Add/confirm the `Following` GraphQL op and `from:handle` SearchTimeline usage. Vendor verbatim; only extend. |
| Cookie resolution | `vendor/bird-search/lib/cookies.js` (+ `lib/safari_cookies.py`, `lib/cookie_extract.py`) | None expected — reuse the browser-cookie reader for Chrome/Firefox/Safari/Edge/Brave. |
| queryId live-refresh | `vendor/bird-search/lib/runtime-query-ids.js` | None — reuse refresh-on-404 + baked-in fallbacks. |
| Clustering / fusion / dedupe | `scripts/lib/cluster.py`, `fusion.py`, `dedupe.py` | Adapt for short-merge / long-cross-link distinction (brief §4 Stage 4). |
| Reranking / relevance | `scripts/lib/rerank.py`, `relevance.py` | Replace scoring weights with Orbit's: creator priority, cluster size, uniqueness boost, baseline-relative engagement, recency, trending/scoop multiplier (brief §3 Stage 6). |
| Trending / signals | `scripts/lib/signals.py` (+ broader search modules) | Reuse for external cross-search; build internal-velocity + anomaly/scoop on top. |
| HTML render | `scripts/lib/render.py`, `html_render.py` + `references/save-html-brief.md` | Re-style per Orbit's Design Brief (§3 Stage 7); add density tiers + page-2 spill. |
| Plugin/skill packaging | `.claude-plugin/marketplace.json`, `skills/last30days/SKILL.md` | Rename to Orbit; adapt SKILL frontmatter + orchestration. |

## Gotchas
- **VTT timestamps are the product.** The biggest deviation from the reference: do not let any lifted YouTube code flatten transcripts to plain text. Cue offsets feed chapterization and `watch?v=ID&t=Ns` deep-links.
- **queryId staleness (X).** X rotates GraphQL `queryId`s; the client resolves them live from the x.com JS bundle with baked-in fallbacks and refreshes on 404. A persistent 404 after refresh means the bundle format changed — surface, don't loop.
- **Cookie DB locking.** A browser holding its cookie DB lock causes "No cookies found." Troubleshooting tells the user to close the browser and retry (brief §8.6).
- **`auth_token` = full account access.** Treat as a password. Read at runtime only; never log/transmit/persist off-machine.
- **Rate limits on X.** Cookie-based reads are ToS-gray; pace conservatively, honor `depth`, rotate deep-pulled handles for large follow counts. Aggressive volume risks flagging the account.
- **LLM spend is the user's.** All classify/chapterize/cluster-label/summarize tokens hit the user's own Claude Code/Codex plan. `depth` (`quick|default|deep`) is the only throttle; default to `default`. Put a rough daily-cost estimate in the README.
- **`reference/save-html-brief.md`** in the clone is the closest analog to Orbit's Design Brief — read it when building Stage 7 render.

## Gotchas — email + launchd pivot (2026-07-18, M5-M7)
- **Gmail will not render Tiles inline.** ~102KB body clip, grid/flex/`@font-face` stripped, base64 `data:` images blocked, `file://` links dead. The design is summary body + HTML **attachment**; never attempt inline rendering.
- **App passwords need 2FA.** A Google account without 2FA cannot mint app passwords — setup must detect auth rejection and say so (`fix_suggestion`), not loop.
- **launchd, not cron, and migrate the old entry.** `StartCalendarInterval` fires a missed 07:00 run on next wake (cron silently skips — the whole point of the switch). Install is idempotent by label (`com.orbit.daily`): `launchctl bootout` before re-`bootstrap`, plist in `~/Library/LaunchAgents`. Setup must also remove the legacy `# orbit-daily-digest`-tagged crontab line or two schedulers race (see the Phase-5 double-orchestrator incident).
- **Headless artifact publish is unproven.** M7's chat link depends on the `claude -p` session being able to publish an artifact; spike it first, and everything downstream of the link is fail-soft.
- **SMTP is an injected boundary.** Like the cron runner and LLM classifier, tests fake the transport (`smtplib.SMTP_SSL`), never the message-building logic; assert no credential appears in headers or logs.
