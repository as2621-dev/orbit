# Master Plan — Orbit

**Date:** 2026-06-18
**Source brief:** documents/product-brief.md
**Status:** Active

## Vision (one paragraph)

Orbit is a personal daily intelligence digest that pulls everything new from the YouTube channels you subscribe to and the X accounts you follow, organizes it by creator and topic, ranks it by signal (not raw popularity), and renders a single HTML one-pager (spilling to page 2 only when needed). A busy person opens it once a day and instantly knows the day's scoops, what's trending across their network, the top podcasts, and what's happening — with one-click deep-links into the exact moment of any video. It ships as a Claude Code skill + plugin, runs on a daily cron on the user's own machine, authenticates cookies-only (no OAuth), and runs all LLM work on the user's own Claude Code/Codex plan. Personal-first, but architected per-user from day one so a future public release is a config flip, not a rewrite.

## Tech stack

- **Frontend:** None (no web app). The only UI is a static, self-contained HTML digest produced by Python templating, governed by its own Design Brief (brief §3 Stage 7). Rationale: the deliverable is a file the user opens locally and links from iMessage — a framework would add build/runtime weight for zero benefit.
- **Backend / data layer:** Local SQLite via `store.py` (lifted from last30days), WAL mode, file at `~/.local/share/orbit/orbit.db`. Rationale: state is single-user, local, and never networked; SQLite is zero-config, durable, and already the proven pattern in the reference implementation. No Convex/Supabase — there is no server and nothing to sync.
- **Agents:** None (no Pydantic AI / LangChain agent framework). LLM calls (classify, chapterize, cluster-label, summarize) are made through the host Claude Code/Codex session that invokes the skill, orchestrated deterministically by `orbit.py`. Rationale: brief §5 (Rule 5) — the model is used only for judgment calls (classification, labeling, summarization); routing, retries, and deterministic transforms stay in code. A standalone agent runtime would duplicate orchestration the skill harness already provides.
- **Jobs:** OS `cron` firing `claude -p "/orbit"` on the user's machine (no Trigger.dev). Rationale: brief §2 — cloud schedulers cannot read local browser cookies, which is the entire auth model. Cron is cookie-native, keeps every credential local, and enables iMessage delivery. Trigger.dev / Claude Routines are explicitly out for cookie mode.
- **Hosting:** None — Orbit runs entirely on the user's own machine. No Orbit server exists. Rationale: privacy and the cookie constraint demand local-only execution; "everything is local" is a stated product promise (brief §8.5).
- **Languages:** Python 3.11+ (skill scripts, pipeline stages, store, render) and Node.js 22+ (the vendored `bird-search` X client — JS, for cookie auth + GraphQL). External tools: `yt-dlp` (YouTube subs, uploads, VTT transcripts). Rationale: matches the reference implementation exactly so lifted modules drop in with minimal change; the X client's header/queryId machinery is JS and is reused verbatim rather than reimplemented in Python.

## Architecture (one diagram)

```
                          ┌──────────────────────────────────────────┐
   OS cron (daily)  ─────▶│  claude -p "/orbit"   (host CC/Codex)      │
   or  /orbit (manual)    │  └─ SKILL.md orchestrates via Bash         │
                          └───────────────────┬────────────────────────┘
                                               │ runs
                                               ▼
                              ┌────────────────────────────────┐
                              │  scripts/orbit.py  (pipeline)   │
                              └───┬─────────────┬───────────────┘
        ┌─────────────────────────┘             └───────────────────────────┐
        ▼                                                                    ▼
  STAGE 0 Load sources (weekly)                                       lib/ stage modules
   ├─ yt-dlp --cookies-from-browser  ──▶ YouTube subs feed                  │
   └─ bird-search Following (Node)   ──▶ X following list                   │
        │                                                                   │
        ▼                                                                   │
  STAGE 1 Delta fetch (per run)                                             │
   ├─ YouTube: new uploads → VTT transcript (KEEP cue timestamps)           │
   └─ X: from:handle SearchTimeline (paced, cursor-paginated)               │
        │                                                                   │
        ▼                                                                   │
  STAGE 2 Classify  (LLM: signal/noise × on/off-topic, channel prior)       │
        ▼                                                                   │
  STAGE 3 Chapterize long-form (creator chapters OR transcript segment)     │
        ▼                                                                   │
  STAGE 4 Cluster overlaps (embed; short merge, long cross-link)            │
        ▼                                                                   │
  STAGE 5 Trending & scoop (internal velocity + external cross-search)      │
        ▼                                                                   │
  STAGE 6 Derank → density tiers (Hero/Standard/Compact/Index)              │
        ▼                                                                   ▼
  STAGE 7 Render HTML (page 1 + optional page 2)  ◀──── orbit.config.json + SQLite state
        │
        ▼
   Deliver: write HTML locally  ──▶  iMessage (AppleScript) / WhatsApp (opt) / Briefcast payload (opt)

   SQLite (store.py): sources · seen · classifications · carryforward · interests
   Cookies: read at runtime only — never logged, never transmitted, never stored off-machine
```

