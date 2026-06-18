---
description: Execute a phase end-to-end. Spawns 4 sub-agents (one per sub-phase), each doing code → review → fix → validate → report. Single commit at phase end.
argument-hint: [path to phase file, e.g. plans/phase-1-auth.md]
---

# Run Phase

You are the **phase orchestrator**. You do NOT write code yourself. You spawn one fresh-context sub-agent per sub-phase, track progress, and produce a single atomic commit at the end of the phase.

## Step 0 — Prime your own context (lightly)

Read:
- The phase file: `$ARGUMENTS`
- `CLAUDE.md` — project rules
- `plans/master-plan.md` — for big-picture context
- Relevant reference docs in `reference/`
- Recent git state (`git status`, `git log --oneline -5`)

Confirm:
- Working tree is clean. If not, stop and tell the user to commit/stash first.
- The phase file has exactly 4 sub-phases. If it has 3 (a documented exception from `/plan-phases`), proceed but flag in the final report. If anything else, stop.

Per Rule 1, if anything is unclear, ask before proceeding.

## Step 1 — Set up progress tracking

Derive `[feature-slug]` from the phase filename (e.g., `phase-1-auth.md` → `phase-1-auth`).

Create or read `plans/[feature-slug]-progress.md`:

```markdown
# Progress: [feature-slug]

**Phase file:** $ARGUMENTS
**Started:** [date]

## Sub-phase progress
- [ ] 1: [name] — PENDING
- [ ] 2: [name] — PENDING
- [ ] 3: [name] — PENDING
- [ ] 4: [name] — PENDING
```

If the file already exists, **resume from the first incomplete sub-phase**. Don't redo completed ones.

## Step 2 — Decide: sequential or parallel-via-worktrees?

Before spawning anything, inspect the 4 sub-phases for **parallelizability**:

1. Build a dependency graph from each sub-phase's `Dependencies` field.
2. Build a file-overlap graph: any two sub-phases listing overlapping paths in `Files touched` are **in conflict**.
3. Identify **independent groups**: sub-phases with no dependencies on each other AND no file overlap.
4. Check for `⚠ irreversible` markers from `/plan-phases` self-critique — irreversible sub-phases run **sequentially only**, never in parallel.

If the phase contains a group of 2+ truly independent sub-phases, prompt the user:

> "Sub-phases [list] are independent (no shared files, no dependencies). Run them in parallel via git worktrees?
> - **Parallel:** faster, but each sub-agent works in its own worktree; you'll need to merge worktrees back at phase end (I handle this).
> - **Sequential:** slower, simpler. Safe default."

Default to **sequential** if:
- No worktree-capable git state (not a git repo, or detached HEAD)
- Any sub-phase is marked `⚠ irreversible`
- The user declines

If the user opts for parallel, jump to **Step 2P**. Otherwise continue to **Step 2S**.

---

## Step 2S — Sequential execution (default)

For each sub-phase, in order:

### 2a. Mark IN PROGRESS in the progress file.

### 2b. Spawn a `general-purpose` sub-agent with this prompt:

```
You are executing sub-phase [N] of phase [feature-slug].

## Inputs to read
- Phase file: [$ARGUMENTS]
- Project rules: CLAUDE.md
- Reference docs in reference/ (read only those relevant to your files)
- If sub-phase [N] depends on earlier sub-phases, read the files those sub-phases created/modified to understand current state.

## Your mission
Execute ONLY sub-phase [N]. Then run the full quality cycle on your own changes:
implement → review → fix → validate → report.

## Step A — Implement
- Touch ONLY files listed in sub-phase [N]'s "Files touched"
- Follow CLAUDE.md (especially Rules 2, 3, 8, 11)
- Verify as you go (imports resolve, types check)
- **If this sub-phase touches UI:** before writing UI from scratch, consult the remote design library:
  - Read `reference/design-language.md` (the chosen design system for this product). All tokens live there.
  - For animation/layout/interaction patterns, fetch the relevant skill: `curl -s https://raw.githubusercontent.com/ashesh2621/design-references/main/skills/INDEX.md` then fetch the chosen `.md` files.
  - For starter HTML (hero, pricing, dashboard, card, etc), search components: `gh api -X GET search/code -f q="<keyword> repo:ashesh2621/design-references path:components" --jq '.items[].path'`, then fetch the HTML from `raw.githubusercontent.com/.../components/html/<slug>.html`.
  - Adapt the fetched HTML to match `reference/design-language.md` tokens; do not copy-paste raw. Credit the original creator in code comments per the meta JSON's `credit_name`.

