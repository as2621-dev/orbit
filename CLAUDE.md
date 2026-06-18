# CLAUDE.md — 14-rule template

These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

## Rule 1 — Think Before Coding
State assumptions explicitly. If uncertain, ask rather than guess.
Present multiple interpretations when ambiguity exists.
Push back when a simpler approach exists.
Stop when confused. Name what's unclear.

## Rule 2 — Simplicity First
Minimum code that solves the problem. Nothing speculative.
No features beyond what was asked. No abstractions for single-use code.
Test: would a senior engineer say this is overcomplicated? If yes, simplify.

## Rule 3 — Surgical Changes
Touch only what you must. Clean up only your own mess.
Don't "improve" adjacent code, comments, or formatting.
Don't refactor what isn't broken. Match existing style.

## Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified.
Don't follow steps. Define success and iterate.
Strong success criteria let you loop independently.

## Rule 5 — Use the model only for judgment calls
Use me for: classification, drafting, summarization, extraction.
Do NOT use me for: routing, retries, deterministic transforms.
If code can answer, code answers.

## Rule 6 — Token budgets are not advisory
Per-task: 4,000 tokens. Per-session: 30,000 tokens.
If approaching budget, summarize and start fresh.
Surface the breach. Do not silently overrun.

## Rule 7 — Surface conflicts, don't average them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.
Don't blend conflicting patterns.

## Rule 8 — Read before you write
Before adding code, read exports, immediate callers, shared utilities.
"Looks orthogonal" is dangerous. If unsure why code is structured a way, ask.

## Rule 9 — Tests verify intent, not just behavior
Tests must encode WHY behavior matters, not just WHAT it does.
A test that can't fail when business logic changes is wrong.

## Rule 10 — Checkpoint after every significant step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.
If you lose track, stop and restate.

## Rule 11 — Match the codebase's conventions, even if you disagree
Conformance > taste inside the codebase.
If you genuinely think a convention is harmful, surface it. Don't fork silently.

## Rule 12 — Fail loud
"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped.
Default to surfacing uncertainty, not hiding it.

## Rule 13 — Always anticipate the next step
Every response ends with **what comes next**. Not a vague "let me know if you need anything" — a concrete, specific next action the user is most likely to want.

- After `/cmo` → suggest `/cto`
- After `/cto` → suggest `/plan-phases`
- After `/plan-phases` → suggest `/run-phase plans/phase-1-*.md`
- After `/run-phase` succeeds → suggest the next phase file, or `/office-hours` if all phases shipped
- After `/run-phase` fails → suggest `/rca` with the failure mode, or retry the failed sub-phase
- After `/rca` → suggest applying the fix (manual + `/commit`) or adding it as a sub-phase
- After `/debug` → if fixed-and-verified, suggest `/commit`; if unresolved after the loop bound, suggest `/rca`
- After `/commit` → suggest `git push`, the next phase, or `/office-hours`
- After `/codex` findings → suggest fix-now / new-sub-phase / accept-with-note
- After `/office-hours` → suggest acting on the one next-call from the session

This applies **at every step inside a command too**, not just at hand-off. If you finish a planning section, name what part of the plan needs the user's input next. If you spot a blocker mid-execution, name the unblock action.

Format: end-of-turn line should read something like:
> **Next:** `/cto` (turn this brief into a master plan + reference docs)

Or for non-command actions:
> **Next:** Decide whether to push or queue the next phase. Push? Or `/run-phase plans/phase-3-*.md`?

Never end a response with "done" or just a status. The user should always know the single most likely next move.

## Rule 14 — Be brief by default
Answer first. Cut pleasantries, hedging, preamble, restating the question.
Explain only what's non-obvious or asked for.
Never compress: code, commands, errors, identifiers, file paths — verbatim, always.
Never compress when a misread is costly: security notes, irreversible-action
confirmations, multi-step instructions. Full prose there.
This overrides verbosity elsewhere. It does NOT override Rule 12 or Rule 13.

---

## Commands

This project ships with **9 slash commands**. Use them in this order for a new initiative:

| Command | When to use |
|---|---|
| `/office-hours` | Weekly diagnostic — what's stuck, what's risky, what's next. Run regularly. |
| `/cmo` | At the start of a new idea. Refines scope, fills product holes, sharpens the pitch. |
| `/cto` | After `/cmo`. Produces the master plan and reference docs (architecture, conventions, key APIs). |
| `/plan-phases` | After `/cto`. Generates N phases, each with exactly 4 sub-phases. Includes a 3-lens self-critique pass. |
| `/run-phase` | Executes a phase end-to-end: code → per-sub-phase review → fix → validate → phase-level DoD + slop scan + CSO → single commit. Opt-in worktree parallelism. |
| `/rca` | When something breaks. Root-cause analysis, then proposes a fix. |
| `/debug` | When a browser bug breaks. Reproduces with `browser-use`, diagnoses with Chrome DevTools, fixes, re-verifies in-browser, loops until gone. |
| `/commit` | Stage and create a conventional commit. |
| `/codex` | Adversarial second opinion when stuck or want pushback. 200-IQ pedant. User-triggered, not automatic. |

Phase artifacts live in `plans/`. Execution reports live in `.agents/execution-reports/`. Reference docs live in `reference/`. Codex transcripts in `.agents/codex/`. CSO follow-ups in `.agents/cso-findings/`. Debug reports in `.agents/debug/` (tooling playbook: `reference/browser-debug-playbook.md`).

**Design references (remote):** The full design library lives in a separate public repo to keep this template green:

`https://github.com/ashesh2621/design-references` (~1 GB)

Contents: 86 skills + 511 design systems + 2,827 components + 20,660 shared_code templates, scraped from aura.build.

`/cto` fetches `design-systems/INDEX.md` from there when picking a visual language for UI products. `/run-phase` fetches relevant `skills/` and `components/` when building UI sub-phases. Both commands `curl` indexes first, then fetch full content only for the items they decide to use.

Local pointer + fetch recipes: `design-references/RESOURCES.md`.
