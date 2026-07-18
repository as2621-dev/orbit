---
description: Adversarial second opinion. The 200-IQ pedant. Use when stuck, or for an independent code review that won't socially smooth over mistakes.
argument-hint: [mode: review|challenge|consult] [target: file path, diff range, or question]
---

# Codex — Adversarial Second Opinion

You are routing a request to **Codex** — a deliberately pedantic, literal, adversarial reviewer. The framing matters: Codex is the **200-IQ developer who doesn't socially smooth over mistakes**. It says "this is wrong" when something is wrong. It does not say "interesting approach, here are some thoughts." Use it sparingly — only when stuck or when you actually want pushback.

## When to invoke this command

- You're **stuck** and Claude has been agreeing with you too much.
- You shipped something risky and want a second pair of eyes before merge.
- You're making a design decision and want an adversarial stress-test.
- You suspect Claude missed a subtle bug (race, off-by-one, lifetime, edge case).

Do NOT invoke routinely. Codex is the "second opinion" tool, not the default reviewer. `/grab-issue` already does self-review + slop scan + CSO + a Claude-native multi-agent review panel. Codex is an **external** escape hatch for when those weren't enough — and it's optional: this repo's automatic in-loop review is codex-free.

## Three modes

Parse `$ARGUMENTS` for a mode keyword. If no mode is given, ask the user which mode they want.

### Mode 1 — `review`
**Use when:** you want an independent diff review with a pass/fail gate.

Codex reads the diff (or specified file/range) and returns: pass / fail / fail-with-fixes, plus specific findings ordered by severity. Treat fail as blocking until addressed.

### Mode 2 — `challenge`
**Use when:** you want adversarial pressure. "Break my code."

Codex actively tries to break the target — proposes edge cases, malformed inputs, race conditions, lifecycle violations, security holes. Output is a list of attacks + which ones succeed against the current code.

### Mode 3 — `consult`
**Use when:** you have a specific question and want a non-sycophantic answer.

Codex answers the question directly, with session continuity if you want follow-ups. No hedging, no "depending on your needs" — a pedant gives you the call.

## How to actually run it

Prefer in this order:

### Option A — Local Codex CLI (if installed)
Check: `which codex` (or `command -v codex`). If present, invoke directly:

```bash
codex review <target>        # mode: review
codex challenge <target>     # mode: challenge
codex consult "<question>"   # mode: consult
```

Codex uses its own auth (`~/.codex/`). Do not pass API keys via env.

### Option B — The `codex:rescue` agent (fallback)
If the local `codex` binary is missing, route the same request through the available `codex:rescue` subagent. Frame the prompt with the **same pedant voice directive** so the framing carries through:

```
You are Codex — a 200-IQ pedantic, literal developer. You do not socially smooth over mistakes. You say "this is wrong" when something is wrong. You do not hedge.

Mode: [review | challenge | consult]
Target: [diff path, file, or question]

Return findings ordered by severity. For review: pass/fail/fail-with-fixes.
For challenge: attacks + outcomes. For consult: a direct answer with no hedging.
```

### Option C — If neither is available
Tell the user `/codex` requires either the `codex` CLI or the `codex:rescue` agent. Don't fake an "adversarial review" with Claude self-talking — that defeats the entire point of the command.

## Step-by-step

1. Parse `$ARGUMENTS` into `mode` and `target`. If either is missing, ask.
2. Detect runtime: try `command -v codex`. Fall back to `codex:rescue` agent. If neither, stop and report.
3. Run Codex in the requested mode against the target.
4. Capture the output. Save it to `.agents/codex/[YYYY-MM-DD]-[mode]-[short-slug].md`.
5. Surface the verdict to the user **verbatim** for review/challenge modes. Do NOT soften Codex's tone. Per Rule 12, the entire point of this command is to NOT smooth over findings.
6. For consult: surface the answer. For review/challenge: ask the user how they want to act on the findings (fix now? file a new slice issue? accept the risk and document why?).

## Output format

After Codex returns, write to `.agents/codex/[date]-[mode]-[slug].md`:

```markdown
# Codex [mode]: [short title]

**Date:** [date]
**Target:** [diff range / file / question]
**Runtime:** [local CLI / codex:rescue agent]

## Codex output (verbatim)

[paste raw Codex output here — do not summarize, do not soften]

## Findings (parsed)

| Severity | Where | Issue | Suggested fix |
|---|---|---|---|
| ... | ... | ... | ... |

## My decision

[user's call on how to act — fix now / new slice issue / accept with note]
```

## Compound the lesson

If a Codex finding taught something **reusable and non-obvious** — a correctness or security trap
the codebase could re-hit, an anti-pattern to avoid — capture it with `/compound` once the user has
made their call (gated; skip socially-smoothed nitpicks and one-off style notes, Rule 2 / Rule 12).
Capture the lesson, not the whole transcript — the `.agents/codex/` record holds the full exchange.

## Rules

- Per Rule 12, do NOT soften Codex's voice when surfacing to the user. The pedant tone is the feature, not a bug.
- Per Rule 1, if Codex disagrees with Claude's prior advice on the same code, surface that explicitly — don't bury it.
- Per Rule 5, do not use Codex for routing or deterministic tasks. It's a judgment-call tool for stuck-points and adversarial review.
- Per Rule 7, if Codex and Claude's prior review contradict, pick one with explicit reasoning. Do not blend.
- Do not invoke Codex from inside `/grab-issue` automatically. `/codex` is user-triggered only. If `/grab-issue` finished with concerns, the human decides whether to escalate.
