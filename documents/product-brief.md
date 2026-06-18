# Orbit — Claude Code Build Spec

**For:** Claude Code (building the plugin) and the maintainer.
**What this is:** the complete build-and-distribute spec for Orbit, a personal daily intelligence digest of your own YouTube subscriptions and X following.
**Reference implementation:** `mvanhorn/last30days-skill` — copy its packaging, cookie technique, and pipeline shape. Where this spec says "lift X from last30days," it means reuse that module nearly verbatim.

---

## 1. What we're building

Orbit pulls everything new from the YouTube channels you subscribe to and the X accounts you follow, organizes it by **creator and topic**, ranks it by signal, and renders a **single HTML one-pager** (overflowing to page 2 when needed). A busy person opens it once a day and instantly knows: the day's scoops, what's trending, the top podcasts, and what's happening — with one-click deep-links into the exact moment of any video.

It is packaged as a **Claude Code skill + plugin** (and importable into Codex), run on a **daily schedule**.

**Scope:** personal-first. But architect every layer (config, state, auth) to be per-user from the start, so a future public release is a configuration flip, not a rewrite.

**Auth:** cookies-only (power-user tool). No OAuth/API path in v1. This makes the permissions README a first-class deliverable, not an afterthought (see §8).

---

## 2. Architecture & packaging

Mirror last30days exactly:

```
orbit/
├── .claude-plugin/
│   └── marketplace.json        # plugin manifest (for Claude Code marketplace, public-later)
├── skills/orbit/
│   ├── SKILL.md                # user-invocable skill, frontmatter + orchestration
│   ├── scripts/
│   │   ├── orbit.py            # entrypoint / pipeline driver
│   │   ├── store.py            # SQLite state (lift from last30days)
│   │   └── lib/                # one module per stage + per source
│   ├── references/             # prompt templates (classify, label, summarize, chapterize)
│   └── README.md               # onboarding + permissions (see §8) — the big one
└── pyproject.toml
```

- **Skill, not MCP.** Same as last30days: `SKILL.md` orchestrates Python scripts via Bash.
- **Distributable as a plugin** via `marketplace.json` when we go public. Codex can `/import` Claude Code skills directly.
- **Invocation:** `/orbit` for a manual run; the same skill is what the scheduler fires.

### Scheduling — and the cookie constraint

There is a hard tension to design around: **cloud schedulers can't read local browser cookies.**

| Scheduler | Laptop closed OK? | Reads browser cookies? | Use for Orbit? |
|---|---|---|---|
| OS cron → `claude -p` (headless) | No | **Yes** | **Default.** Simplest, cookie-native, enables iMessage delivery. |
| Claude Code Desktop task | No (app must be open) | Yes | Fine on a Mac that stays awake. |
| Claude Code Routine (cloud) | Yes | No (cloud only clones repos) | Only viable if cookies are injected as secrets — discouraged in cookie mode. |
| Codex automation | Yes (if machine on) | — | Fine if living in Codex. |

**Default recommendation in the README:** a plain OS cron entry running `claude -p "/orbit"` on the user's own machine, daily. It reads cookies locally, writes the HTML locally, and can fire iMessage via AppleScript — all without sending a single credential anywhere.

---

## 3. The pipeline

Adapt last30days' staged pipeline. Each stage is a `lib/` module; the driver runs them in order and persists between them.

**Stage 0 — Load sources** (cached, refreshed weekly, not daily)
- YouTube subscriptions: `yt-dlp --cookies-from-browser <b> --flat-playlist --dump-json https://www.youtube.com/feed/channels`
- X following: the `Following` GraphQL op via the cookie technique (see §4). Cursor-paginated.
- Persist the source list; subscriptions/follows barely change day to day.

