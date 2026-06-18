---
description: Generate N phases from the master plan. Each phase has exactly 4 sub-phases. Run AFTER /cto.
argument-hint: [optional: milestone name from master-plan.md — defaults to next incomplete milestone]
---

# Plan Phases

You are the phase planner. Given the master plan, you produce **N phase files**, where each phase has **exactly 4 sub-phases**. Each sub-phase is a unit of work that `/run-phase` can execute in a single fresh-context sub-agent.

## Step 0 — Load inputs

1. Read `plans/master-plan.md`. If missing, stop and tell the user to run `/cto`.
2. Read `CLAUDE.md` and any reference docs in `reference/`.
3. List existing phase files in `plans/` (pattern: `phase-*.md`) so you don't collide on numbering.
4. Determine target milestone:
   - If `$ARGUMENTS` is provided, use that milestone.
   - Otherwise, pick the first milestone in the master plan with no corresponding phase files.

## Step 1 — Decompose the milestone into phases

Slice the chosen milestone into **N phases** where N is the smallest number that makes each phase:
- Independently shippable / verifiable
- Bounded to a clear file region (don't touch the whole repo per phase)
- Achievable in one `/run-phase` session

N is usually 1-4. **If N > 4, the milestone is too big — escalate to the user.** Don't silently expand scope.

For each phase, you'll generate one file at `plans/phase-[N]-[slug].md`.

## Step 2 — For each phase, design exactly 4 sub-phases

Each phase has **exactly 4 sub-phases** (not 3, not 5). Sub-phases are ordered and may depend on earlier sub-phases within the same phase. If you can't think of 4 sub-phases:
- The phase is too small — merge it with a neighbor
- Or split a sub-phase that's actually two things

If you genuinely have only 3 things to do, do NOT pad. Stop and propose a 3-sub-phase phase to the user — Rule 12 (fail loud) over forced symmetry.

For each sub-phase, define:
- **Name:** one short verb phrase
- **Files touched:** explicit paths; tight boundary
- **What ships:** observable outcome, not internal detail
- **Definition of done:** the test/check that proves it works
- **Dependencies:** which earlier sub-phases must complete first

## Step 3 — Write the phase files

For each phase, save to `plans/phase-[N]-[slug].md`:

```markdown
# Phase [N]: [name]

**Milestone:** [from master plan]
**Status:** Not started
**Estimated effort:** [S / M / L]

## Goal
[One sentence — what's true when this phase is done]

## Sub-phases

### Sub-phase 1: [name]
- **Files touched:** `path/to/x.ts`, `path/to/y.ts`
- **What ships:** [observable outcome]
- **Definition of done:** [the check that proves it works]
- **Dependencies:** none

### Sub-phase 2: [name]
- **Files touched:** ...
- **What ships:** ...
- **Definition of done:** ...
- **Dependencies:** Sub-phase 1

### Sub-phase 3: [name]
- ...

### Sub-phase 4: [name]
- ...

## Phase-level definition of done
[The single check that proves the whole phase shipped — what `/run-phase` validates at the end before the commit]

## Out of scope
[What this phase explicitly does NOT do]

## Open questions
[Anything that needs the user to decide before /run-phase]
```

## Step 4 — Update master plan

Append a "Phases" section (or update it) in `plans/master-plan.md` linking each generated phase file under its milestone. Keep the format tight:

```markdown
## Phases

### M1 — [name]
- [Phase 1](phase-1-foo.md) — [one-line summary]
- [Phase 2](phase-2-bar.md) — [one-line summary]
```

## Step 5 — Self-critique (3 lenses, single pass)

Before saving anything as final, re-read your generated phases through three lenses **in this order**. For each lens, list findings explicitly. If any lens turns up a P0/P1 issue, fix it and re-run that lens. Per Rule 12, do not silently accept a failed lens — surface and resolve.

### Lens 1 — Product (CMO hat)
Re-read `documents/product-brief.md`, then ask of the phases:
- Does the **MVP from the brief** ship by the end of these phases? Trace each MVP capability to a sub-phase that delivers it.
- Does any phase chase a feature **outside the brief**? Scope creep is a P0 — cut it.
- Does the **riskiest assumption** from the brief get tested in the **first phase**? If it's deferred to phase 3+, that's wrong — re-order.
- Does the 90-day metric become measurable by phase end? If not, what's missing?

### Lens 2 — Engineering (CTO hat)
Re-read `plans/master-plan.md` and reference docs, then ask:
- Does any sub-phase touch files **outside the chosen tech stack** (e.g., a sub-phase implies a service the master plan doesn't include)? Either fix the phase or escalate to update the master plan.
- For each sub-phase: is the **definition of done** something a sub-agent in a fresh context could actually verify? "Works end-to-end" is not checkable; "POST /api/x returns 201 and row exists in users table" is.
- Sub-phase 4 of each phase: does it **lock in choices** made by sub-phases 1-3 that should stay flexible? If sub-phase 4 cements an API shape early, consider re-ordering.
- Are any two sub-phases within the same phase **secretly the same thing**? Merge them and split the real fourth out.

### Lens 3 — Risk (the "what blows up" hat)
- **File boundary conflicts:** any two sub-phases within a phase touching the same file? If yes, mark the dependency explicitly or merge.
- **Test coverage:** does each sub-phase's definition of done include a test (per Rule 9)? "Manual smoke" is fine for UI but flag it.
- **Reversibility:** does any sub-phase make an **irreversible change** (DB migration, public API, data deletion)? Flag with a `⚠ irreversible` marker so `/run-phase` proceeds with extra care.
- **The "painting into a corner" check:** simulate executing sub-phases 1 → 2 → 3 mentally. Does sub-phase 4 still work given the state left by 3? If not, re-order or restructure.

### Critique output

After all three lenses, append a `## Self-critique` section to **each** generated phase file:

```markdown
## Self-critique

**Product lens:** [PASS / findings + how addressed]
**Engineering lens:** [PASS / findings + how addressed]
**Risk lens:** [PASS / findings + how addressed]
**Irreversible sub-phases:** [list, or "none"]
```

If the critique forces you to **regenerate** phases, do so — and re-run the critique on the new version. Do not save phase files that have unresolved P0/P1 findings.

Per Rule 9, the bar is: "definition of done" should fail if business logic is wrong, not just if code compiles. Per Rule 2, if the critique reveals the phases are too big or too small, fix the slicing — don't paper over.

## Step 6 — Hand off

End with:
> "Generated [N] phase files in `plans/`. To execute one, run `/run-phase plans/phase-1-[slug].md`."

## Rules

- Per Rule 2, don't invent sub-phases to hit 4. If you've got 3, surface it.
- Per Rule 3, sub-phases should be surgically scoped. "Refactor module X" is not a sub-phase — pick which function.
- Per Rule 1, if the master plan is ambiguous about how a milestone breaks down, ask before guessing.
- `/run-phase` will spawn one sub-agent per sub-phase, so each sub-phase must be self-contained enough for a fresh-context agent to execute given only the phase file and `CLAUDE.md`.
