---
description: Break the PRD into independently-grabbable vertical-slice issues and publish them to GitHub Issues as a kanban backlog. Run AFTER /cto.
argument-hint: [optional: path to PRD; defaults to plans/prd.md]
---

# To Issues — PRD → Vertical-Slice Kanban

Adapted from Matt Pocock's `to-issues`. Break the PRD into **tracer-bullet vertical
slices** and publish each as a **GitHub Issue**, forming a kanban backlog. No phases —
thin end-to-end slices instead.

## Step 0 — Load inputs

1. Read the PRD at `$ARGUMENTS` (default `plans/prd.md`). If missing, stop and tell the
   user to run `/cto` (the PRD now ships from `/cto`).
2. Read `CLAUDE.md` and relevant `reference/` docs for the domain glossary and conventions
   (Rule 11 — issue titles use the project's vocabulary). The PRD's Technical Foundation
   section is the architecture context — there is no separate master plan. Note its
   **Module contracts** (per-deep-module requirements + edge cases): for each slice, the
   contracts of the modules it touches become the slice's edge-case acceptance criteria.
3. If the repo has code, skim it for the current state and **prefactoring opportunities**:
   "make the change easy, then make the easy change."

## Step 1 — Ensure the kanban labels exist

The board is GitHub Issue **labels**. Before publishing, ensure these exist on the repo
(create any that are missing):

- `status:backlog` · `status:in-progress` · `status:review` · `status:done` — the columns
- `ready-for-agent` — triage: this slice is fully specced and an agent can grab it
- `blocked` — has an open blocker
- `slice` — marks a tracer-bullet issue from this command

Use the GitHub MCP tools (search ToolSearch for `mcp__github__` label/issue tools). The
target repo is the one in session scope.

## Step 2 — Draft vertical slices (tracer bullets)

Break the PRD into slices. **Each slice cuts through EVERY layer end-to-end** — not a
horizontal slice of one layer.

<vertical-slice-rules>
- Each slice delivers a narrow but COMPLETE path through every layer (data → logic → UI → tests)
- A finished slice is demoable / verifiable on its own
- Prefactoring slices come first
- Aim for the fewest seams — one is ideal
- **Each slice must fit one subagent inside a 120k-token budget** (see sizing gate below)
</vertical-slice-rules>

Bad (horizontal): "Build the database schema." / "Build all the API endpoints."
Good (vertical): "A user can sign up with email and see an empty dashboard" — touches
schema + endpoint + UI + one test, demoable end-to-end.

### Sizing gate — one slice = one subagent ≤ 120k tokens

Every slice is built by a **single dedicated subagent** (via `/grab-issue`) that gets its
own fresh context. That context has to hold: the issue body, `CLAUDE.md` + the relevant
`reference/` docs, the existing files it must read, the new code it writes, and the
test/validation loop. Budget that at **~120k tokens, hard ceiling**.

For each drafted slice, estimate the footprint before publishing:

- How many existing files must the builder read to do this? (rough: ~1k tokens per ~400
  lines)
- How much new code + tests will it write?
- How chatty is the validate/fix loop likely to be (lint/typecheck/test output)?

If a slice plausibly blows past ~120k, **split it** into smaller vertical slices (still
end-to-end, just thinner — e.g. "happy path only" then "validation + error states" as a
follow-on) and record the dependency. If a slice can't be made to fit without going
horizontal, that's a finding about the PRD/architecture — surface it (Rule 12), don't ship
a slice you know won't fit. Note the rough token estimate in the issue body so the builder
knows its budget.

## Step 3 — Quiz the user before publishing

Present the breakdown as a numbered list. For each slice show:

- **Title** — short, in domain vocabulary
- **Blocked by** — which slices must finish first (if any)
- **User stories covered** — which PRD stories this delivers

Then ask:
- Does the granularity feel right (too coarse / too fine)?
- Are the dependencies correct?
- Merge or split any?

Iterate until the user approves. Do NOT publish before approval (Rule 1).

## Step 4 — Publish to GitHub Issues, in dependency order

Publish **blockers first** so you can reference real issue numbers in "Blocked by."
For each approved slice, open a GitHub Issue:

- **Labels:** `slice`, `status:backlog`, and `ready-for-agent` (unless it has an open
  blocker → add `blocked` and omit `ready-for-agent` until the blocker closes).
- **Body** — use this template:

```markdown
## What to build
[End-to-end behavior of this slice. Describe the demoable outcome, not layer-by-layer
implementation. No file paths or code snippets — they go stale. Exception: a tiny
decision-encoding snippet from a prototype (schema, type shape, state machine) is fine.]

## Acceptance criteria
[Observable, testable behaviors — these are the test specs the builder drives red-green
against. Expand them from the **Module contracts** in the PRD's Technical Foundation: for
every deep module this slice touches, pull in its requirements and edge cases (empty input,
conflict, concurrent access, failure, boundaries) as concrete criteria. Prose/checkboxes
only — NOT executable tests; `/grab-issue` writes those at build, one behavior at a time.]
- [ ] Happy path: [the core behavior this slice delivers] (fails if business logic is wrong, per Rule 9)
- [ ] Edge case: [from the touched module's contract]
- [ ] Edge case: [from the touched module's contract]
- [ ] Error/boundary: [what must happen when input is bad / a dependency fails]

## User stories covered
- PRD story #N: As a …, I want …, so that …

## Blocked by
- #<issue-number> (or "None — can start immediately")

## Build budget
~[estimate]k tokens — one subagent, ceiling 120k. If the builder is approaching the
ceiling, it should stop and split the remainder into a follow-on issue (Rule 6), not
silently overrun.
```

After publishing, post one comment on the PRD issue (or print) listing the created issue
numbers and the dependency order.

## Step 4b — Add the slices to the visual board (GitHub Projects)

So the user has a board they can actually *see*, add every published slice to a **GitHub
Project** (the native kanban view over these same issues — nothing to host):

- If the repo/user has no Project yet, create one (a simple board with a single-select
  **Status** field whose options match our columns: Backlog · In progress · Review · Done),
  then tell the user the board URL.
- Add each new slice issue as an item and set its Status to **Backlog**.
- Keep the `status:*` labels as the source of truth; the Project's Status field mirrors
  them. `/grab-issue` already moves the labels — note in the board that dragging a card and
  relabeling are two views of the same state.

Use the GitHub MCP tools (search ToolSearch for Projects / issue tools). If Projects isn't
reachable, don't block — the labels still form the board; just tell the user the labels are
live and the Project view can be added later.

## Step 5 — Hand off

End with:
> "Published [N] vertical-slice issues to the backlog (`status:backlog` + `ready-for-agent`).
> Dependency order: #a → #b → #c.
> **Next:** run `/grab-issue` to pull the top unblocked slice and build it end-to-end."

## Rules

- Per Rule 2, don't pad slices to hit a number. Ship the fewest that cover the PRD.
- Per Rule 3, each slice is surgically scoped — a thin path, not "refactor module X."
- Per Rule 9, acceptance criteria must be checkable and must fail when intent is violated,
  not just when code won't compile.
- Do NOT close or modify any parent/PRD issue here. Only create slice issues.
- Per Rule 12, if the PRD can't be sliced thinly (every path needs everything), say so —
  that's a finding about the PRD, not a reason to ship one giant issue.
