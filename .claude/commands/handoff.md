---
description: Compact the current conversation into a handoff document so a fresh agent (or future you) can pick up the work.
argument-hint: [optional: what the next session will focus on]
---

# Handoff — Compact for the Next Agent

Adapted from Matt Pocock's `handoff`. Write a handoff document summarizing the current
conversation so a fresh-context agent can continue without re-deriving everything.

## Where to save

Save to the session scratchpad / OS temp directory — **not** the project workspace, so it
never gets committed by accident. Use the scratchpad path this session provides. Tell the
user the exact path at the end.

## What to include

Keep it dense (Rule 14). Do NOT duplicate content that already lives in artifacts — link to
it by path or URL instead (PRD, ADRs, GitHub issues, commits, diffs).

```markdown
# Handoff — [date]

## Focus of the next session
[If the user passed an argument, tailor this to it. Otherwise: the immediate next goal.]

## Where things stand
[2-4 sentences: what's done, what's in flight, what's verified vs assumed.]

## Active artifacts (by reference, not copied)
- PRD: plans/prd.md (includes the Technical Foundation — no separate master plan)
- Open slice issues: #x (in-progress), #y, #z (backlog)
- Branch / last commit: <branch> @ <short-sha>

## In-flight work
[The slice/issue currently open, what's half-done, the next concrete step in it.]

## Decisions made this session
[Choices the next agent must respect so it doesn't relitigate them — and why.]

## Open questions / blockers
[What needs the user or is unresolved.]

## Suggested skills
[Which skills the next agent should invoke and when, e.g.:
 - `/grab-issue` to continue the in-flight slice
 - `/office-hours` if the backlog needs triage]

## Gotchas
[Traps the next agent would otherwise hit — flaky command, env quirk, ordering constraint.]
```

## Before you save — redact

Strip any sensitive data: API keys, tokens, passwords, connection strings, PII. If
something sensitive is load-bearing for context, reference where it lives (env var name,
secret manager path) rather than the value.

## Hand off

End with:
> "Handoff written to `<path>`. Start the next session by pointing the agent at it.
> **Next:** open a fresh session and have it read the handoff, then run the suggested skills."

## Rules

- Per Rule 14, dense over complete — link artifacts, don't re-paste them.
- Per Rule 12, if the work is mid-failure, say so in "Where things stand." Don't write a
  handoff that reads as if everything's fine.
- Per Rule 10, this IS a checkpoint — if you can't describe the state cleanly, that's a
  signal to stop and reconcile before handing off.
