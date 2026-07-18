---
description: Capture a reusable learning into docs/solutions/ — the compounding store. The canonical write recipe; other commands (/grab-issue, /rca, /debug, /codex, /improve-architecture) call this as their tail step. Run standalone for any insight that surfaced outside a command.
argument-hint: [optional: the learning, or path/issue/sha it came from — empty = infer from this session]
---

# Compound — write the learning back

This is the **write half** of the compounding flywheel. `docs/solutions/` is read before
building (`/grab-issue` B2.5 learnings pass) and written after — that read→write cycle is what
makes slice N+1 cheaper than slice N. This command owns the canonical write recipe so every
entry has the same shape no matter which command produced it.

**This is a capture, not a deliberation.** It is cheap, gated, and additive. Do not write code,
do not refactor, do not re-open the work. One durable fact in, then stop.

## Inputs

What to capture: `$ARGUMENTS`

- **With arguments** — the learning stated directly, or a pointer to where it came from
  (an `.agents/rca/*.md`, a `.agents/debug/*.md`, a `/codex` finding, an issue, a commit sha).
- **Empty** — infer the candidate learning from the current session: the non-obvious bug just
  fixed, the pattern just established, the convention just adopted, the gotcha just hit.

When invoked as another command's tail step, the calling command passes the candidate learning
in `$ARGUMENTS`.

## Step 1 — Gate: is there actually something to capture?

A learning is worth an entry only if it is **reusable and non-obvious** — it will change how a
future slice is built. Capture when the work surfaced one of:

- a **non-obvious bug** + its root cause (not a typo; something a future slice could re-hit)
- a **pattern** now established that later slices should follow
- a **convention** adopted (naming, layering, error shape, test posture)
- a **library/tooling gotcha** (an API that bites, a flag that's required, a version trap)
- a **known-failed approach** — "we tried X, it doesn't work because Y" (absence is signal)

**Skip — write nothing — if** the work was routine, the lesson is already in `CLAUDE.md` /
`reference/` / the PRD, or it only matters to this one conversation. Per Rule 2 and Rule 12, do
**not** manufacture filler. An empty `docs/solutions/` is healthier than a noisy one. If you skip,
say so in one line ("Nothing reusable to compound — routine work") and stop.

## Step 2 — Dedup before writing

Grep `docs/solutions/` for the topic (keywords, tags, the file/function involved) before creating
anything. If an entry already covers this:

- **Sharpen the existing entry** rather than adding a near-duplicate — update its body, add a tag,
  refine the root cause.
- If the new learning **contradicts** an old one (the old was stale, the code changed), per Rule 7
  pick the current truth, rewrite the entry, and note what changed + the date. Don't leave both.

Never create a second file for a fact that already has a home.

## Step 3 — Write the entry

If `docs/solutions/` (or its `README.md`) doesn't exist yet, create it from the convention below.
Then place **one fact per file** in the right subdir (create subdirs as needed):

- `runtime-errors/` · `performance-issues/` · `security-issues/` — bug-track
- `architecture-patterns/` · `conventions/` · `tooling-decisions/` — knowledge-track
- `patterns/critical-patterns.md` — optional, must-know cross-cutting patterns

Filename: `[short-kebab-slug].md`. Use this exact frontmatter (matches the store's contract and
`/grab-issue` B2.5/B10.5):

```markdown
---
title: <short title>
tags: [<keyword>, <keyword>]
problem_type: runtime-error | performance | security | pattern | convention | tooling
symptoms: <how it shows up — for bug entries>
root_cause: <the actual cause — for bug entries>
date: YYYY-MM-DD
---

<What was learned, the fix or pattern, and how to apply it next time. If an approach was tried
and failed, say so and why. Link related entries with their path.>
```

Keep the body short — a future slice skims this, it doesn't study it. State the constraint, the
fix-or-pattern, and the "next time, do X." Verbose enough to act on, not an essay (Rule 14).

## Step 4 — Confirm

End with one line: the path written (or sharpened), and the one-sentence takeaway a future slice
will read. If you skipped, the skip reason. Nothing else.

## Rules

- Per Rule 2 / Rule 12: the gate is the whole point. Reusable-and-non-obvious or nothing. Filler
  rots the store and forces a `/compound-refresh` prune later — don't create that debt.
- A past learning is **signal, not gospel** — it reflects what was true when written. When the
  entry names a file/function/flag, the reader is told (in the store README) to verify it still
  exists. Date every entry so staleness is visible.
- This command does not write code, run the build, or re-open the work. Capture only.
- Do not duplicate what `CLAUDE.md`, `reference/`, the PRD, or git history already record. If
  asked to compound one of those, capture only the non-obvious delta — or skip.

## Used as a tail step

These commands call `/compound` (or inline this recipe) at their end, gated the same way:

- `/grab-issue` B10.5 — a slice that taught something reusable
- `/rca` — the root cause + what class of bug the fix rules out
- `/debug` — the reproduction + the real cause, after fix-and-verify
- `/codex` — a non-obvious correctness/security lesson from the findings
- `/improve-architecture` — the shallow→deep pattern behind a filed RFC

`/compound-refresh` (or the prune section of `/office-hours`) is the **audit** half — it re-reads
the store against current HEAD and removes/merges stale entries. Capture here; prune there.
