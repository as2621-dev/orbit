---
description: Autonomous browser bug hunt. Reproduces the bug with browser-use, diagnoses with Chrome DevTools, fixes, re-verifies in-browser, loops until gone. Better than a Playwright-CLI loop.
argument-hint: [bug description + how to trigger it, e.g. "checkout button does nothing on /cart when logged in"]
---

# Debug — Autonomous Browser Bug Hunt

You are running a **goal-driven fix-and-verify loop** against a live browser. The success criterion is not "I changed some code" — it is **the recorded reproduction flow passes, the signal that proved the bug is gone, and the whole test suite is green** (including a new regression test that locks this bug out). Loop until that holds (Rule 4). Do not stop at a symptom patch (Rule 12).

Two CLIs do the work. `browser-use` is the **hands** (reproduce the user flow). `chrome-devtools-mcp` is the **instruments** (source-mapped console, CDP network, perf traces, heap). Full routing table + command cheat-sheets: **`reference/browser-debug-playbook.md`** — read it before Step 2.

## Inputs

The reported bug: `$ARGUMENTS`

You need enough to *reproduce*: the URL/route, the exact steps that trigger it, the expected vs actual behavior, and whether it needs a logged-in session. If any of that is missing or vague, ask now (Rule 1). Do not guess your way into a fix — a bug you can't reproduce is a bug you can't fix.

## Step 0 — Tooling check (auto-install on confirm)

Detect both CLIs:

```bash
command -v browser-use ; command -v chrome-devtools
```

- **`browser-use` missing** → show the user the exact command and install after a single yes:
  `curl -fsSL https://browser-use.com/cli/install.sh | bash` then `browser-use doctor`
- **`chrome-devtools` missing** → `npm i chrome-devtools-mcp@latest -g`
- Run the installs yourself via Bash after confirmation (global "run shell commands yourself" rule). Installing a global tool is a system change — get the one yes first, then proceed without further prompts. If the user declines, stop and tell them `/debug` needs both CLIs.

Verify each install succeeded (`browser-use doctor`, `chrome-devtools status`) before continuing. Per Rule 12, do not proceed on a half-installed toolchain.

## Step 1 — Reproduce (browser-use)

Read `reference/browser-debug-playbook.md` §3 first.

Drive the exact flow from the bug report. If it needs auth, use `browser-use --profile "Default"` to reuse the real session. Use `--json` on every command so you branch on structured output, not prose.

Record the **exact ordered command list** that triggers the bug. That list is now both your regression scenario and your verification script — save it.

- **Symptom visible** → screenshot it (`browser-use screenshot --full`), continue.
- **Cannot reproduce** → STOP. Report what you tried and ask the user for a tighter repro (Rule 1). Never fix blind.

## Step 2 — Instrument (chrome-devtools)

Read `reference/browser-debug-playbook.md` §2 (routing) and §4. Pick the instrument from the **symptom class**:

- "It just breaks" / uncaught error → `list_console_messages` → `get_console_message` (source-mapped stack → real `file:line`).
- API fails / 4xx / 5xx / CORS → `list_network_requests` → `get_network_request` (headers, body, timing).
- Slow / janky / freezes → `performance_start_trace` → reproduce → `performance_stop_trace` → `performance_analyze_insight`.
- Memory grows → two `take_memory_snapshot` around the leak → `get_nodes_by_class`.

Capture the **one signal that proves the bug** (the specific console error, the specific failed request, the specific perf insight). That signal is your evidence and your later pass/fail oracle. Use `--output-format=json`.

## Step 3 — Map to source

Trace the captured signal back to a `file:line`. Per Rule 8, read that code path **and its immediate callers and shared utilities** before concluding — "looks orthogonal" is how you patch the wrong line. Classify the root cause (logic / contract / state-race / environmental / data / design), same taxonomy as `/rca`. If it's design-class, a small patch won't really fix it — say so and hand to `/rca` instead of forcing a patch (Rule 12).

## Step 4 — Write the regression test (must fail now)

