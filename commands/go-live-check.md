---
description: Push-button iOS readiness check. Seeds 4 test profiles, drives the full web journey in parallel (onboarding → reel → audio → Q&A → live voice), audits internal functionality, verifies personalized ranking, smoke-tests the iOS simulator build, and assembles a human review pack + consolidated go-live report.
argument-hint: [optional: "skip-ios" to stop after the web fleet, or "profiles a,b" to run a subset]
---

# Go-Live Check

You are the **go-live orchestrator**. You do NOT fix app code yourself and you do NOT commit. You run preflight, seed test users, spawn parallel sub-agents (4 profile drivers + 1 evaluator), allocate real personalized feeds, re-verify, smoke the iOS build, and produce one consolidated report under `.agents/e2e/`. Failures route to `/debug` — never patch mid-run (Rule 12: record the red, finish the sweep, report honestly).

All building blocks already exist: `scripts/e2e/profiles.json` (4 profiles), `seed-test-users.ts`, `drive-profile.ts`, `allocate_test_feeds.py`, `cleanup-test-users.ts`.

## Step 0 — Preflight (fail loud, stop on any miss)

Every check below must pass before anything else runs. On the first miss: STOP, report exactly what's missing and how to fix it. Do not "continue with what works."

1. `.env` contains values for: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `NEXT_PUBLIC_QA_API_BASE_URL`. (Check presence, never print values.)
2. `NEXT_PUBLIC_FEED_SOURCE` and `NEXT_PUBLIC_ONBOARDING_SKIP_AUTH` must be **UNSET** in `.env` — either one invalidates the entire run (fixture feed / skipped auth = not testing production paths).
3. Chrome binary exists: `test -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`.
4. Railway worker alive: `curl -s -o /dev/null -w "%{http_code}" -X POST $NEXT_PUBLIC_QA_API_BASE_URL/api/voice/live-token` → must be `200`.
5. `.venv` exists (`test -d .venv`).
6. Record `git rev-parse HEAD` for the final report.

## Step 1 — Boot the dev server

Run `npm run dev` in the background. Poll `curl -sf http://localhost:3000` until it returns 200, up to 90s.

**Known failure mode:** a stale dev server can return 500 on `/` from a corrupt dev cache. If you get persistent 500s: kill the dev server, `rm -rf .next`, restart, re-poll. If it still won't serve 200 after the restart, STOP and report (infra failure).

## Step 2 — Seed test users

```bash
npx tsx scripts/e2e/seed-test-users.ts
```

Creates/resets the 4 tagged test users (password auth — no magic-link dependency) and wipes their personalization rows. Verify `.agents/e2e/state/test-users.json` was written; STOP if not.

## Step 3 — First pass: 5 parallel sub-agents in ONE message

Spawn all five as background sub-agents **in a single message**: one profile agent per profile (`profile-a-tech-ai`, `profile-b-sport`, `profile-c-markets-geo`, `profile-d-arts-mixed`) + one evaluator agent.

### Profile-agent prompt template (×4, substitute `<profile>`)

```
You are verifying the full user journey for test profile <profile> in the News20/blip go-live check.

## Your mission
Run: npx tsx scripts/e2e/drive-profile.ts --profile <profile>

This drives a dedicated headless Chrome (per-profile CDP port) through: onboarding splash →
topic picker (real clicks) → source swipe → build-30 → reel (this first pass asserts the
global-feed fallback console event) → audio playback + karaoke sync → article layer → text
Q&A (Railway worker grounding) → LIVE Gemini voice session (token mint, constrained WS,
ask_about_story declared, setupComplete, model responds, clean teardown).
Exit 0 = all steps passed. Results: .agents/e2e/state/<profile>-result.json.
Failure artifacts (screenshot + console/network dumps): .agents/e2e/state/<profile>/.

## On failure — flake guard (at most ONE retry)
1. Read the result JSON and the failure artifacts FIRST. Diagnose from evidence, not guesses.
2. If the failure looks like stale user state, you MAY re-seed (npx tsx scripts/e2e/seed-test-users.ts)
   and retry the driver ONCE. WARNING: re-seeding resets ALL FOUR profiles — other agents may be
   mid-run. Prefer retrying WITHOUT a reseed unless stale state is clearly the cause.
3. One retry total. If it fails again, record the failure — do not loop.

## Report
Write .agents/e2e/state/<profile>-verdict.md:
- Per-step status table (step → PASS/FAIL)
- Evidence for any failure (verbatim console/network error, screenshot path)
- Suspected root cause as file:line if diagnosable from the artifacts; otherwise say "undiagnosed"

## Hard rules
- NEVER edit or fix app code. You verify; /debug fixes.
- NEVER commit anything.
- Return: STATUS (PASS/FAIL), failed steps if any, retry used (yes/no), verdict file path.
```