## Key design decisions

1. **Skill + plugin, not MCP.** `SKILL.md` orchestrates Python scripts via Bash, mirroring last30days. Why: it's the proven distribution path (Claude Code marketplace via `marketplace.json`, Codex `/import`) and keeps the pipeline as plain, debuggable scripts. Rules out an always-on MCP server and any persistent daemon.
2. **Cookies-only auth, cron-on-device scheduling.** No OAuth/API path in v1. Why: power-user tool, and the local-cookie constraint dictates that scheduling must run where the cookies live. Rules out cloud schedulers (Trigger.dev, Claude Routines) and any flow that transmits credentials. Makes the permissions README (§8) a first-class deliverable.
3. **Per-user architecture from day one, personal-first scope.** Config, state, auth keyed to be per-user even though v1 serves one person. Why: brief §1 — a future public release should be a configuration flip. Rules out hardcoded paths, single-tenant globals, and shortcuts that assume one user.
4. **Retain VTT cue timestamps end-to-end.** The YouTube transcript fetch keeps cue offsets rather than flattening to plain text. Why: timestamps power chapterization and `watch?v=ID&t=Ns` deep-links — the headline feature. Rules out the simpler "plain transcript" lift; `youtube_yt.py` must be modified to preserve offsets.
5. **Two-axis, item-level classification with a channel-level prior.** Axis A signal/noise, Axis B on/off-topic, classified per item but seeded by a channel default. Why: lets a comedian's insightful video rise and a serious creator's off-topic post sink. Rules out channel-only (too coarse) and pure per-item with no prior (too expensive/noisy). Failing items are deranked to a "they also posted" strip, never dropped.
6. **Rank controls density, never inclusion.** The weighted score sorts items into Hero → Standard → Compact → Index tiers; nothing is excluded by rank. Why: the digest is a complete picture at varying resolution, not a filtered top-N. Rules out hard cutoffs that hide low-ranked-but-present items.
7. **Long-form stays a unit; short-form merges.** Clustering merges short reactions into one "Everyone's talking about" block, but long-form episodes are cross-linked (pointing into timestamps), never shredded into topic clusters. Why: a podcast is one viewing decision; fragmenting it destroys the deep-link value. Rules out treating all items uniformly in the cluster step.
8. **LLM work runs on the user's own plan, throttled by `depth`.** `quick | default | deep` is the single cost/time lever controlling how much gets transcribed/deep-pulled. Why: brief §7 — tokens count against the user's limits, so cost must be visible and controllable. Rules out a hosted inference path or hidden background spend.
9. **Lift, don't rebuild, from last30days.** store, youtube_yt, the vendored `bird-search` Node client (incl. `runtime-query-ids.js`, `cookies.js`, `twitter-client-base.js`), cluster/fusion/dedupe, rerank/relevance, and render are adapted from `mvanhorn/last30days-skill`. Why: these are battle-tested for the exact auth/scrape/rank problems. Rules out greenfield reimplementation of the X header recipe and queryId refresh.

## Milestones (not phases — phases come from /plan-phases)

- **M1 — YouTube half, end to end:** Subscriptions load (yt-dlp, cookies-from-browser, cached weekly) → delta detection of new uploads against `seen` → VTT transcript fetch retaining cue timestamps → chapterize (creator chapters or transcript segmentation) → two-axis classify with channel prior → derank into density tiers → render a density-laddered HTML page with `watch?v=ID&t=Ns` deep-links, spilling to page 2 over budget. YouTube-only Orbit is usable standalone.
- **M2 — X half:** X following list loads via the `Following` GraphQL op (vendored bird client, cookie auth, cursor pagination, queryId live-refresh) → per-handle `from:handle` SearchTimeline delta pull, rate-limit-paced → classified and fed into the *same* ranking and render path as M1. One unified digest spanning both sources.
- **M3 — Overlap, trending & scoop:** Embed + cluster across both sources (short-form merge, long-form cross-link) → internal trending (network velocity, baseline-relative spikes) → external trending (light cross-search to tag corroboration vs scoop) → anomaly/scoop detection (dormant account suddenly accelerating, flagged loudly). Overlap block, right-rail trending, and scoops strip populate.
- **M4 — Delivery & onboarding:** iMessage delivery via AppleScript (TL;DR + scoops + link) → `orbit.config.json` schema, `/orbit --setup` wizard, schedule/cron-entry generation → the README/permissions deliverable (§8) in full → optional WhatsApp (Twilio/Business API) and optional Briefcast script payload. Plugin packaging via `.claude-plugin/marketplace.json`.

## Riskiest assumption (from brief) and how we test it

**Riskiest assumption:** that signal-based ranking + density tiering produces a digest a busy person actually reads daily — i.e., that the ranking surfaces what matters and the one-pager format earns the daily open. This is a product-quality bet, not a technical one; everything else (cookies, yt-dlp, GraphQL) is proven in last30days.

