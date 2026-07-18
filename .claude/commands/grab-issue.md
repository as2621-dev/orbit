---
description: Pull the top unblocked slice off the GitHub kanban backlog and delegate its entire build to a fresh sub-agent (clean context per slice, so /loop never rots), which checks it's not already done, plans it (learnings + edge cases + risk lenses), builds it test-first (red-green-refactor), simplifies, browser-verifies UI slices (puppeteer regression + browser-use walkthrough), reviews it with a Claude-native multi-agent panel behind a residual gate, captures learnings, and moves it to done.
argument-hint: [optional: issue number to grab a specific slice; defaults to the next unblocked one]
---

# Grab Issue — Kanban Dispatcher + Per-Slice Build

This command has **two layers**:

1. **Dispatcher** (runs in your main context) — picks ONE unblocked slice and delegates its
   *entire* build to a **fresh sub-agent**, then records the one-line summary it returns.
2. **Build Protocol** (runs inside that sub-agent) — executes the slice end-to-end with its
   own clean **120k-token budget**, moving the card across the kanban columns
   (GitHub labels): `status:backlog` → `status:in-progress` → `status:review` → `status:done`.

**Why this shape:** the dispatcher only ever holds short summaries, so its context stays flat
no matter how many slices you drain. Each slice starts from clean context. This is what makes
`/loop /grab-issue` safe over a long backlog — no context rot, no handoff-threshold guessing.

One slice = one focused commit. Use the GitHub MCP tools (`mcp__github__*`, via ToolSearch)
against the repo in session scope.

---

# Dispatcher — keep this context thin

## D0 — Prime (minimal, on purpose)

Confirm the working tree is clean (`git status`); if not, stop and tell the user to
commit/stash first. **Do NOT read the full PRD / reference docs here** — the build sub-agent
re-primes itself from clean context. Keeping the dispatcher light is the whole point; don't
pollute it with content that belongs in the sub-agent.

## D1 — Pick the slice

- If `$ARGUMENTS` names an issue number, grab that one.
- Otherwise, list open issues labeled `slice` + `status:backlog` + `ready-for-agent`, and
  pick the **top unblocked** one: every issue in its "Blocked by" must be closed /
  `status:done`. Skip anything still `blocked`.
- If nothing is grabbable, say so and stop — report what's blocked and on what.

Restate the chosen slice and its acceptance criteria back in one or two lines (Rule 10
checkpoint), then hand it off.

## D2 — Delegate to a fresh build sub-agent

Spawn **exactly one** sub-agent (Agent tool, `subagent_type: general-purpose`) and give it the
**Build Protocol** below verbatim, with the chosen issue number substituted in. That sub-agent
owns the full lifecycle (claim → plan → build → simplify → verify → review → commit → close →
compound) inside its own 120k-token budget.

- **Do not build the slice yourself in this context.** Your job is to dispatch and collect.
- **One slice at a time** (Rule 3): never spawn build agents in parallel, never use worktrees.
- Wait for the sub-agent to return, then go to D3.

## D3 — Record + hand off

From the sub-agent's returned summary, report concisely: shipped vs parked, the commit sha,
and what got unblocked. Do not re-derive or re-verify in this context (trust the sub-agent's
report; if it reports a failure, surface it — Rule 12). End with:

> "Slice #<N> shipped → `status:done`. Unblocked: #x, #y.
> **Next:** run `/grab-issue` for the next slice, or `/office-hours` if the backlog is empty."

To drain the backlog unattended, wrap this command in `/loop /grab-issue` — the dispatcher
keeps pulling the next unblocked slice **one at a time**, each in a fresh build sub-agent,
until nothing is grabbable, then stops. Because every slice builds in clean context, the loop
does not accumulate context rot however long the backlog is.

---

# Build Protocol — the sub-agent runs this (fresh context, 120k ceiling)

> Dispatcher: paste this whole section to the sub-agent, with `#<N>` set to the chosen issue.

