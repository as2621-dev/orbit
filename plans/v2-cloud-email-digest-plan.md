# Orbit v2 — Cloud Cron + Email Digest Plan

**Date:** 2026-07-17
**Status:** Proposed (supersedes parts of `plans/master-plan.md` — conflicts listed in §3)
**Source:** Brainstorm session 2026-07-17 (feasibility verified via docs + 3 research passes)

## 1. What v2 is

Orbit v1 runs on the user's machine: OS cron → `claude -p "/orbit"` → local SQLite →
density-laddered HTML → iMessage. It works, but only when the laptop is on.

v2 moves the daily run server-side so the digest arrives every morning regardless of
whether any user machine is awake:

- **Scheduler:** GitHub Actions scheduled workflow (chosen over Anthropic Managed Agents
  for simplicity; it lives in this repo, needs no extra platform).
- **Delivery:** email (HTML digest), replacing iMessage as the primary channel.
- **Artifacts per run:** an email-safe HTML digest **and** a detailed `.md` companion
  (condensed no-fluff summaries of every video and original tweet) committed to the repo.
- **Voice handoff:** a link in the email opens Claude with a prefilled prompt referencing
  the day's `.md`, so the user can discuss the digest (voice mode on mobile).
- **Behavior control:** everything the user can tune lives in versioned repo config,
  edited conversationally through the skill.

### Verified feasibility constraints (do not re-litigate without new evidence)

1. Claude Code's in-session cron is session-only (7-day expiry, dies with the session) —
   **not** usable as the daily scheduler. GitHub Actions `schedule:` is the durable option.
2. Headless / API-key Claude sessions **cannot publish claude.ai Artifacts**. Cloud-run
   outputs persist by `git push` (the repo is the storage) — the runner container is ephemeral.
3. Deep links (`claude://claude.ai/new?q=<urlencoded>`) exist for Claude desktop/mobile:
   they **prefill** a prompt (no auto-submit, text only, no file attach). A web
   `claude.ai/new?q=` scheme is undocumented/unverified. Voice mode is **mobile-only**, no
   deep link straight into it. So the handoff is: tap link → prefilled prompt → send →
   tap voice. Three taps, not one. Reading the `.md` from the new session requires the
   user's claude.ai GitHub connector (repo is private; raw URLs won't be public).
4. iMessage cannot be sent from a cloud runner (requires a Mac). It survives only as the
   legacy local mode.
5. X free API tier is dead (Feb 2026). Cookie scraping (vendored bird client / twscrape
   pattern) is the path; it breaks every 2–4 weeks and needs a maintained client.
   Paid pay-per-use API (~$0.005/read) is the reliability fallback if scraping churn
   becomes unacceptable.
6. Gmail clips HTML email over ~102KB (pre-gzip source, images excluded). This is the
   binding budget for a 20–30 item digest.

## 2. Architecture

```
GitHub Actions (cron, UTC, off-hour minute; + workflow_dispatch for manual runs)
  └─ checkout repo (state + config live here)
     └─ setup: Python 3.12, Node 22, yt-dlp; secrets → env (mask everything)
        └─ cookie health check (fail fast → send "auth expired" alert email, exit)
           └─ claude -p "/orbit --cloud"   (headless, ANTHROPIC_API_KEY secret)
              └─ scripts/orbit.py — existing Stages 0–6 unchanged
                 ├─ Stage 7a  render email HTML   (NEW renderer, email-safe, §5)
                 ├─ Stage 7b  render digest .md   (NEW: detailed companion, §6)
                 └─ (existing browser HTML render kept, linked from the email)
        └─ commit digests/YYYY-MM-DD.{html,md} + state → push
        └─ send email via Resend API (RESEND_API_KEY secret)
        └─ on any failure: send failure email naming the broken stage + the fix command
```

- **State:** keep SQLite + `store.py` (seen, classifications, carryforward, interests)
  but relocate the DB into the repo (`state/orbit.db`) and commit it after each run.
  Single writer (the cron) → no merge conflicts. The daily commit also keeps the repo
  active, which prevents GitHub's 60-day auto-disable of scheduled workflows.
- **Delta window:** "since last successful run" (already how `seen` works), not a fixed
  24h — a missed run is caught up automatically, and re-runs don't duplicate.
- **Manual run:** `workflow_dispatch` on the same workflow; the skill triggers it via the
  GitHub API/MCP so on-demand and scheduled runs share one code path.

### Secrets (GitHub Actions secrets — never in the repo, `::add-mask::` defensively)