**How M1 tests it:** M1 ships a real, daily-usable YouTube-only digest for the maintainer. Running it for several real days against the maintainer's own subscriptions exposes whether the Hero/Standard/Compact/Index laddering and the derank weights (creator priority, baseline-relative engagement, recency, uniqueness) actually float the right items and whether deep-links get used. If the maintainer doesn't open it or constantly hunts past the top tier, the ranking model is wrong and M2/M3 should not be built on it unchanged. The classification-override loop (user corrects, overrides persist) is the built-in tuning mechanism.

## Out of scope

- No web app, hosted service, multi-tenant backend, or login system in v1.
- No OAuth or official YouTube/X API path — cookies-only.
- No cloud scheduling (Trigger.dev, Claude Routines) in cookie mode.
- No mobile/desktop native client — delivery is HTML file + iMessage/WhatsApp link.
- The digest's look adopts a base design system ("Aura Editorial Features") from the design-references library, but **tokens-only**: they are inlined into the renderer (`reference/design-language.md` is the source of truth), so the page stays self-contained — no `<link>`, no `<script src>`, no web-font fetch. The framework/runtime-weight rationale above still holds.
- WhatsApp delivery and Briefcast payload are explicitly *optional* (M4 stretch), not core.
- No payments, accounts, or analytics telemetry.

## Open questions for /plan-phases

1. **Embedding source for clustering (M3).** Brief §4 cluster lift uses embeddings — resolve whether to embed via the host Claude/Anthropic session, a local model, or reuse whatever last30days' `cluster.py`/`fusion.py` already does. Confirm against the lifted module before phasing M3.
2. **Page-budget height estimation (Stage 7).** "Estimate rendered height" — decide the concrete heuristic (char/element counts → estimated px, or a rendered measurement pass). Affects the page-1/page-2 spill logic and should be pinned in the render phase.
3. **`store.py` schema adaptation.** Reference `store.py` models topics/research_runs/findings; Orbit needs `sources/seen/classifications/carryforward/interests` (brief §5). Decide migrate-in-place vs new schema module. See reference/api-contracts.md.
4. **Following-op coverage in the vendored client.** Confirm the vendored `bird-search` exposes the `Following` GraphQL op (M2 Stage 0) or whether it needs adding alongside the existing SearchTimeline support; if missing, M2 gains a "extend bird client" sub-phase.
5. **`depth` cap semantics for X (Stage 1).** "Rotate which handles get deep-pulled if the follow count is large" — pin the rotation/fairness policy so high-follow users still get coverage over days.
6. **Chapterization trigger threshold (Stage 3).** Define what counts as "long-form" (duration cutoff?) to decide which items get chaptered vs treated as single short items.

## Phases

Generated by `/plan-phases` against the reference clone at `/Users/asheshsrivastava/last30days-skill`. Globally, sequentially numbered across milestones. Each phase has exactly 4 sub-phases and an appended 3-lens self-critique.

### M1 — YouTube half, end to end
- [Phase 1](phase-1-scaffold-state-sources.md) — Scaffold, SQLite state store (5 Orbit tables on the lifted migration framework), YouTube subscriptions loader (Stage 0). Pins: Python floor → 3.12; store schema = reuse migration framework + new schema module + XDG/env path.
- [Phase 2](phase-2-delta-transcripts-classify-chapterize.md) — Delta upload detection, VTT transcripts retaining cue timestamps, two-axis classify with channel prior, chapterize (Stages 1-3). Pins: VTT cue-retention replaces `_clean_vtt`; long-form = duration > 1200s.
- [Phase 3](phase-3-rank-density-render.md) — Weighted derank, Hero/Standard/Compact/Index density tiers, HTML one-pager with deep-links, page-2 spill (Stages 6-7, YouTube-only). Pins: page-budget = per-tier px-estimate heuristic (built from scratch; reference has none).

### M2 — X half
- [Phase 4](phase-4-x-source-into-pipeline.md) — Extend vendored bird client with the `Following` op (ABSENT today), Python Following loader, paced+rotated `from:handle` SearchTimeline delta, classify X items into the shared M1 pipeline. Pins: Following extension required; deterministic day-ordinal handle rotation for fairness.

### M3 — Overlap, trending & scoop
- [Phase 5](phase-5-overlap-trending-scoop.md) — Lexical clustering (short-merge / long-cross-link), baseline-relative internal trending, external corroboration-vs-scoop cross-search, dormant-account scoop detection, render the three M3 sections (Stages 4-5). Pins: NO embedding model — reuse lexical similarity; trending/anomaly built on top of `signals.py` (which lacks time-windowed velocity).

### M4 — Delivery & onboarding
- [Phase 6](phase-6-delivery-onboarding.md) — Config schema + `/orbit --setup` wizard + cron-entry generation, iMessage AppleScript delivery, the §8 permissions/onboarding README (primary deliverable) + plugin packaging; optional WhatsApp + Briefcast gated as stretch.