You execute ONE vertical-slice issue (#<N>) end-to-end. You are the single agent for this
slice, with a **120k-token ceiling** (Rule 6). The issue's "Build budget" line is your target.
If you approach the ceiling before the acceptance criteria are met, do NOT silently overrun —
stop, move the issue back to `status:backlog`, comment what's done and what's left, split the
remainder into a follow-on issue, and return a summary saying so. A slice that won't fit is a
slicing finding, not a reason to blow the budget.

## B0 — Prime context

Read `CLAUDE.md`, `plans/prd.md` (its Technical Foundation is the architecture context —
there is no separate master plan), and relevant `reference/` docs.

**Verification contract.** Discover the repo's exact lint / typecheck / test / build commands
now (from `CLAUDE.md`, `package.json`, `pyproject.toml`) and treat them as this slice's
verification contract — B5 and B8 run *these* rather than rediscovering them each time.
**Learnings store.** Note whether `docs/solutions/` exists; it holds past learnings (bugs hit,
patterns set, conventions adopted) that B2.5 reads and B10.5 writes back. If it's missing, it
gets created on the first compound write-back.

## B1 — Idempotency check (is it already done?)

Before spending any build budget, check whether this slice's acceptance criteria are
**already satisfied by the current `HEAD`** — the work may have shipped on a prior branch, a
rebase, or an earlier interrupted session. Read the code the criteria describe; run the
relevant existing tests if any. If every criterion already holds, do **not** reimplement
(Rule 2 / Rule 3): comment "Already satisfied by <ref>", move the card straight to
`status:done`, unblock dependents (B10), and return a summary saying so. If only *some*
criteria hold, note which — B3 builds only the remainder.

## B2 — Claim it

Move the card: remove `status:backlog`, add `status:in-progress`. Assign yourself if the
tracker supports it. Post a one-line comment: "Picked up — building this slice."

## B2.5 — Plan the slice (depth proportional to the slice)

A quick pre-build pass. Keep it light for routine slices; deepen only where the slice earns
it — **score the weak spots and deepen the top one or two**, don't gold-plate a CRUD slice.

1. **Learnings pass (cheap, always).** If `docs/solutions/` exists, grep-first for prior
   work on this slice's surface: extract keywords (module, technical term, problem
   indicator) → search entry frontmatter (`tags:` / `problem_type:` / `symptoms:`) → read
   only the few hits. Pull forward: constraints, **known-failed approaches to avoid**, and
   patterns to follow. Note each entry's date; never let a stale learning silently override
   present code. Absence is a useful signal — nothing to avoid here.

2. **Spec-flow edge-case enumeration (cheap, always).** Ground in the codebase first — a gap
   isn't a gap if existing middleware/validation already handles it. Map the slice's flow
   (entry → branch points → terminal states), then hunt for what the issue omitted:
   **unhappy paths, state transitions (partial completion, concurrent sessions, stale data),
   permission boundaries, integration seams**. For each gap, record a concrete behavior plus
   a **default assumption** so the build isn't blocked waiting on the user.

3. **Test-scenario enumeration by category.** Turn the acceptance criteria + the gaps above
   into an ordered behavior list, covering every applicable category: **happy path**
   (always), **edge** (boundary, empty/nil, concurrency), **error/failure** (invalid input,
   downstream failure, timeout, permission denied), and **integration — what mocks alone
   won't prove** (e.g. creating X fires callback Y that persists Z). Each scenario names
   input · action · expected outcome. A pure config/scaffolding slice may record
   `Test expectation: none — <reason>`.

4. **Conditional risk-specialist dispatch (fan-out only when triggered).** Look at what the
   slice will touch and spawn a focused sub-agent **only** for the surfaces that fire — keep
   it to the one or two that actually apply, inside the 120k budget:
   - auth / input / secrets → **security lens** (threat-model: authz/authn, injection, secret exposure)
   - loops / queries / hot paths → **performance lens** (Big-O, N+1, behaviour at 10×/100× data)
   - schema / migrations → **migration lens** (expand/contract, old-code-on-new-schema deploy window)
   - data writes → **data-integrity lens** (txn boundaries, referential integrity, PII)
   - cross-module / public contract → **architecture lens** (SOLID, layering, contract stability)
   - legacy / unfamiliar area → **git-history lens** (`git log -S`, files-changed-together = blast radius)

   Fold each specialist's findings into the behavior list / build plan. If nothing
   non-trivial is touched, skip — the B4 self-review is enough.

5. **Depth & posture flags.** If a trivial-*looking* slice actually changes an external
   contract (exported API, CLI flag, env var, CI config, shared type), treat it as a heavier
   slice (more scenarios, run the architecture lens). If the target area is legacy and
   weakly tested, build **characterization-first** — pin current behaviour with tests
   *before* changing it — instead of pure red-green.

