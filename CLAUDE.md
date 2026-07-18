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

- After `/ideate` → suggest `/brainstorm` on the chosen direction
- After `/brainstorm` → suggest `/cto`
- After `/cto` → suggest `/to-issues`
- After `/to-issues` → suggest `/grab-issue`
- After `/grab-issue` succeeds → suggest the next `/grab-issue`, or `/office-hours` if the backlog is empty (or `/improve-architecture` if a few slices have landed since the last review)
- After `/improve-architecture` → suggest `/grab-issue` on the highest-leverage refactor slice it filed
- After `/grab-issue` fails → suggest `/rca` with the failure mode, or retry the slice
- After `/rca` → suggest applying the fix (manual + `/commit`) or filing it as a new slice issue
- After `/debug` → if fixed-and-verified, suggest `/commit`; if unresolved after the loop bound, suggest `/rca`
- After `/commit` → suggest `git push`, the next `/grab-issue`, or `/office-hours`
- After `/codex` findings → suggest fix-now / new-slice-issue / accept-with-note
- After `/office-hours` → suggest acting on the one next-call from the session
- After `/handoff` → suggest opening a fresh session pointed at the handoff doc

This applies **at every step inside a command too**, not just at hand-off. If you finish a planning section, name what part of the plan needs the user's input next. If you spot a blocker mid-execution, name the unblock action.

Format: end-of-turn line should read something like:
> **Next:** `/cto` (turn this brief into a PRD + reference docs)

Or for non-command actions:
> **Next:** Decide whether to push or grab the next slice. Push? Or `/grab-issue`?

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

This project ships with **13 slash commands**. The core pipeline runs top-to-bottom; the rest are support commands.

| Command | When to use |
|---|---|
| `/ideate` | **Optional, for a blank page.** When you have no specific idea yet: grounded research → ideas from many angles → adversarial cut → ranked shortlist. Hand the winner to `/brainstorm`. |
| `/brainstorm` | After `/ideate` (or first, if you already have an idea). Relentless, NON-technical interview that pressure-tests the raw idea AND refines it into a product brief. Critical — pushes back, won't flatter. |
| `/cto` | After `/brainstorm`. Produces **the PRD** (with a Technical Foundation: architecture, stack, key decisions, milestones) and reference docs. |
| `/to-issues` | After `/cto`. Slices the PRD into vertical-slice (tracer-bullet) issues on the GitHub kanban backlog. |
| `/grab-issue` | After `/to-issues`. **Dispatcher** — pulls the top unblocked slice and hands its whole build to a **fresh sub-agent** (clean context per slice, so `/loop /grab-issue` drains the backlog without context rot). The sub-agent builds it **test-first (red→green→refactor)**: test → code → refactor-for-depth → simplify → slop scan → CSO → acceptance check → browser-verify UI slices (puppeteer regression + browser-use walkthrough) → single commit → **Claude-native multi-agent review panel** (residual gate) → move to done. |
| `/compound` | Capture a reusable, non-obvious learning into `docs/solutions/` — the compounding store `/grab-issue` B2.5 reads before building. The canonical write recipe (gated, dedups). Called as a tail step by `/grab-issue`, `/rca`, `/debug`, `/codex`, `/improve-architecture`; run standalone for any insight that surfaced outside a command. |
| `/improve-architecture` | Every few days. Finds shallow/tangled modules, proposes deepenings in plain language, files refactor slices, and syncs `plans/prd.md` + `reference/`. Also runs proactively inside `/cto` on re-runs. |
| `/office-hours` | Weekly diagnostic — what's stuck, what's risky, what's next. Run regularly. |
| `/rca` | When something breaks. Root-cause analysis, then proposes a fix. |
| `/debug` | When a browser bug breaks. Reproduces with `browser-use`, diagnoses with Chrome DevTools, fixes, re-verifies in-browser, loops until gone. |
| `/commit` | Stage and create a conventional commit. |
| `/codex` | Adversarial second opinion when stuck or want pushback. 200-IQ pedant. User-triggered, not automatic. |
| `/handoff` | Compact the conversation into a handoff doc (saved to temp, not committed) so a fresh agent can continue. |

The PRD (with its Technical Foundation) lives in `plans/prd.md` — there is no separate master plan. Slice work lives on **GitHub Issues** — the kanban board is the `status:backlog` / `status:in-progress` / `status:review` / `status:done` labels, viewable as a drag-and-drop board in the repo's **GitHub Project**. Each slice is sized to one subagent within a 120k-token budget — `/grab-issue` spawns a **fresh sub-agent per slice**, so draining the backlog with `/loop /grab-issue` keeps the orchestrator's context flat. Reference docs live in `reference/`. Durable learnings (the compounding store, written by `/compound`) live in `docs/solutions/`. Codex transcripts in `.agents/codex/`. CSO follow-ups in `.agents/cso-findings/`. Debug reports in `.agents/debug/` (browser tooling playbook for both `/debug` and UI-slice verification: `reference/browser-debug-playbook.md`). Handoff docs go to the OS temp / scratchpad, never committed.

**Design references (remote):** The full design library lives in a separate public repo to keep this template green:

`https://github.com/ashesh2621/design-references` (~1 GB)

Contents: 86 skills + 511 design systems + 2,827 components + 20,660 shared_code templates, scraped from aura.build.

`/cto` fetches `design-systems/INDEX.md` from there when picking a visual language for UI products. `/grab-issue` fetches relevant `skills/` and `components/` when building UI slices. Both commands `curl` indexes first, then fetch full content only for the items they decide to use.

Local pointer + fetch recipes: `design-references/RESOURCES.md`.