| Secret | Contents |
|---|---|
| `ANTHROPIC_API_KEY` | LLM calls in headless run. Set a spend limit on the key. |
| `YT_COOKIES` | Netscape cookies.txt export. Export from a **private/incognito window that is then closed** — YouTube rotates cookies on live sessions and will invalidate a file exported from an open browser. |
| `X_COOKIES` | `auth_token` + `ct0`. Prefer a burner/secondary X account — scraping accounts carry ban risk. |
| `RESEND_API_KEY` | Email delivery. Free tier (100/day) is ample. Sending to the owner's own address needs no domain verification; arbitrary recipients (future multi-user) require a verified domain. |

## 3. Conflicts with v1 decisions — resolutions (Rule 7: pick, don't blend)

| v1 decision (master-plan.md) | v2 resolution | Why |
|---|---|---|
| "No cloud scheduling — cookies live on device" (§ decision 2) | **Reversed.** Cookies are exported once into GH secrets; cron runs in Actions. | Deliberate product choice this session: digest must arrive with the laptop off. The privacy trade (cookies held in GH secrets) is disclosed in SETUP.md. |
| `--cookies-from-browser` at runtime | `--cookies state-file` from secret in cloud mode; browser mode kept for local runs | Headless runner has no browser. |
| iMessage is primary delivery | Email primary; iMessage demoted to optional legacy local mode | Cloud runner can't send iMessage. |
| SQLite at `~/.local/share/orbit/` | `state/orbit.db` inside the repo, committed per run (path stays configurable; local mode keeps XDG default) | The repo is the only durable storage a cloud run has. |
| Single density-laddered browser HTML is *the* deliverable | Email-safe digest (new, §5) is what ships; the browser one-pager remains as the "view full digest" link target, committed alongside | Email clients cannot render the v1 design (no grid/flex-wrap/media-queries in Gmail-non-Google-account; 102KB clip). Two renderers, one data model. |
| Skill surface: single `/orbit` with flags | **Kept** (conforms to repo convention) — the 4 verbs from the brainstorm become subcommands, §4 | Rule 11: existing convention wins over the 4-separate-skills sketch. |

## 4. Skill surface (all under the existing `/orbit` skill)

| Command | What it does |
|---|---|
| `/orbit --setup-cloud` | One-time: walks cookie export (incognito procedure), writes the four secrets (`gh secret set` / GitHub MCP), installs `.github/workflows/digest.yml`, migrates/initializes `state/orbit.db` in-repo, sends a test email. Extends the existing `--setup` wizard; reuses its subs/follows pull + classify + curate steps. |
| `/orbit --now` | Fires `workflow_dispatch` on the digest workflow (does **not** run the pipeline locally — one code path). |
| `/orbit --config …` | Conversational edits: add/remove channels & handles, priority up/down, digest length (`depth`), schedule, delivery address, empty-day behavior. Writes `orbit.config.json`, commits + pushes. Git history = audit log of every preference change. |
| `/orbit --auth` | Cookie refresh, the most frequent maintenance event (expect YouTube every few weeks, X on every scraper breakage). Re-runs just the export procedure + secret update + health check. |
| `/orbit` (bare) | Unchanged: local run (legacy mode). |

Config schema additions to `orbit.config.json`: `mode: "cloud"|"local"`,
`delivery.email_to`, `delivery.empty_day: "note"|"skip"`, `digest.max_items`,
`digest.bullets_per_item: 3-4`.

## 5. Email digest design (locked this session)

Global: 600px single-column table layout, fluid-hybrid (inline-block + mso ghost tables),
**no** CSS grid/flexbox-wrap/media-query dependence, no hover/JS/srcset, white background
with near-black text and no mid-grey text or 1px light borders (Gmail dark-mode
force-inversion), whole card clickable, minified, **hard budget ~90KB** with a validator
in the render step (over budget → drop to compact rows + "view full digest" link).

### YouTube card
- Thumbnail left: hotlink `https://i.ytimg.com/vi/<ID>/mqdefault.jpg` (never base64-embed).
- Right: channel + title, then **3–4 bullets**, each prefixed with a clickable timestamp
  → `https://youtu.be/<ID>?t=<seconds>`.
- **Timestamp grounding (hard requirement):** the summarize prompt receives the
  cue-timestamped transcript (already retained end-to-end per v1 decision 4) and must cite
  a real cue per bullet; the renderer **validates in code** that every timestamp exists in
  the caption cues and is < video duration. No captions → plain bullets from
  title/description, no fabricated timestamps.

### X section — Option B (avatar strip + author roll-up cards)
- Section header: one row of small circular avatars — "who posted today". Avatars are the
  one safe grid-ish element at 600px.
- Then one card per author: circular avatar left (`_200x200` variant displayed at 48px,
  retina-sharp; `border-radius:50%`, square fallback in Outlook classic is accepted),
  name + handle, then 2–3 bullets summarizing that author's **original** tweets/threads
  of the day — each bullet links to its tweet. A thread (grouped by `conversationId`,
  author-only tweets) is **one** bullet. Retweets and replies excluded; quoted content
  available as context to the summarizer, not rendered as items.
