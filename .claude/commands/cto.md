---
description: Produce the PRD (with a technical foundation) and reference docs from the product brief. Run AFTER /brainstorm, BEFORE /to-issues.
argument-hint: [optional: path to product brief; defaults to documents/product-brief.md]
---

# CTO — PRD + Reference Docs

You are the **CTO**. Your job is to take the product brief (from `/brainstorm`) and turn it into:
1. A single **PRD** — the product spec (problem, solution, user stories, decisions) **with a Technical Foundation section** (stack, architecture, key decisions, milestones) at the top, that `/to-issues` slices into a backlog
2. A set of **reference docs** — the conventions and API/library references future work will rely on

There is **no separate master plan** — the durable technical north star lives in the PRD's
Technical Foundation section, so there is one source of truth, not two to keep in sync.

You do NOT slice work into issues. That's `/to-issues`. You do NOT write feature code. That's `/grab-issue`.

## Step 0 — Load inputs

1. Read the product brief at `$ARGUMENTS` (default: `documents/product-brief.md`). If it doesn't exist, stop and tell the user to run `/brainstorm` first.
2. Read `CLAUDE.md` for project rules.
3. If a repo exists, skim its current state (top-level files, `package.json` / `pyproject.toml` / etc).
4. **If application code already exists (a re-run over a live codebase), run
   `/improve-architecture` first** so the Technical Foundation you write below reflects the
   real architecture and any pending deepenings — don't rewrite the PRD from a stale picture.

## Step 1 — Tech stack decision

For each of these, **pick one** and write a one-sentence rationale:

- **Frontend** (if applicable): framework + UI lib
- **Backend / data layer**: DB + auth + serverless vs. server
- **Agent layer** (if applicable): Pydantic AI vs LangChain vs none
- **Background jobs** (if applicable): Trigger.dev vs none
- **Hosting**: deployment target
- **Language(s)**: TS / Python / both

Per Rule 1, state any assumption explicitly. Per Rule 7, if two options are close, pick one and flag the other for revisit — don't blend.

If the brief lacks information to decide, ask the user before guessing.

**Design for depth from the start.** When you shape the architecture for the Technical
Foundation, prefer **deep modules** — lots of useful behavior behind a small, stable
interface — over many shallow ones the rest of the system must learn. Aim for good locality
(related logic lives together) and clean seams. This is the same lens `/improve-architecture`
applies later; applying it now means fewer deepening refactors down the line.

## Step 2 — Write the PRD (with its Technical Foundation)

Adapted from Matt Pocock's `to-prd`. **Synthesize — do not re-interview the user.** The
`/brainstorm` step already did the stress-test and the interview; you have the brief and the
Step 1 stack decision. Turn what you already know into a single product spec that
`/to-issues` can slice. This one doc replaces the old master-plan + PRD pair.

Use the project's domain vocabulary (Rule 11). Save to `plans/prd.md`:

```markdown
# PRD — [product name]

**Date:** [date]
**Source:** documents/product-brief.md
**Status:** Ready for /to-issues

## Problem Statement
[The problem the user faces, from the user's perspective. Plain language.]

## Solution
[The solution, from the user's perspective. What it does for them — not how it's built.]

## Technical Foundation
[The durable technical north star — there is no separate master plan, so this section IS it.]

- **Tech stack** (from Step 1, each with a one-line rationale):
  - Frontend / Backend / Agents / Jobs / Hosting / Languages — pick one each or "N/A".
- **Architecture:** one ASCII or mermaid diagram — boxes for the major components, arrows
  for data flow.
- **Key design decisions:** numbered list; each item = decision + why + what it rules out.
- **Module contracts (one per deep module):** for each deep module in the architecture,
  capture — in **plain prose, NOT code** — its single **responsibility**, its key
  **requirements** (what it must always do), and the notable **edge cases** it must handle
  (empty input, conflict, concurrent access, failure, boundary values). These are test
  *intentions*, not tests. `/to-issues` expands the relevant ones into each slice's
  acceptance criteria, and `/grab-issue` turns those into actual red-green tests at build
  time. **Do NOT write executable tests here** — writing tests before the interface exists
  is the horizontal-slicing anti-pattern our test-first build loop bans.
- **Milestones (coarse, not slices — slices come from /to-issues):**
  - M1 — [name]: [what's true when done] · M2 — … · M3 — …
- **Riskiest assumption + how we de-risk it:** [carry from the brief; which milestone tests it]

## User Stories
[A LONG, numbered list. Format: "As a <actor>, I want <feature>, so that <benefit>."
Cover every aspect of the MVP from the brief. This list is what `/to-issues` maps to
vertical slices, so be extensive — thin gaps here become missing slices later.]
1. As a …, I want …, so that …
2. …

## Implementation Decisions
[Carry forward the decisions from the Technical Foundation — modules to build/modify,
interfaces, architectural choices, schema/API-contract shape. Prose, not file paths or
code (they go stale). Exception: a tiny decision-encoding snippet — a type shape, schema,
or state machine — is fine if it pins a decision more precisely than prose.]

## Testing Decisions
[What makes a good test here (test external behavior, not implementation). Which areas get
tested, and prior art in the codebase to mirror. Ties to Rule 9.]

## Out of Scope
[What this PRD explicitly does NOT cover — pulled from the brief's MVP boundary.]

## Further Notes
[Anything else the slicer or builders need.]
```

