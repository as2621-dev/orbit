# docs/solutions/ — institutional memory (the compounding store)

Short, durable learnings that make future `/grab-issue` slices cheaper. The build loop
**reads** this before building (Step 2.5 learnings pass) and **writes back** after shipping
(Step 10.5 compound). That read→write cycle is the compounding flywheel — it's what makes
slice N+1 cheaper than slice N.

## How it's used

- **Read (Step 2.5):** grep-first — extract keywords from the slice's surface, search entry
  frontmatter (`tags`, `problem_type`, `symptoms`), read only the few hits. Pull forward
  constraints, known-failed approaches to avoid, and patterns to follow.
- **Write (Step 10.5):** when a slice teaches something reusable, append one entry. One fact
  per file. Skip routine slices — no filler.

## Layout (create subdirs as needed)

- `runtime-errors/` · `performance-issues/` · `security-issues/` — bug-track
- `architecture-patterns/` · `conventions/` · `tooling-decisions/` — knowledge-track
- `patterns/critical-patterns.md` — optional, must-know cross-cutting patterns

## Entry format

```markdown
---
title: <short title>
tags: [<keyword>, <keyword>]
problem_type: runtime-error | performance | security | pattern | convention | tooling
symptoms: <how it shows up>
root_cause: <the actual cause, for bug entries>
date: YYYY-MM-DD
---

<What was learned, the fix or pattern, and how to apply it next time. Link related entries.>
```

## Rule

A past learning is **signal, not gospel** — it reflects what was true when written. If an
entry names a file/function/flag, verify it still exists before relying on it; never let a
stale learning silently override present code.
</content>
</invoke>