### Evaluator-agent prompt template (×1 — internal-functionality audit)

```
You are the internal-functionality evaluator for the News20/blip go-live check. You verify the
data and ranking layers directly — DB truth, not UI. Use the service-role key from .env for
queries (never print it). NEVER edit app code; NEVER commit.

Run all six audits; report each independently. A red in one does not stop the others.

(a) Ranking invariants:
    .venv/bin/python -m agents.pipeline.sim.ranking_sim --days 5 --profile B
    .venv/bin/pytest tests/agents/pipeline/test_ranking_simulation.py -v
    Both must pass with zero skips.

(b) Sourcing health (service-role queries on stories):
    - Recency of story_first_reported_utc — was today's GDELT ingest fresh?
    - Distinct outlet count
    - Duplicate-headline sanity check

(c) YouTube/X/podcasts presence:
    - Stories per feed bucket + content_sources catalog coverage per platform.
    - If a platform has ZERO rows, report it as an honest named gap ("ingestion not built"),
      never a fudged pass.

(d) Asset completeness — for EVERY story in the global feed and the 4 test users' daily_feeds:
    - digest_audio_url responds HTTP 200
    - poster url responds HTTP 200
    - caption_sentences rows exist with non-empty word_tokens
    - digest_duration_ms > 0
    List every failing story id.

(e) Personalization conformance — per profile: the stories in daily_feeds have story_interests
    overlapping that profile's user_interest_profile / user_entity_follows rows.

(f) RLS spot check — using the ANON client (not service-role), confirm you CANNOT read another
    user's daily_feeds or user_interest_profile rows. A readable foreign row is a critical red.

## Report
Write .agents/e2e/state/evaluator-verdict.md: one section per audit (a)–(f) with
PASS/FAIL/AMBER/GAP, the exact numbers found, and evidence queries/outputs.
Return: per-audit status summary + verdict file path.
```

## Step 4 — Collect first-pass results

Wait for all 5 sub-agents. Read every `.agents/e2e/state/<profile>-result.json` and all 5 verdict files.

