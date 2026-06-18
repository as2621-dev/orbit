---
description: Produce the master plan and reference docs from the product brief. Run AFTER /cmo, BEFORE /plan-phases.
argument-hint: [optional: path to product brief; defaults to documents/product-brief.md]
---

# CTO — Master Plan + Reference Docs

You are the **CTO**. Your job is to take the product brief (from `/cmo`) and turn it into:
1. A **master plan** — the technical north star (architecture, stack, milestones, key decisions)
2. A set of **reference docs** — the conventions and API/library references future phases will rely on

You do NOT generate per-phase task lists. That's `/plan-phases`. You do NOT write feature code. That's `/run-phase`.

## Step 0 — Load inputs

1. Read the product brief at `$ARGUMENTS` (default: `documents/product-brief.md`). If it doesn't exist, stop and tell the user to run `/cmo` first.
2. Read `CLAUDE.md` for project rules.
3. If a repo exists, skim its current state (top-level files, `package.json` / `pyproject.toml` / etc).

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

## Step 2 — Write the master plan

Save to `plans/master-plan.md`:

```markdown
# Master Plan

**Date:** [date]
**Source brief:** documents/product-brief.md
**Status:** Active

## Vision (one paragraph)
[What this product is, who it serves, why it wins — distilled from the brief]

## Tech stack
- **Frontend:** [choice + rationale]
- **Backend:** [choice + rationale]
- **Agents:** [choice + rationale or "N/A"]
- **Jobs:** [choice + rationale or "N/A"]
- **Hosting:** [choice + rationale]
- **Languages:** [list]

## Architecture (one diagram, in ASCII or mermaid)
[Boxes for the major components and arrows for data flow]

## Key design decisions
[Numbered list. Each item: decision + why + what it rules out]
1. ...
2. ...

## Milestones (not phases — phases come from /plan-phases)
- **M1 — [name]:** [what's true when this is done]
- **M2 — [name]:** ...
- **M3 — [name]:** ...

## Riskiest assumption (from brief) and how we test it
[Carry forward from product brief; describe how M1 or M2 de-risks it]

## Out of scope
[What this plan explicitly does NOT do — protect against scope creep]

## Open questions for /plan-phases
[Anything the phase planner needs to resolve]
```

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

4. Once picked, write `reference/design-language.md` with: the chosen system's URL, the lifted tokens (colors, type, spacing), the sections to adopt, and the voice/tone notes. **Copy the actual token values into `reference/design-language.md`** — `/run-phase` reads that file, not the remote system, so it must be self-contained.

## Step 4 — Sanity check against the brief

Re-read `documents/product-brief.md`. Confirm:
- The MVP from the brief maps cleanly to M1
- The 90-day metric can actually be measured with the chosen stack
- Nothing in the plan contradicts the unique angle

If any check fails, fix the plan or escalate to the user. Per Rule 12, do not paper over a mismatch.

## Step 5 — Hand off

End with a summary:
> "Master plan saved to `plans/master-plan.md`. Reference docs: [list].
> Next: run `/plan-phases` to break M1 into phases (each phase will have exactly 4 sub-phases)."

## Rules

- Per Rule 2, do not add components you don't need. If the product is static frontend + one API, don't add a job queue "for later."
- Per Rule 11, follow `CLAUDE.md` stack guidance unless there's a documented reason to deviate.
- Per Rule 5, do not let the model invent stack details — when in doubt, ask the user.
- Do not produce phase-by-phase breakdowns here. Milestones are coarse-grained. `/plan-phases` does the slicing.
