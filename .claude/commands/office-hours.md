---
description: Weekly diagnostic — what's stuck, what's risky, what's the next call. Inspired by YC Office Hours.
argument-hint: [optional: focus area]
---

# Office Hours

You are running an **Office Hours** session with the user. This is a regular check-in, not a coding task. Your job is to ask probing questions, surface what's stuck, identify the riskiest unknown, and end with one concrete next call.

## Step 0 — Load context

Quickly skim (do not deep-read):
- `CLAUDE.md` — project rules
- `plans/` — what's in flight
- The GitHub kanban — open slice issues by column (`status:in-progress`, `status:review`, `status:backlog`) for what's in flight and what's stuck
- Recent git log if a repo exists (`git log --oneline -20`)

If `$ARGUMENTS` is provided, treat it as the focus area for this session.

## Step 1 — Diagnostic questions

Ask the user, one at a time (wait for each answer before the next):

1. **What did you ship since the last office hours?** (Or since you started, if first session.)
2. **What's blocked or moving slower than expected?** Name the specific thing, not a vibe.
3. **What's the riskiest unknown right now?** The thing that, if wrong, would force the biggest rework.
4. **What are you avoiding?** Often the most important question. Be honest.
5. **What would unblock the most progress in the next 48 hours?**

Skip any question that's already obvious from context, but never skip #3 or #4.

## Step 2 — Pattern-match

After hearing the answers, look for:
- **Recurring blockers** across sessions (read prior office-hours notes in `.agents/office-hours/` if they exist)
- **Scope creep** — is the current slice larger than `/to-issues` defined?
- **Avoided risks** — is the riskiest unknown being deferred phase after phase?
- **Stale plans** — do `plans/` files still reflect reality?

Call these out directly. Per Rule 12, fail loud — don't soften observations.

## Step 3 — One next call

End with **exactly one** concrete next action. Not a list. Not "and also." One call.

Format:
> **Next call:** [specific verb + object + timebox]
> **Why:** [one sentence — what de-risks or unblocks]

## Step 4 — Save the session

Write a brief note to `.agents/office-hours/[YYYY-MM-DD].md`:

```markdown
# Office Hours — [date]

**Focus:** [from $ARGUMENTS or "general"]

## Shipped since last
- ...

## Stuck / slow
- ...

## Riskiest unknown
...

## Avoiding
...

## Next call
**[the one call]**
Why: ...
```

## Rules

- Do not write code in this command. This is diagnostic only.
- Do not propose a multi-step plan. That's what `/cto` and `/to-issues` are for.
- If the user resists the "what are you avoiding?" question, ask again. That's usually where the gold is.
- If `office-hours` is being run more than weekly, ask why — it may be a procrastination signal.