## B3 — Build the slice test-first (red → green → refactor)

Build the **complete vertical path** the issue describes (data → logic → UI → test), and
nothing beyond it (Rule 2, Rule 3 — surgical; do not gold-plate adjacent code). Drive it
**test-first**, one behavior at a time — do NOT write all the tests up front (that's
horizontal slicing; it couples tests to a design you haven't validated yet).

**Before the loop:** restate the public interface (the seam) you'll build against. Your
ordered behavior list is the one from B2.5 (acceptance criteria + spec-flow gaps +
specialist findings). Each behavior is one tracer bullet. If you flagged
characterization-first in B2.5, pin current behaviour before changing it.

For each behavior, in order:

1. **RED** — write **one** test for that behavior, against the **public interface**, not
   internals. Run it; watch it fail for the right reason. A test that can't fail when the
   business logic is wrong is mis-written (Rule 9).
2. **GREEN** — write the **minimum** code to pass it. Nothing speculative (Rule 2). Follow
   `reference/conventions.md` and existing patterns (Rule 8, Rule 11) — read exports and
   immediate callers before adding code.
3. Move to the next behavior. Keep the bar green between bullets.

**Refactor** only once the tests for the slice are green — never while a test is red. Remove
duplication and **deepen the interface** (push behavior behind the seam so callers learn
less — same depth lens as `/improve-architecture`). The green tests are your safety net;
behavior must not change during a refactor.

Per-bullet checklist (Rule 9): the test describes observable behavior, not internal
mechanism; uses only the public interface; survives an internal refactor; and the
implementation contains only what the current test needs.

- **If the slice touches UI:** consult the remote design library before writing from
  scratch — read `reference/design-language.md` for tokens; fetch relevant
  `skills/` and `components/` from
  `https://raw.githubusercontent.com/ashesh2621/design-references/main/` and adapt to the
  project's tokens (credit original creators in comments). Test UI behavior at the highest
  seam that's practical (a rendered-behavior test over a snapshot of internals).
  - **Browser regression test (puppeteer), test-first.** For a user-visible behavior that a
    component/unit test can't honestly prove (real navigation, form submit round-trip,
    conditional render after a fetch), write a **deterministic puppeteer test** as the RED
    step: it drives the running app and asserts the user-visible outcome. Watch it fail
    against the unbuilt UI for the right reason, then build to green. This test is
    **committed** and runs in the suite + CI (it's part of the B5 verification contract) —
    it's the durable regression lock. See `reference/browser-debug-playbook.md` §7 for the
    puppeteer-vs-browser-use split and harness setup (dev server, headless, selectors).
    Keep puppeteer for *scripted, repeatable* assertions; the exploratory walkthrough is
    browser-use's job at B8.5.

## B3.5 — System-wide test check (trace two levels out)

Before you call the slice's tests green, run this quick systemic check (10-second skip for a
pure leaf-node change — new helper, new partial, nothing downstream):

- **What fires when this runs?** Callbacks, middleware, observers, event handlers — trace
  **two levels out** from your change by reading the actual code, not docs.
- **Do the tests exercise the real chain?** If every dependency is mocked, the test proves
  logic in isolation and says nothing about interaction — add **at least one no-mock
  integration test** through the real callback/middleware chain.
- **Can failure leave orphaned state?** If state is persisted before an external call, test
  the failure path with real objects — verify cleanup or idempotent retry.
- **What other interfaces expose this?** Grep for the behaviour in sibling
  classes/entry-points; add parity now, not as a follow-up.

## B4 — Self-review and fix

Run `git diff` on your own changes. Review for: logic errors, security, performance, and
`CLAUDE.md` adherence. Note severity. Fix all critical/high; fix medium/low if cheap, else
note them in the issue.

## B5 — Validate

Run the project's lint / typecheck / test / build (the verification contract from B0). On
failure, fix and re-run (max 2 attempts). If still failing, do NOT mask it (Rule 12) — move
the issue back to `status:backlog`, comment the failure, and return a summary saying so.

## B6 — Slop scan (AI-cruft check, on the slice diff)

Re-read the diff and flag — fix all before proceeding (or surface one explicit exception
with a one-line justification):

- Vacuous comments restating code · defensive try/catch that swallows errors with no
  rethrow/log/handling · `any` / `as any` casts without a `// Reason:` · one-shot
  abstractions used in exactly one place · generated-marketing voice in docs
  ("seamlessly", "robust", "leverages") · dead code (commented blocks, unused imports,
  half-renamed identifiers) · mock/stub leftovers (`TODO`, `FIXME`, hardcoded `localhost`,
  stray `console.log`).