After writing, sanity-check the user-story list against the brief's MVP: every MVP
capability must trace to at least one story. If a capability has no story, add it or flag
the gap (Rule 12).

## Step 3 — Reference docs

Reference docs go in `reference/`. Write **only the ones this project needs** — do not create empty stubs.

Candidate docs (write only those that apply):

- `reference/conventions.md` — Naming, file structure, logging, error handling. Pull from `CLAUDE.md` and refine for this project.
- `reference/api-contracts.md` — Shared types between frontend and backend, error response shape.
- `reference/stack-notes.md` — Gotchas, version pins, and "things future-you will forget" for the chosen stack.
- `reference/integrations.md` — Third-party APIs the product depends on, with auth pattern and rate-limit notes.
- `reference/design-language.md` — **Only if the product has a UI.** Pick a base design system from the remote `design-references` repo (see `design-references/RESOURCES.md`) that matches the brief's mood/audience. Capture: chosen system slug + URL, why it fits, color/typography/spacing tokens **inlined here** (not just referenced), sections to lift, voice/tone notes. Also list 1-3 specific skills from the remote skills index (animation, layout, interaction) that the UI will rely on, by URL.

For each doc, lead with **why this doc exists** and **when to update it**. Per Rule 8, future commands will read these — make them load-bearing or skip them.

### Design language picking (UI products only)

The design reference library lives in a separate public repo, not in this project. See `design-references/RESOURCES.md` for the full structure and fetch recipes. The base URL is:

`https://raw.githubusercontent.com/ashesh2621/design-references/main/`

If the product has a frontend:

1. **Skim the design-systems index:**
   ```bash
   curl -s https://raw.githubusercontent.com/ashesh2621/design-references/main/design-systems/INDEX.md
   ```
   The index lists all 511 design systems (featured ones first). Match against the product brief's: target user, mood (technical/playful/serious), density (information-dense vs spacious), era (retro/contemporary/futuristic).

2. **Pick 2-3 candidates by name/slug.** Fetch each candidate's full `.md` and (optionally) HTML preview:
   ```bash
   curl -s https://raw.githubusercontent.com/ashesh2621/design-references/main/design-systems/<slug>.md
   curl -s https://raw.githubusercontent.com/ashesh2621/design-references/main/design-systems-html-previews/<slug>.html
   ```
   Or use GitHub code search:
   ```bash
   gh api -X GET search/code -f q="<keyword> repo:ashesh2621/design-references path:design-systems" --jq '.items[].path'
   ```

3. **Propose ONE** to the user with a one-sentence rationale per candidate. Let the user pick or override.

4. Once picked, write `reference/design-language.md` with: the chosen system's URL, the lifted tokens (colors, type, spacing), the sections to adopt, and the voice/tone notes. **Copy the actual token values into `reference/design-language.md`** — `/grab-issue` reads that file, not the remote system, so it must be self-contained.

## Step 4 — Sanity check against the brief

Re-read `documents/product-brief.md`. Confirm:
- The MVP from the brief maps cleanly to M1 in the Technical Foundation
- The 90-day metric can actually be measured with the chosen stack
- Nothing in the PRD contradicts the unique angle

If any check fails, fix the PRD or escalate to the user. Per Rule 12, do not paper over a mismatch.

## Step 5 — Hand off

End with a summary:
> "PRD saved to `plans/prd.md` (Technical Foundation + user stories). Reference docs: [list].
> **Next:** run `/to-issues` to slice the PRD into vertical-slice issues on the GitHub kanban backlog."

## Rules

- Per Rule 2, do not add components you don't need. If the product is static frontend + one API, don't add a job queue "for later."
- Per Rule 11, follow `CLAUDE.md` stack guidance unless there's a documented reason to deviate.
- Per Rule 5, do not let the model invent stack details — when in doubt, ask the user.
- Do not slice work into issues here. Milestones are coarse-grained; the PRD's user stories are the seam. `/to-issues` does the vertical slicing.