- Avatar URLs are fetched fresh every run (they 404 after a user changes avatar — never
  cache into archived digests). Avatars/media hotlinked from `pbs.twimg.com`; verify
  hotlinkability with one live curl during Phase 9 (research was consistent but could not
  confirm first-hand from the sandbox).
- Text-only card variant required (authors with no media; also used if avatar fetch fails).

### Cross-source
- Cross-source topic clustering in the *email* is deferred (v1's overlap pass still runs
  for the full-HTML page). Email order: user-priority creators first, then recency.
- Empty day: per config, one-line "quiet day" email or no email.

## 6. The `.md` companion + voice handoff

- `digests/YYYY-MM-DD.md`: per video — condensed detailed summary (from raw transcript,
  no fluff) with the same grounded timestamps; per X author — original-ideas summary with
  tweet links. This is the discussion corpus, richer than the email bullets.
- Email footer link: "Discuss today's digest with Claude" →
  `claude://claude.ai/new?q=<urlencoded "Read digests/YYYY-MM-DD.md from my orbit repo (GitHub connector) and discuss it with me">`.
  Fallback plain `https://claude.ai/new` link beside it for clients that block custom
  schemes. Scope honestly: prefilled handoff, not one-click voice.

## 7. Reliability & failure policy (Rule 12, product edition)

- **Fail loud:** any stage failure → a *different* email ("Orbit failed: <stage> — run
  `/orbit --auth`" for auth failures), never silent absence. Cookie health check is step 1.
- **Partial failure:** X breaks but YouTube succeeds → ship the partial digest with a
  visible notice line, and still commit state for the succeeded half only.
- **Cost guards:** cap transcripts per run (existing `depth` lever governs), truncate
  very long transcripts (3h podcast ≈ 40k tokens), write per-run token spend into the
  state DB and print it in the email footer.
- **Cron hygiene:** off-hour minute (e.g. `23 13 * * *` UTC ≈ 6:23am PT); GH Actions
  schedule delays of minutes are normal and acceptable for a morning digest.

## 8. Security notes (full prose — do not compress)

The YouTube and X cookies stored as GitHub Actions secrets are full account-access
credentials. The repository must remain private. The workflow must never echo secrets
into logs; apply `::add-mask::` to every secret-derived value at job start. The setup
wizard must state this trade-off to the user explicitly before writing any secret, and
must recommend a burner X account for scraping. Set a monthly spend limit on the
`ANTHROPIC_API_KEY` used by the workflow. Digest content (summaries of a person's
subscriptions) is personal data: it stays in this private repo and in the user's own
inbox, nowhere else.

## 9. Risks

1. **Cookie churn** (highest frequency): YouTube rotation weeks-scale; X scraper breakage
   every 2–4 weeks. Mitigation: health check + alert email + one-command `--auth`;
   vendored client's runtime queryId refresh already self-heals the most common X break.
2. **Gmail 102KB clip:** enforced by a render-time size validator with graceful degrade.
3. **Timestamp hallucination:** eliminated by cue-grounding + deterministic validation.
4. **Handoff friction:** 3-tap flow may see low use; measure by link style later, don't
   over-invest in v2.0.
5. **GH Actions on free tier:** daily commit keeps the workflow from the 60-day disable;
   runner minutes for one daily run are far inside the free allowance for private repos.

## 10. Proposed phases (input to `/plan-phases`; numbering continues after phase 7)

- **Phase 8 — Cloud runner + state-in-repo:** `digest.yml` workflow (schedule +
  dispatch), secrets plumbing + masking, cookie health check + alert email, cloud cookie
  mode in `youtube_yt.py`/bird client, `state/orbit.db` relocation + post-run commit/push.
  DoD: scheduled run completes headless end-to-end with existing renderer, state persists
  across runs.
- **Phase 9 — Email renderer + .md companion:** new `email_render.py` implementing §5
  (both card types, avatar strip, size validator, dark-mode-safe palette), grounded
  timestamp validation, `digests/` output + commit. DoD: real digest renders unclipped in
  Gmail (web + app), timestamps all validate, one live pbs.twimg.com hotlink confirmed.
- **Phase 10 — Delivery + skill verbs:** Resend integration, failure/partial-failure
  emails, `--setup-cloud`, `--now`, `--config`, `--auth` subcommands, SETUP.md cloud
  section (§8 disclosures). DoD: test email + forced-failure email both received; each
  verb round-trips config/secrets.
- **Phase 11 — Voice handoff + polish:** footer deep link + fallback, `.md` prompt
  tuning for discussion quality, empty-day behavior, token-spend footer. DoD: handoff
  works desktop + mobile; a full week of real daily runs green.
