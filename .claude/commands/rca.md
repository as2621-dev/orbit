---
description: Root-cause analysis for a bug or incident, then propose a fix. Does not commit — hand off to /grab-issue or apply manually.
argument-hint: [bug description, error message, or path to a failing test]
---

# RCA — Root Cause Analysis

You are doing **root-cause analysis**, not patching symptoms. Find the actual cause, then propose the smallest fix that addresses it. Do not write the fix into the codebase in this command — surface the diagnosis and proposed change, let the user decide.

## Inputs

The reported issue: `$ARGUMENTS`

If `$ARGUMENTS` is empty or vague, ask the user for:
- The exact error message or symptom
- Steps to reproduce
- When it started (if known)
- Whether it's deterministic or flaky

Do not guess. Per Rule 1, if you don't have a reproducer, ask for one.

## Step 1 — Reproduce

Reproduce the issue locally before anything else. If you can't reproduce, stop and tell the user — RCA on an unreproduced bug is fortune-telling.

If reproduction requires environment setup (services, env vars, fixtures), spell out what's needed and ask the user to confirm.

## Step 2 — Build the trace

Trace the failure backward from the symptom to the cause:

1. **Symptom:** what's observed
2. **Proximate cause:** the line/condition that directly produced the symptom
3. **Why that line behaves that way:** the next layer back — what input, state, or decision led to it
4. **Continue until you hit the actual cause:** a wrong assumption, a missing guard, a contract violation, a race, etc.

Per Rule 8, read each function on the trace before drawing conclusions. Don't skip layers.

## Step 3 — Classify

Pick exactly one:
- **Logic bug** — the code does the wrong thing given correct inputs
- **Contract violation** — caller and callee disagree on inputs/outputs
- **State / race** — order-of-operations or concurrency
- **Environmental** — config, env var, dependency version, infra
- **Data** — bad/unexpected input that wasn't validated
- **Design** — the architecture allowed this; a local fix won't really prevent recurrence

The classification matters because the fix shape differs. Per Rule 7, if it's two of these blended (common), name the dominant one and flag the secondary.

## Step 4 — Propose the smallest fix

State the fix in this shape:

```
**Fix:** [one-sentence change]
**Files:** [paths]
**Why this is the root cause and not a symptom:** [one paragraph]
**What this fix rules out:** [the class of bug it prevents from recurring]
**What this fix does NOT address:** [related issues left for later]
```

Per Rule 2, prefer the smallest change that addresses the root cause. Per Rule 3, do not bundle unrelated cleanup.

If the right fix is large (design-class root cause), say so explicitly. Don't pretend a small patch fixes a design bug — Rule 12.

## Step 5 — Write the regression test FIRST (proposal only)

Per Rule 9, propose a test that would have caught this — one that fails on the current (buggy) code and passes after the fix. Show the test code. Do not run it yet; the user decides whether to apply.

## Step 6 — Save the RCA

Save to `.agents/rca/[YYYY-MM-DD]-[short-slug].md`:

```markdown
# RCA: [short title]

**Date:** [date]
**Reporter:** user / monitoring / test failure
**Status:** Diagnosed — fix proposed, not applied

## Symptom
[what was observed]

## Reproduction
[steps]

## Trace
[symptom → proximate cause → ... → root cause]

## Classification
[one of: logic / contract / state / environmental / data / design]

## Root cause
[the actual cause, one paragraph]

## Proposed fix
[file paths + summary of change]

## Regression test
[the test that would have caught this — code block]

## What this fix does NOT address
[related issues, design smells, etc.]

## Follow-ups
[if any — link to issues or future phases]
```

## Step 6.5 — Compound the root cause

If the root cause is **reusable and non-obvious** — a class of bug a future slice could re-hit, a
contract trap, a missing-guard pattern — capture it with `/compound` (gated; pass the root cause +
what the fix rules out). Skip routine causes; don't manufacture filler (Rule 2 / Rule 12). The
saved `.agents/rca/*.md` is the full record; `/compound` distills the one durable lesson into
`docs/solutions/` so `/grab-issue` B2.5 reads it next time.

## Step 7 — Hand off

End with:
> "RCA saved to `.agents/rca/[file].md`. To apply the fix, either:
> - Apply it manually (small fix), then `/commit`
> - File it as a new slice issue, then `/grab-issue`"

## Rules

- Per Rule 1, if you can't reproduce, do not guess. Ask for more info.
- Per Rule 12, do not declare a root cause you're not confident in. "Possibly related to X" is not a root cause — keep tracing.
- Per Rule 2, the proposed fix should be the smallest one that addresses the root cause. If you find yourself proposing a refactor, the root cause is probably "design" — say so.
- Do not apply the fix in this command. RCA diagnoses; `/grab-issue` or manual edits + `/commit` apply.