## Step B — Self code-review
- Run `git diff` to see your own uncommitted changes
- Review for: logic errors, security issues, performance, code quality, CLAUDE.md adherence
- Note severity (critical / high / medium / low) for each issue

## Step C — Fix
- Fix all critical and high severity issues
- For medium/low, fix if cheap; otherwise note in your report

## Step D — Validate
- Run the project's lint/typecheck/test/build commands (read CLAUDE.md or package.json/pyproject.toml to discover them)
- If validation fails, fix and re-run (max 2 attempts)
- If still failing, do NOT mask the failure — report it

## Step E — Definition-of-done check
- Verify the sub-phase's stated "Definition of done" actually holds
- Per Rule 9 and Rule 12, do not declare success if the check doesn't pass

## Step F — Report
Save to `.agents/execution-reports/[feature-slug]-sub-[N].md`:
- What you implemented
- Files created / modified
- Divergences from the plan (and why)
- Code review findings + fixes
- Validation results
- Definition of done: PASS / FAIL
- Any concerns for the orchestrator

## CRITICAL: Do NOT commit. The orchestrator commits at phase end.

## Return to orchestrator with:
1. STATUS: SUCCESS or FAILURE
2. Files touched (paths only)
3. Validation: PASS / FAIL with details
4. Definition of done: PASS / FAIL
5. Concerns
```

### 2c. Process the sub-agent's result

- **SUCCESS + DoD PASS:** Mark COMPLETED in progress file. Move to next sub-phase.
- **FAILURE or DoD FAIL:** Mark FAILED. Stop. Report to user:
  > "Sub-phase [N] failed: [reason]. Options: retry / skip / stop. The phase will not commit until all 4 succeed."
  Wait for user direction. Do NOT auto-retry.

### 2d. Do NOT spawn the next sub-agent in parallel (in sequential mode).

Sequential only — later sub-phases may depend on earlier ones' file state. (Parallelism happens in Step 2P below if selected.)

---

## Step 2P — Parallel-via-worktrees execution (opt-in)

Only enter this step if the user explicitly opted in at Step 2.

### 2P.a — Set up worktrees

For each independent sub-phase in the parallel group:

```bash
git worktree add ../[repo-name]-sub-[N] HEAD
```

Track the worktree paths in the progress file:

```markdown
## Worktrees
- Sub-phase 1: ../[repo]-sub-1
- Sub-phase 3: ../[repo]-sub-3
```

### 2P.b — Spawn parallel sub-agents

Spawn sub-agents **in a single message** (multiple tool calls in parallel) so they run concurrently. Each sub-agent gets the **same prompt as in Step 2S/2b**, with one addition at the top:

```
## Your working directory
You are working in worktree `[worktree-path]`. cd into it before any git or file operations. Do NOT cd back to the main worktree. Do NOT commit — the orchestrator merges and commits at phase end.
```

Sub-phases that depend on members of the parallel group **must wait** — they run sequentially after the parallel group merges.

### 2P.c — Collect results

Wait for all parallel sub-agents to complete. For each:
- If SUCCESS + DoD PASS: stage in the progress file as `READY-TO-MERGE`
- If any failure: STOP — do not proceed to merge. Report failures to user. Discard or fix per user direction.

### 2P.d — Merge worktrees back

For each successful parallel worktree, in declared sub-phase order:

```bash
# from the main worktree
git diff [main-branch]..[worktree-branch] -- [files-touched-by-that-sub-phase]
```

Apply the diff to the main worktree (cherry-pick the worktree's uncommitted changes file-by-file). Because the file-overlap check at Step 2 ensured disjoint paths, this should be conflict-free. **If a conflict appears, STOP** — your overlap detection was wrong. Report to user and ask whether to merge manually or abort.

After merge, run the validate step (lint/typecheck/test/build) on the merged state from the main worktree. A passing parallel group means the merged result also validates.

### 2P.e — Tear down worktrees

```bash
git worktree remove ../[repo]-sub-[N]
```

Update progress file: mark merged sub-phases COMPLETED. Move on to any remaining sequential sub-phases (those that depended on the parallel group).

### Parallel-mode caveats

- Per Rule 12, if merge surfaces ANY conflict, that's a failure of the planning step — not something to paper over. Re-flag to `/plan-phases` for the next phase.
- Per Rule 3, do not let worktree sub-agents touch files outside their declared `Files touched` — surgical scope is enforced by the file-overlap check.
- Never run irreversible sub-phases (migrations, deletions, API renames) in parallel, even if their file sets look disjoint.

---

## Step 3 — Phase-level end-to-end checks (DoD + slop scan + CSO)

After all 4 sub-phases report SUCCESS, run **three passes on the full phase diff**, in order. All three must pass before the commit. If any fail, do NOT commit — report and stop.

Compute the full phase diff once: `git diff` against the working tree's pre-phase state (or `git diff [base-branch]` if you tracked the base). Apply all three passes to the same diff.

### 3a — Phase-level definition of done
Verify the **phase-level "definition of done"** from the phase file (not just per-sub-phase ones). Run any phase-wide check (integration test, end-to-end smoke, deployed preview ping). Per Rule 9, this is the test that proves the *phase's intent*, not just compilation.

### 3b — Slop scan (AI-cruft check)
Re-read the full phase diff and flag any of these. Per Rule 12, surface findings even if they feel pedantic.

- **Vacuous comments** that restate the code (`// increment counter` above `counter++`)
- **Defensive try/catch that swallows errors** with no rethrow / no log / no user-facing handling
- **`any` / `as any` casts** without a `// Reason:` comment explaining why type safety was abandoned
- **One-shot abstractions** — a helper used in exactly one place
- **Generated-marketing voice** in README/docs ("seamlessly integrates", "powerful", "robust", "leverages")
- **Dead code from earlier iterations** — commented-out blocks, unused imports, half-renamed identifiers
- **Mock/stub leftovers** — `TODO`, `FIXME`, hardcoded `localhost`, `console.log`s that shouldn't ship

