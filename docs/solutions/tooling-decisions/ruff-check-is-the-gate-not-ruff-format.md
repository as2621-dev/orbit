---
title: The lint gate is `ruff check .`, not `ruff format` — do not run repo-wide format
tags: [ruff, lint, format, verification-contract, tooling]
problem_type: tooling
symptoms: "`ruff format --check .` reports ~30 files 'would reformat' even on a clean tree; a slice's edited files also show as 'would reformat'"
root_cause: "The repo was never `ruff format`-clean at baseline; only `ruff check .` (Pyflakes/lint) is enforced. `pyproject.toml` sets line-length 120 + double quotes but nothing runs `ruff format` in the gate."
date: 2026-07-18
---

Orbit's verification contract for a slice is **`ruff check .`** (must be clean) plus the
pytest suite — see any slice issue's acceptance criteria ("Test suite is green and
`ruff check .` is clean"). `ruff format` is **not** part of the gate.

At baseline `ruff format --check .` reports ~30 files "would reformat" (as of 2026-07-18).
This is pre-existing drift, not something a slice introduced — the repo predates any
format enforcement.

**How to apply next time:**

- To satisfy the gate, run `.venv/bin/ruff check .` (and `--fix` for the pre-existing
  F401s the issues flag). Do **not** run `ruff format .` across the repo — it reformats
  ~30 untouched files and balloons your diff, violating surgical-change discipline
  (CLAUDE.md Rule 3).
- If `ruff format --check` flags *your* changed files, run `ruff format --diff <file>` and
  check **where** the changes fall. In practice the flagged lines are in code the diff
  didn't author (pre-existing long f-string `raise`/`log_error` calls, multi-line call
  wrapping). Leave those verbatim — preserving existing style beats a cosmetic reformat
  that diverges from the other 30 untouched files.
- When you fully rewrite a file (e.g. via Write), still match the file's existing
  formatting rather than the formatter's ideal, so the rewrite stays consistent with the
  rest of the repo that the gate accepts as-is.
