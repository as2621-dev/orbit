---
description: Create a conventional git commit. Stages explicit files, never `-A`. Never amends, never skips hooks.
argument-hint: [optional: short description of the change]
---

# Commit

Create a clean git commit for the current changes. Follow the safety protocol in the global Claude instructions strictly.

## Step 1 — Inspect state (in parallel)

Run these in parallel:
- `git status` (no `-uall`)
- `git diff` (staged + unstaged)
- `git log --oneline -10` (to match style)

## Step 2 — Decide what to stage

- Identify the **logical unit** of change. If the diff contains two unrelated changes, ask the user whether to split into two commits.
- **Never use `git add -A` or `git add .`.** Stage specific paths.
- Refuse to stage files that look like secrets (`.env`, `*.pem`, `credentials*`, files with high-entropy tokens). If the user explicitly wants one staged, warn loudly first.

## Step 3 — Draft the commit message

Conventional commit format:

```
<type>(<scope>): <short summary in imperative mood>

<optional body — explain WHY, not WHAT. Wrap at 72 cols.>

<optional footer — e.g., closes #123>
```

Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`, `build`, `ci`.

- Title ≤ 72 chars, imperative ("add X" not "added X" / "adds X").
- If `$ARGUMENTS` provides a description, use it as the source for the title; refine for type/scope.
- Match the recent commit style from `git log` (Rule 11).
- Body is optional — include it when the WHY isn't obvious from the diff.

## Step 4 — Commit

- Stage the chosen files.
- Commit using a HEREDOC so the message formats correctly.
- **Do not** use `--amend`. **Do not** use `--no-verify`. **Do not** use `--no-gpg-sign`.
- If a pre-commit hook fails: fix the underlying issue, re-stage, create a NEW commit.

Run `git status` after the commit to verify success.

## Step 5 — Do NOT push, but DO anticipate next

Never push unless the user explicitly says so. Report the commit hash, then per Rule 13 surface the single most likely next move. Pick from this list based on context:

- If this commit was a phase-end commit from `/run-phase` and **more phases remain in `plans/`** → suggest `/run-phase plans/phase-[N+1]-*.md`
- If this commit was a phase-end commit and **no more phases remain** → suggest `/office-hours` or planning the next milestone with `/plan-phases`
- If this commit was a one-off fix → suggest `git push` (ask first) or "back to whatever you were doing"
- If branch is ahead of remote and the commit looks shippable → suggest `git push`
- If there are still **uncommitted changes** after this commit (you only staged a subset) → suggest a second `/commit` for the remaining hunk

Format example:
> Commit: `a1b2c3d` — `feat(auth): add session middleware`
>
> **Next:** Phase 2 still has uncommitted sub-phase reports — finish the phase or `/commit` them separately. Or run `/run-phase plans/phase-3-*.md`.

Never end with just the commit hash.

## Rules

- Per Rule 3, this command commits only what's there. It does not "tidy up" first.
- Per Rule 12, if hooks fail or anything was skipped, surface it. Don't claim success.
- One logical change per commit. If unsure, ask.