For each finding: file:line, the smell, and the minimum fix. Fix all of them before proceeding (or surface a single explicit exception with a one-sentence justification). Do not commit slop.

### 3c — CSO (lite security pass)
Re-read the full phase diff with security goggles. Scope this to **what changed** — do not audit the whole repo here.

- **Secrets in code:** API keys, tokens, connection strings, private keys. Even in test fixtures.
- **Auth changes:** any modification to login, session, token, role, permission, or middleware that gates access. Trace where the new code is called from and confirm the auth boundary still holds.
- **Input validation:** any new endpoint, form handler, or data ingestion point — confirm input is validated at the boundary (Pydantic / Zod / explicit checks), not assumed safe.
- **SQL/command injection surface:** any new string interpolation into a query, shell command, or template — flag it.
- **Dependency additions:** new package in `package.json` / `requirements.txt` — check it's not abandoned (last release > 18mo ago), not deprecated, and not a known typosquat.
- **Logging hygiene:** confirm no secret, token, password, or PII is logged.

For each finding: severity (critical/high/medium/low), file:line, the risk, the fix. Critical and high MUST be fixed before commit. Medium/low: fix if cheap, else log to `.agents/cso-findings/[phase-slug].md` for follow-up.

If the diff touches none of these surfaces (e.g., pure styling phase), say so explicitly: "CSO: no security-relevant surface in this diff." Don't fake an audit.

---

## Step 4 — Single atomic commit

Per the project's commit policy (`/commit` style), produce **one commit** for the whole phase:

```
feat([phase-scope]): [phase name]

Sub-phases:
- 1: [name]
- 2: [name]
- 3: [name]
- 4: [name]

Definition of done: PASS
Slop scan: PASS (or "PASS — N findings fixed")
CSO: PASS (or "PASS — no security-relevant surface" / "PASS — N findings fixed, M medium logged")
Reports: .agents/execution-reports/[feature-slug]-sub-{1..4}.md
```

Stage only files actually changed by the sub-agents. Do not stage adjacent edits, generated logs, or progress files unless they're part of the phase's intent.

## Step 5 — Phase summary

Update `plans/[feature-slug]-progress.md` to status COMPLETE, then output:

```
## Phase Complete: [feature-slug]

Sub-phases:
- [x] 1: [name]
- [x] 2: [name]
- [x] 3: [name]
- [x] 4: [name]

Definition of done: PASS
Commit: [hash]

Reports:
- .agents/execution-reports/[feature-slug]-sub-1.md
- ... (etc)

Next: run /run-phase for the next phase file, or /office-hours to check in.
```

## Rules

- Per Rule 5, you are an orchestrator — do not write feature code yourself. Sub-agents do that.
- Per Rule 10, the progress file is your checkpoint. If you lose track, re-read it before proceeding.
- Per Rule 12, do not commit a phase where any sub-phase or the phase-level DoD failed. "Mostly worked" is failure.
- Resumption: if `/run-phase` is re-invoked after a crash, the progress file lets you skip completed sub-phases. Trust it but verify with `git log` (and any remaining worktrees) before assuming a sub-phase truly shipped.
- Parallel mode is **opt-in only**, requires git, refuses irreversible sub-phases, and falls back to sequential at the first sign of conflict.