**Stage 1 — Delta fetch** (only what's new since last run)
- Read `last_seen` IDs per source from the state store.
- YouTube: per channel, list recent uploads; for new ones, pull transcript as **VTT and keep cue timestamps** (do not discard them — they power deep-links and chapterization). Lift `youtube_yt.py`, modify to retain VTT offsets.
- X: per handle, pull recent tweets via `from:handle` SearchTimeline (lift the bird client). Rate-limit aware; rotate which handles get deep-pulled if the follow count is large.
- Cap transcription by rank/recency (depth config) to control cost and time.

**Stage 2 — Classify** (two orthogonal axes, item-level with channel-level prior)
- Axis A — **signal vs noise**: knowledge/news/analysis vs entertainment/comedy/vlog.
- Axis B — **on-topic vs off-topic**: relevance to the user's interest profile (§6).
- First run auto-classifies channels from recent titles; user corrects; overrides persist. Per-item classification lets a comedian's insightful video rise and a serious creator's off-topic post sink.
- Items failing either axis are not dropped — they're routed to the bottom "they also posted" strip.

**Stage 3 — Chapterize long-form** (podcasts, long videos)
- If the creator supplied YouTube chapters (timestamps in description): use them verbatim.
- Else segment the transcript: detect topic shifts, label each segment, attach the start cue timestamp.
- Output: an ordered chapter list per episode, each resolving to `watch?v=ID&t=Ns`.
- Long-form is **never** shredded into the topic clusters; it stays one episode unit.

**Stage 4 — Cluster overlaps**
- Embed items (title + highlights for video; text for X). Cluster by cosine similarity (lift `cluster.py` / `fusion.py` / `dedupe.py`).
- **Short-form reactions on the same thing → merge** into one "Everyone's talking about" block.
- **Long-form sharing a topic → cross-link, don't merge.** The overlap block points into each episode's timestamp; the episodes stay separate cards.

**Stage 5 — Trending & scoop detection**
- **Internal trending:** velocity within the user's network — multiple followed people converging, or one item spiking vs the creator's own baseline.
- **External trending:** a light cross-search (repurpose last30days' broader search) to tag "also big outside your network." Distinguishes corroboration ("your people AND everyone") from a scoop ("your people first").
- **Anomaly / scoop:** a normally dormant account that suddenly posts something accelerating fast. Flag it loudly — this is the highest-value signal.

**Stage 6 — Derank into density tiers**
Weighted score per item:
- creator priority weight (user-set) — the thumb on the scale that honors "priority to the creator"
- source diversity (cluster size)
- uniqueness boost (a lone sharp take from a high-priority creator never sinks)
- engagement **relative to the creator's own baseline** (not raw views)
- recency
- trending / scoop multiplier
Sort descending → assign density tiers: **Hero → Standard → Compact → Index**. Rank controls density, never inclusion.

**Stage 7 — Render & deliver**
- Render HTML per the Design Brief: one-line TL;DR, scoops strip, overlap block, creator episode cards with chapter lists, right-rail trending, bottom "they also posted" strip.
- **Page budget:** estimate rendered height; when it crosses the budget, spill Compact + Index tiers to page 2 (a second linked file). Cap at 2 pages/day.
- Deliver: write HTML locally; send a short iMessage/WhatsApp with the TL;DR + scoops + a link to the page. (iMessage = AppleScript on Mac; WhatsApp = Twilio/Business API, optional.)
- Optional: emit the TL;DR + episode list as a Briefcast script payload.

---

## 4. Auth & cookies (cookies-only)

Lift the technique from last30days verbatim.

- **YouTube:** `yt-dlp --cookies-from-browser <chrome|firefox|safari|edge|brave>`. No script needed beyond the wrapper.
- **X:** resolve `auth_token` + `ct0` (priority: CLI args → env `AUTH_TOKEN`/`CT0` → browser cookie store via the sweet-cookie/yt-dlp reader). Build the header recipe exactly as `twitter-client-base.js` does: public web Bearer, `x-csrf-token: ct0`, `x-twitter-auth-type: OAuth2Session`, cookie header, randomized client UUIDs. Call GraphQL ops with cursor pagination.
- **queryId staleness:** X rotates GraphQL `queryId`s. Resolve them live from the x.com JS bundle (lift `runtime-query-ids.js`) with baked-in fallbacks; refresh-on-404.
- **Never transmit cookies.** They are read at runtime, used for local requests, and never written to logs or sent anywhere. Document this plainly (§8).

---

## 5. State management

SQLite, lift `store.py`. Tables:
- `sources` — channels/handles, category (signal/noise), priority weight, last refreshed
- `seen` — per-source last-seen video/tweet IDs (delta engine)
- `classifications` — item-level overrides
- `carryforward` — top-tier items the user hasn't opened, to resurface once
- `interests` — the user's topic profile (§6)

---

## 6. Config schema (per-user from day one)

`orbit.config.json` (or `.env` for secrets):
- `cookie_source`: which browser, or explicit env cookies
- `creator_weights`: map of channel/handle → priority weight
- `interests`: topic keywords (seeded from subscriptions on first run, user-editable) — drives Axis B
- `depth`: `quick | default | deep` — controls how many items get transcribed/deep-pulled; the main cost/time lever
- `delivery`: `{ html_path, imessage_to?, whatsapp_to? }`
- `schedule`: cron expression (for the README's setup step)

---

## 7. Cost & usage expectations

- Orbit runs on the **user's own** Claude Code / Codex plan; LLM tokens (classify, label, chapterize, summarize, cluster) count against their limits.
- `depth` is the throttle: `quick` skips most transcription; `deep` transcribes everything.
- Put a rough daily-cost estimate in the README so users aren't surprised, and recommend `default` to start.

---

## 8. README / onboarding / permissions — the detailed section

This is a primary deliverable. Tone: honest, plain, no hand-waving. Structure:

### 8.1 What Orbit does & what to expect
A daily HTML page summarizing your YouTube subs and X follows, organized by creator and topic, with deep-links. One sentence at the top tells you if it's worth reading further.

### 8.2 Prerequisites
- Python 3.11+, Node 22+ (for the X client), `yt-dlp` installed.
- Logged into YouTube and X in a supported browser (Chrome/Firefox/Safari/Edge/Brave), **or** cookies pasted into `.env`.

### 8.3 Setup (5 steps)
1. Install the plugin.
2. Confirm you're logged into YouTube and X in your browser.
3. Run `/orbit --setup` — it reads your subs/follows, auto-classifies channels, and asks you to confirm categories and pick a few priority creators.
4. Set your delivery target and schedule.
5. Add the cron entry it prints (`claude -p "/orbit"`).

### 8.4 Permissions — what we ask and **why**

| Permission | Why we need it | What we do / don't do |
|---|---|---|
| Read browser cookies (YouTube, X) | In cookie mode there is no API; we authenticate **as you** to read your own subscriptions and following. | Read at runtime only. Never logged, never transmitted, never stored outside your machine. |
| Filesystem write | To save the HTML digest, page 2, and the local state database. | Writes only to the paths you configure. |
| Network access (youtube.com, x.com) | To fetch new videos, transcripts, and tweets from the people you follow. | Talks only to YouTube and X (plus the LLM endpoint for summarizing). |
| Run AppleScript (Mac, optional) | To deliver the digest to iMessage. | Only if you set an iMessage target. Skipped otherwise. |
| LLM usage (your plan) | To classify, cluster, chapterize, and summarize. | Runs on your own Claude Code/Codex usage; controlled by `depth`. |

### 8.5 Honest risk disclosure (do not soften)
- `auth_token` is **full account access**. Treat your cookies like a password. Orbit keeps them local, but you are pasting/holding sensitive credentials.
- Reading X via session cookies is an **unofficial, ToS-gray** method. At high volume, X may rate-limit or flag the account. Orbit paces requests conservatively, but use a sane `depth` and don't point it at thousands of handles aggressively.
- **Revocation is simple:** log out of X/YouTube in that browser, and the cookies invalidate immediately.
- Everything is local. No Orbit server exists. If your machine is off, nothing runs and nothing leaks.

### 8.6 Troubleshooting
- **X returns 404:** stale `queryId` — Orbit auto-refreshes; if it persists, the bundle format changed.
- **"No cookies found":** you're not logged into that browser, or the browser is locking its cookie DB (close it and retry).
- **Cookies expired:** re-log-in; Orbit will alert on auth failure rather than dying silently.
- **Rate-limited on X:** lower `depth` or reduce deep-pulled handles.

---

## 9. Build order

1. **YouTube half, end to end:** subs → delta uploads → transcripts w/ timestamps → chapterize → classify → derank → density-laddered HTML with deep-links + page 2. Ship this working alone.
2. **X half:** following → timelines → classify → into the same ranking/render.
3. **Overlap + trending + scoop passes.**
4. **Delivery** (iMessage, then optional WhatsApp) and the Briefcast payload.

---

## 10. Reuse map from last30days

| Need | Lift from |
|---|---|
| YouTube search/transcripts | `lib/maybe_yt.py` / `lib/youtube_yt.py` (retain VTT timestamps) |
| X cookie auth + GraphQL client | `lib/bird_x.py` + `lib/vendor/bird-search/*` |
| Cookie resolution | `vendor/bird-search/lib/cookies.js` |
| queryId live-refresh | `vendor/bird-search/lib/runtime-query-ids.js` |
| Clustering / fusion / dedupe | `lib/cluster.py`, `lib/fusion.py`, `lib/dedupe.py` |
| Reranking | `lib/rerank.py`, `lib/relevance.py` |
| State store | `scripts/store.py` |
| HTML render | `lib/render.py`, `lib/html_render.py` |
| Plugin/skill packaging | `.claude-plugin/`, `skills/last30days/SKILL.md` |
