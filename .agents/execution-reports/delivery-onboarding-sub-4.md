# Phase 6 — Sub-phase 4: Permissions/onboarding README + plugin packaging ⚠ irreversible

**Status: SUCCESS**

## Implemented
The §8 onboarding/permissions README (primary deliverable), finalized the public marketplace
manifest, added a repo-root README, and added structural grep tests. No code modules touched.

## Files created / modified
- **CREATED** `skills/orbit/README.md` — the brief §8 README, exact §8.1-§8.6 structure:
  - §8.1 what Orbit does (daily HTML, by creator/topic, deep-links, top TL;DR sentence).
  - §8.2 prerequisites (Python 3.12+, Node 22+, yt-dlp, browser login or env cookies).
  - §8.3 5-step setup, copy-pasteable, including the printed cron line `claude -p "/orbit"`.
  - §8.4 permissions TABLE — all five rows (cookies / filesystem / network / AppleScript /
    LLM) with both a "why we need it" and a "what we do / don't do" column.
  - §8.5 honest risk disclosure, un-softened: `auth_token` = full account access / treat
    cookies like a password; ToS-gray unofficial X reads; revocation = log out; local-only,
    no Orbit server.
  - §8.6 troubleshooting (X 404 / stale queryId, "No cookies found" / DB lock, expired
    cookies, rate-limited).
  - Cost & usage subsection: rough daily-cost-by-depth table (labeled rough estimates) +
    "start with `default`" recommendation. References `orbit.config.example.json` + `.env.example`.
- **MODIFIED** `.claude-plugin/marketplace.json` — finalized for distribution. Minimal,
  conservative changes only: tightened the plugin `description` to match the shipped M1-M4
  feature set, and added a `version: "0.1.0"` field on the plugin (mirrors SKILL.md's
  version). Kept the existing shape (name/owner/plugins[].{name,source,description,skills[]});
  invented no new fields. Still declares the `orbit` plugin + `orbit` skill at `skills/orbit`.
- **CREATED** `README.md` (repo root) — concise: what Orbit is, pointer to the full §8 doc at
  `skills/orbit/README.md`, shipped feature set (YouTube half / X half / overlap+trending+scoop
  / delivery+wizard+cron), quick-start, config + env setup (`orbit.config.example.json`,
  `.env.example`), local-only/cookies security note. Does not duplicate §8.
- **CREATED** `tests/test_readme_packaging.py` — 12 structural tests, each docstring states WHY.

## Divergences
- **Python 3.12+ vs brief's 3.11+.** README §8.2 states 3.12+ (with an explicit parenthetical
  noting the brief says 3.11+) per the instruction to match the codebase's actual 3.12 floor
  (master-plan Phase 1 pin). Intentional; flagged in the README itself.

## Review findings + fixes
- No defects found in self-review. Confirmed: marketplace `version` addition is within the
  existing schema shape (does not invent a marketplace-level field — it sits inside the plugin
  object alongside name/source/description). No secrets in any file. No marketing voice (no
  "seamlessly"/"powerful"/"robust"/"leverages"/"effortlessly"). Tone is plain; §8.5 is blunt.

## Validation
- `uv run --with pytest pytest tests/ -q` → **163 passed** (baseline 151 + 12 new). PASS.
- New file alone: 12 passed.

## Definition of done — PASS
- §8.1-§8.6 headings present (grep-tested). PASS.
- Permissions table with why + what-we-do/don't row for each of the five permissions
  (grep-tested). PASS.
- §8.5 risk lines present (auth_token / full account access, ToS-gray, log-out revocation,
  no Orbit server). PASS.
- marketplace.json parses (json.load) and declares orbit plugin + orbit skill at
  skills/orbit. PASS.
- Cost estimate + default-depth recommendation present. PASS.
- Root README exists and points at skills/orbit/README.md. PASS.

## Flagged manual-review / manual-smoke items (NOT automatically verified)
1. **README reads honestly end-to-end.** The grep tests confirm required clauses exist but
   cannot judge whether the prose reads honestly and without hand-waving cover-to-cover. This
   is the brief's designated single human-review item — needs a maintainer read-through.
2. **macOS Automation-permission UX.** The OS prompt the user must approve for AppleScript
   iMessage delivery is documented in §8.4 (note) and implied in setup, but cannot be
   unit-tested. Needs a manual smoke on a real Mac to confirm the prompt appears and the
   documented path (System Settings → Privacy & Security → Automation) is accurate.

## Concerns
- The depth-by-cost numbers ($/day) are first-cut estimates explicitly labeled as rough;
  they should be refined after the maintainer's real runs (matches the phase's open question).
- DID NOT COMMIT, per instructions. Working tree holds sub-phases 1-4 uncommitted.