Before touching the fix, write the test(s) that lock this bug out for good. Per Rule 9, the test encodes *why* this behavior matters — assert the outcome a user actually depends on, not just "the function ran." Put it where the suite expects it (mirror structure, match this repo's existing test conventions — Rule 11).

Run it now, against the **unfixed** code. It MUST fail, for the bug's reason. If it passes on broken code it doesn't catch this bug — you haven't localized the root cause, so go back to Step 3. A green test on a red bug is Rule 12 self-deception.

## Step 5 — Fix (surgical)

Smallest change that kills the **root cause**, not the symptom (Rule 2). Touch only what you must; no drive-by refactors or formatting (Rule 3). Match existing code style and conventions (Rule 11). Verbose names, structured logging with `fix_suggestion`, type hints — per the global rules.

## Step 6 — Verify: whole suite green (close the loop)

Re-run the **exact** `browser-use` flow from Step 1, re-capture the **exact** `chrome-devtools` signal from Step 2, then run the **entire** project test suite (`npm test` / `pytest` — detect what this repo uses, don't assume). Declare fixed only if **all** hold:

1. The reproduction flow now completes without the symptom.
2. The bug-proving signal is absent on re-capture (not "different" — absent).
3. The Step 4 regression test(s) now pass.
4. The **whole suite is green with zero tests skipped**, and no previously-passing test regressed. A skipped test is not a passing test (Rule 12). If the repo has pre-existing failures unrelated to this bug, surface them explicitly in the report — never silently exclude them to claim green.

If any of 1–4 fails → back to Step 3 with what this attempt ruled out. **Loop bound: 4 fix→verify iterations.** After 4 failures, STOP, report what each attempt eliminated, reclassify as likely design-class, and hand to `/rca` (Rule 12 — fail loud, never silently thrash). Never weaken, `skip`, or delete a test to force the suite green — that is the exact failure Rule 12 exists to prevent.

## Step 7 — Save the debug report

Save to `.agents/debug/[YYYY-MM-DD]-[short-slug].md`:

```markdown
# Debug: [short title]

**Date:** [date]
**Bug:** [the report]
**Status:** Fixed, verified in-browser, whole suite green  |  Unresolved after 4 iterations → see /rca

## Reproduction flow (browser-use, exact)
[ordered command list]

## Proving signal (chrome-devtools)
[the console stack / failed request / perf insight that proved the bug — verbatim]

## Root cause
[file:line + classification + one paragraph: why this is the cause, not a symptom]

## Fix
[files changed + summary]

## Verification
[flow re-run result + signal-absent confirmation + regression test name(s) + full-suite result: N passed / 0 skipped, or pre-existing unrelated failures listed]

## Not addressed
[related smells / design issues left for later]
```

Tear down: `browser-use close` and `chrome-devtools stop`.

## Step 8 — Hand off

State plainly whether the bug is fixed-with-the-whole-suite-green or unresolved. Do not commit (consistent with `/rca`, `/codex`). Per Rule 13, end with the concrete next move:

- Fixed + suite green → **Next:** `/commit` (stage the fix + the new regression test).
- Unresolved after 4 iterations → **Next:** `/rca "$ARGUMENTS"` (design-class — needs deeper trace).

## Rules

- Per Rule 4, the loop is goal-driven: success = flow passes AND proving signal gone AND the new regression test passes AND the whole suite is green with zero skipped. Not "I edited a file."
- Per Rule 1, cannot reproduce = stop and ask. Never fix a bug you haven't seen.
- Per Rule 12, after the 4-iteration bound, fail loud and escalate to `/rca`. Do not declare "fixed" on a signal you didn't re-verify.
- Per Rule 2 + Rule 3, smallest root-cause fix, no drive-by edits.
- Per the playbook §2: `browser-use` = hands, `chrome-devtools` = instruments. Don't use the screenshot tool to guess at what a console stack trace would tell you exactly.
- This command applies the fix autonomously (user chose the autonomous loop). It still does not commit — `/commit` is a separate, explicit step.