- **Infra failure** (dev server died, Supabase unreachable, seeding broken, Chrome won't launch) → STOP, report what broke and where the artifacts are.
- **Journey reds** (a step failed for a profile) → record them in your running matrix and **continue** — the rest of the sweep still produces useful signal (Rule 12: report all reds at the end, hide none).

Checkpoint (Rule 10): state the step×profile matrix so far before proceeding.

## Step 5 — Allocate personalized feeds (real ranking code)

```bash
.venv/bin/python scripts/e2e/allocate_test_feeds.py .agents/e2e/state/test-users.json
```

Writes today's personalized `daily_feeds` for the 4 test users from the live pool using the REAL ranking code, and prints ranked lists with scores. Capture the ranked lists + the distinctness verdict for the report. Non-zero exit = orderings not distinct → red; `pool_too_thin` → AMBER (record, continue).

## Step 6 — Second pass: personalized-feed verification

Re-spawn the 4 profile agents in parallel (one message, same template as Step 3) with the driver command changed to:

```bash
npx tsx scripts/e2e/drive-profile.ts --profile <profile> --expect-personalized --steps reel_loads,personalized_feed
```

Collect results and verdicts the same way as Step 4.

## Step 7 — Human review pack

Assemble `.agents/e2e/review-pack-YYYY-MM-DD/index.md` — one section per profile:

1. **Interests selected** — what the driver clicked in the picker + the resulting DB row counts (`user_interest_profile`, `user_entity_follows`).
2. **Stories shortlisted** — ranked `daily_feeds` table: position, headline, bucket, score, matched interest.
3. **The reels** — per story: poster image embedded via markdown (`![...](url)`), audio URL link, full transcript assembled from `caption_sentences`, duration.

Plus: the driver's screenshots inline, and a **"review it yourself" recipe** — sign in with the profile's email + password from `.agents/e2e/state/test-users.json` on the running app (or the simulator after Step 8).

## Step 8 — iOS stage (gated)

Run only if the web fleet's must-pass steps are green. Otherwise mark the iOS stage **SKIPPED** with the reason, and move to Step 9. If `$ARGUMENTS` contains `skip-ios`, skip too.

```bash
npm run build:ios
xcodebuild -project ios/App/App.xcodeproj -scheme App -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17' -derivedDataPath .agents/e2e/state/ios-derived build
xcrun simctl boot "iPhone 17" 2>/dev/null || true
xcrun simctl install booted .agents/e2e/state/ios-derived/Build/Products/Debug-iphonesimulator/App.app
xcrun simctl privacy booted grant microphone com.blip.app
xcrun simctl launch booted com.blip.app
sleep 8
xcrun simctl io booted screenshot .agents/e2e/screenshots/ios-01-launch.png
sleep 20
# crash-on-boot catch: verify the app process is still alive before the steady screenshot
xcrun simctl io booted screenshot .agents/e2e/screenshots/ios-02-steady.png
```

Verify the app process is still alive after the 20s soak (`xcrun simctl spawn booted launchctl list | grep com.blip.app` or `simctl listapps`/process check) — a dead process = crash-on-boot = red, even if the launch screenshot looked fine.

Plist check on the **built** app (not the source plist):

```bash
plutil -p .agents/e2e/state/ios-derived/Build/Products/Debug-iphonesimulator/App.app/Info.plist | grep NSMicrophoneUsageDescription
```

Must show `NSMicrophoneUsageDescription`, or live voice will crash on-device.

**Honest scope note (include verbatim in the report):** this stage is boot + integrity smoke only — there is no XCUITest journey driving (standing gap-list item). Leave the simulator open so the user can sign in as a test profile and review reels on-device.

## Step 9 — Consolidated report + cleanup

Write `.agents/e2e/go-live-report-YYYY-MM-DD.md`:

- **Run metadata** — git SHA (from Step 0), story pool size, worker URL, timestamps.
- **Step×profile matrix** — every journey step × every profile: PASS / FAIL / BLOCKED / AMBER, each with a pointer to its artifact (result JSON, verdict, screenshot).
- **Evaluator summary** — audits (a)–(f) with the numbers.
- **DB-truth row counts per profile** — interests, entity follows, daily_feeds.
- **Console/network error zero-tolerance list** — every console error and failed network request captured across all runs. The target is an empty list; anything present is named.
- **iOS results** — build/install/launch status + both screenshots, or SKIPPED + reason.
- **App Store readiness gap list** — always seed with these standing items (plus anything new this run surfaced):
  - Voice tool call verified-by-proxy only (declaration + setupComplete, not an end-to-end tool roundtrip)
  - XCUITest journey gap (iOS stage is boot smoke only)
  - Magic-link email deliverability untested (test users use password auth)
  - YouTube/X ingestion status (per evaluator audit c)
  - App icons / launch screen / privacy manifest review
  - Offline behavior

Then cleanup:

```bash
npx tsx scripts/e2e/cleanup-test-users.ts   # rows only — KEEP auth users so the user can still sign in to review
```

Kill the dev server you started, and kill any harness Chrome instances — these run on CDP ports 9301–9304 ONLY; match the port in the pkill pattern (e.g. `pkill -f "remote-debugging-port=9301"` … `9304`), never a bare `pkill -f Chrome`. Do **not** kill the iOS simulator — it stays open for the user's on-device review.

## Rules

- **No commits.** This command verifies; `/commit` is a separate, explicit step (consistent with `/debug`, `/rca`).
- **Never edit app code mid-run.** Sub-agents diagnose and record; every red routes to `/debug` afterward. A go-live check that patches as it goes proves nothing about the tree it started from.
- **Fail loud (Rule 12).** A skipped step is reported as SKIPPED with a reason. An AMBER is named, not rounded up to green. Zero rows for a platform is a gap, not a pass.
- **Preserve artifacts.** Never delete `.agents/e2e/state/`, screenshots, verdicts, or review packs during cleanup — they ARE the deliverable. Cleanup touches only DB rows, the dev server, and harness Chrome.
- Per Rule 5, you are an orchestrator — drivers and sub-agents do the work; you sequence, collect, and report.
- Per Rule 10, checkpoint the step×profile matrix after Steps 4, 6, and 8 before proceeding.

## Hand off (Rule 13)

End with exactly one concrete next move:

- **All green** →
  > **Next:** review `.agents/e2e/review-pack-YYYY-MM-DD/index.md` (and the open simulator), then `/commit` the harness if not yet committed; re-run `/go-live-check` before TestFlight submission.
- **Any red** →
  > **Next:** `/debug "<failing flow + exact repro from the verdict>"` — one invocation per distinct root cause, worst first.
