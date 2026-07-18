---
description: Scan the codebase for deepening opportunities (shallow → deep modules), report them in plain language, file refactor RFCs as backlog slices, and sync the PRD Technical Foundation + reference docs. Run every few days.
argument-hint: [optional: a path or area to focus the scan]
---

# Improve Architecture — Deepening Review + Doc Sync

Adapted from Matt Pocock's `improve-codebase-architecture`. Find where the codebase is hard
to understand, hard to test, or hard for an agent to navigate, and propose **deepening**:
turning *shallow* modules (callers must learn almost as much as the implementation knows)
into *deep* ones (lots of useful behavior behind a small, stable interface). Deep modules
are how you *simplify* — less for everyone downstream to hold in their head.

Two jobs:
1. Propose concrete refactors and file them as backlog slices the team can grab.
2. **Keep the CTO docs honest** — fold accepted architectural direction back into
   `plans/prd.md` (Technical Foundation) and `reference/` so the north star never drifts
   from the real code.

## Shared vocabulary (use these exact words — vague words make vague refactors)

**module · interface · depth · seam · adapter · leverage · locality.** Avoid mushy words
like "component" or "service." A change has **leverage** if it makes many future changes
easier; it improves **locality** if related logic stops being scattered across files.

**Deletion test** for a suspected shallow module: if deleting it would *concentrate*
complexity (good) rather than just *relocate* it elsewhere (neutral), deepening is warranted.

## The user is NOT technical

Architecture work is technical, but the user is not (see `/brainstorm`). So:
- Do the technical work in **subagents**; keep the user-facing surface in plain language.
- Describe every opportunity as: *what's tangled now → what changes → why it makes future
  work cheaper / safer / faster.* No jargon in what you show the user.
- The user's only decisions are **which opportunities to pursue** and approving doc changes.

## Step 0 — Load context

Read `CLAUDE.md`, `plans/prd.md` (its **Technical Foundation** section is the current
architecture of record — there is no separate master plan), and `reference/conventions.md`
+ `reference/stack-notes.md` if present.

**If there's no application code yet, stop.** Tell the user: architecture is designed
proactively inside `/cto`; there's nothing to review until the first slices have landed.
Come back after a few `/grab-issue` runs.

## Step 1 — Explore for deepening opportunities

Spawn one or more **`Explore` agents** (read-only) to walk the codebase organically — scope
to `$ARGUMENTS` if the user named an area. Have them surface friction:

- One concept you must chase across many tiny modules to understand (poor locality)
- Shallow modules whose interface is nearly as complex as their implementation
- Functions extracted *only* to make a test pass, hiding the real behavior (and real bugs)
- Tightly-coupled modules leaking across a seam that should be clean
- Anything untested because it's hard to test (a hard-to-test shape is usually a design smell)

Apply the deletion test to each candidate. Per Rule 8, read the actual exports and callers
before judging — don't flag on file names alone.

## Step 2 — Report the candidates

Present the opportunities as a numbered list (and, if it helps the user, write a
self-contained HTML report to the session scratchpad with before/after sketches — never to
the workspace). For each candidate show, in plain language:

- **Files** — which modules are involved
- **Problem** — why today's shape causes friction (locality / depth / seam)
- **Solution** — what would change, in one or two plain sentences
- **Payoff** — the future work it makes cheaper or safer (leverage), and what gets simpler

Rank by leverage. Per Rule 2, prefer the few high-leverage deepenings over a long list of
cosmetic ones. If nothing real surfaces, say so plainly (Rule 12) — don't manufacture work.

## Step 3 — Let the user pick, then pressure-test it

Ask which opportunities to pursue (plain language). For each chosen one, think through the
constraints before proposing the refactor — what callers depend on the current seam, what
could break, how it gets verified. If a choice is genuinely a judgment call for the user
(a trade-off they should own), surface it; otherwise decide and explain (Rule 5).

## Step 4 — File refactor RFCs as backlog slices

For each approved deepening, publish a GitHub Issue onto the same kanban backlog
(`/to-issues` conventions), labelled `slice`, `refactor`, `status:backlog`, and
`ready-for-agent` (or `blocked` + a `Blocked by` if it must follow another). Reuse the
issue-body template **including the `## Build budget` line** — each refactor must fit one
subagent under the **120k-token ceiling** (Rule 6); split if it won't.

Prefactor first: "make the change easy, then make the easy change." If a deepening unblocks
other slices, note that in their issues.

## Step 5 — Sync the CTO docs (the loop back to /cto)

This is what makes the review repeatable. For every **accepted** architectural direction,
make a **surgical** update (Rule 3 — touch only what changed):

- **`plans/prd.md` → Technical Foundation:** update the architecture diagram and the "Key
  design decisions" list to match the new direction. Add the new decision; mark any
  superseded one as superseded (don't silently rewrite history).
- **`reference/conventions.md` / `reference/stack-notes.md`:** capture any new seam,
  adapter boundary, or pattern future builders must follow.
- **Rejected** opportunities worth remembering: record the reason as a short ADR note under
  `reference/adr/` (create the folder only if you write one) so it isn't re-proposed every
  few days.

Per Rule 7, if a new decision contradicts an existing doc, pick one, update it, and flag the
other — do not leave both in place.

## Step 5.5 — Compound the pattern

When an accepted deepening reflects a **reusable shallow→deep pattern** — a seam worth repeating, a
class of tangle to avoid — capture it with `/compound` (gated; one entry per distinct pattern, not
one per slice filed). Step 5 syncs the *decision* into `plans/prd.md` + `reference/`; `/compound`
distills the transferable *pattern* into `docs/solutions/` so `/grab-issue` B2.5 applies it on the
next build. Skip if the refactor was local and taught nothing general (Rule 2 / Rule 12).

## Step 6 — Hand off

End with a summary:
> "Reviewed [area]. Filed [N] refactor slices: #a, #b. Synced Technical Foundation in
> `plans/prd.md` and [reference docs]. [k] opportunities parked (ADR notes).
> **Next:** run `/grab-issue` to build the highest-leverage refactor, or re-run
> `/improve-architecture` in a few days."

## Rules

- Per Rule 2, deepening is *simplification* — if a proposed refactor adds abstraction
  without removing downstream load, it's not a deepening; drop it.
- Per Rule 9, every refactor RFC's acceptance criteria must include "behavior is unchanged"
  with a real check (existing tests still pass / characterization test added) — a refactor
  that can silently change behavior is mis-specced.
- Per Rule 12, never report the architecture as "clean" to flatter; if it's tangled, say so
  and show where.
- Do not start refactoring here. This command *finds and files*; `/grab-issue` *builds*.