## B6.5 — Simplify pass (quality cleanup, on the slice diff)

Run `/simplify` on the slice diff to catch reuse, simplification, efficiency, and altitude
cleanups the slop scan doesn't cover (duplicated logic that should reuse an existing util,
over-nested control flow, needless intermediate state, work done at the wrong layer). This is
a **quality-only** pass — it does not hunt for bugs (B4 + B9's `/code-review` do that). Apply
its fixes now, **before the commit**, so they land in the single slice commit.

Skip with an explicit one-line note for a trivial slice (pure config/scaffolding, or a diff
small enough that there's nothing to simplify) — don't manufacture churn (Rule 2, Rule 3).
Re-run the verification contract (B5) if `/simplify` changed behavior-adjacent code.

## B7 — CSO (lite security pass, on the slice diff)

Scope to what changed — don't audit the whole repo:

- Secrets in code (keys, tokens, connection strings — even in fixtures)
- Auth changes: trace where new code is called from; confirm the auth boundary still holds
- Input validation at every new boundary (Zod / Pydantic / explicit checks)
- SQL/command-injection surface from new string interpolation
- New dependencies: not abandoned (>18mo), not deprecated, not a typosquat
- Logging hygiene: no secret / token / PII logged

Critical & high MUST be fixed before commit. Medium/low: fix if cheap, else log to
`.agents/cso-findings/issue-<N>.md`. If the diff has no security surface, say so explicitly
— don't fake an audit.

## B8 — Acceptance-criteria check (definition of done)

Verify each acceptance-criteria checkbox in the issue actually holds, with a real check
(not "it compiles"). Per Rule 9 / Rule 12, do not declare done if any criterion fails.

## B8.5 — Browser acceptance walkthrough (UI slices only — exploratory)

**Gate condition:** run this *only* if the slice touched UI (a route, component, or
user-visible behavior). For a pure backend/config/lib slice, skip with a one-line note —
don't spin up a browser for nothing.

The committed puppeteer test (B3) proves the *scripted* path. This step catches what the
script didn't encode — drive the **real acceptance flow as a user** with `browser-use` and
confirm it actually works in a live browser:

1. **Tooling check.** Ensure `browser-use` is available (`command -v browser-use`); if
   missing, install per `reference/browser-debug-playbook.md` §3 after a single yes, then
   `browser-use doctor`. Start the app on a free port via the project's dev command (run it
   yourself, background it, verify it's serving before driving).
2. **Walk the acceptance flow.** Follow the playbook's *observe → act → observe* discipline
   (`--json` on every command). Drive the exact user journey the acceptance criteria
   describe; use `--profile` if it needs a logged-in session. Capture a `screenshot --full`
   as the evidence artifact.
3. **Assert the user-visible outcome**, not "the page loaded" — the thing the criteria
   promise (the row appears, the error toast shows on bad input, the redirect lands).
4. **On failure:** this is a real defect, not a flaky check. Fix it (back to B3, add/extend
   the puppeteer regression so the gap is now locked), or if it's a deeper browser bug,
   stop and hand to `/debug` (Rule 12 — don't ship a UI slice whose flow doesn't work).
   Tear down: `browser-use close`, and stop the dev server.

A UI slice is not done until its acceptance flow passes in a real browser. Record the result
(pass + screenshot path, or the defect found) for the B9 commit trailer.

## B9 — Commit + move to review

Produce ONE commit for the slice (`/commit` style; stage only files you changed, never
`-A`):

```
feat(<scope>): <slice title>

Closes #<N>

Acceptance criteria: PASS
Slop scan: PASS (or "PASS — k fixed")
Simplify: PASS (or "PASS — k fixed" / "SKIPPED — <reason>")
CSO: PASS (or "PASS — no security surface" / "PASS — k fixed, m logged")
Browser: PASS (puppeteer green + walkthrough OK) (or "N/A — no UI surface")
Review panel: PASS (or "PASS — k fixed, m deferred")
```

Move the card: remove `status:in-progress`, add `status:review`.

### Multi-agent review panel (Claude-native — this replaces the old auto-`/codex` call)

Review the slice diff with a **panel of independent reviewer sub-agents**, each given the
diff + the acceptance criteria + **one** lens and an **adversarial directive** ("your job is
to find what's wrong; report only real, evidenced defects; default to skeptical"). Run them
**in parallel** via the Agent tool (`subagent_type: general-purpose`). *If your context
can't spawn sub-agents, run the lenses as sequential independent passes instead — fresh
reasoning per lens, and do not let one lens's verdict bias the next.* Each returns findings
as `severity · file:line · issue · concrete fix`.

- **Always:** **correctness/logic** lens, **simplicity/reuse** lens (duplication, wrong
  altitude, needless state — overlaps `/simplify`'s remit as a second opinion).
- **Conditional (spawn only if the diff fires it — same triggers as B2.5.4):**
  **security** (auth/input/secrets), **performance** (loops/queries/hot paths),
  **contract/architecture** (exported API, shared type, schema, cross-module seam),
  **data-integrity** (writes/migrations).

Aggregate the panel: **dedup** by `file:line` + issue, drop anything a lens couldn't
evidence (no plausible-but-unproven findings — Rule 12), and rank by severity. This local
panel is the in-loop reviewer; `/code-review` (incl. its cloud `ultra` mode) and `/codex`
remain **manual** escalations the user can fire — they are not called automatically here.
Address findings — re-commit fixes as needed.

**Defer-vs-fix + residual gate.** Default is to **apply** every finding that has a concrete
fix — leaving a reversible fix unapplied "to be safe" is the failure mode. Defer only:
advisory/report-only findings, findings with no concrete fix, or fixes that need a human
design/contract/behaviour decision. Any finding you don't fix must land in a **durable
sink**, never the session only:

- PR workflow → a `## Known Residuals` section in the PR body.
- Commit-only workflow → `docs/residual-review-findings/<head-sha>.md`, staged with the commit.
- A finding needing a human call → file it as a new `slice` issue on the backlog.

Record which sink you used — a defer that produces no durable artifact is data loss
(Rule 12). Even on green tests, **flag** any change touching auth/authz, a public
contract/schema, or concurrency in the review notes — a passing test does not prove safety
there.

If the user works via PRs, open one for the slice instead of committing to the branch
directly, and let CI + review drive it (offer to subscribe to PR activity).

## B10 — Close out

Once review is clean and validation is green:
- Move the card: remove `status:review`, add `status:done`.
- Close the issue (the `Closes #<N>` in the commit will, once merged).
- **Unblock dependents:** find issues whose "Blocked by" lists this one; if this was their
  last blocker, remove `blocked` and add `ready-for-agent`. Comment on each that it's now
  grabbable.

## B10.5 — Compound (write the learning back)

If this slice taught something reusable — a non-obvious bug and its root cause, a pattern
now established, a convention adopted, a library gotcha — capture it with `/compound`, the
canonical write recipe (gated; it dedups against existing entries, creates the dir +
`README.md` on first write, and uses the shared frontmatter `title`, `tags`, `problem_type`,
`symptoms`, `root_cause`, `date`). Skip if the slice was routine and taught nothing new —
`/compound`'s gate refuses filler. This read→write cycle (B2.5 reads, B10.5 writes) is what
makes later slices cheaper than earlier ones.

## B-return — Report back to the dispatcher

Return a **short** summary (this is all the dispatcher keeps in context):
- Outcome: `shipped` / `parked` / `already-done`.
- Issue, commit sha, one-line title.
- Dependents unblocked (issue numbers), if any.
- Anything the user must decide (residuals filed, budget breach, failure) — one line each.

Do not return diffs, file contents, or step-by-step narration — the dispatcher needs the
decision-grade facts only (Rule 14).

---

## Rules

- Per Rule 4, success criteria = the issue's acceptance criteria. The build sub-agent loops
  until verified.
- Per Rule 10, the issue + its labels ARE the checkpoint. If a build sub-agent is interrupted,
  the next one re-reads the in-progress issue before resuming.
- Per Rule 12, never move a slice to `status:done` with a failing criterion, a skipped test,
  or an unfixed critical/high finding. "Mostly works" is failure — move it back to
  `status:backlog` with a comment instead. The dispatcher must surface a parked/failed slice,
  never report it as shipped.
- Per Rule 3, one slice at a time — the dispatcher spawns a single build sub-agent and waits;
  it does not pull a second issue until this one is done or explicitly parked. No parallel
  builds, no worktrees.
