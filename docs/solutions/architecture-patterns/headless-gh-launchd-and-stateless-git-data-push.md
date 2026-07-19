---
title: gh keyring auth works headless under a gui LaunchAgent; archive pushes via the stateless git-data API
tags: [gh, launchd, keyring, keychain, headless, archive, git-data-api, one-commit, chat-bridge, M7]
problem_type: pattern
symptoms: "fear that macOS keychain blocks `gh` under launchd; need a one-commit-per-run multi-file push from an unattended pipeline without a local clone that grows forever"
root_cause: "n/a — pattern entry (verified capability + push recipe from issue #7)"
date: 2026-07-18
---

Two reusable facts from the issue #7 archive slice (`scripts/lib/archive.py`):

**1. `gh` with keyring-stored auth WORKS under a LaunchAgent — verified, not assumed.**
`gh auth status` shows the token in the macOS *keyring*, which raises the classic
"keychain blocks headless" fear. Empirical check (2026-07-18): a one-shot plist in
`gui/$(id -u)` running `/opt/homebrew/bin/gh api user` (bootstrap → kickstart → read
stdout → bootout) returned the login with an empty stderr. LaunchAgents in the `gui`
domain run inside the user's login session where the keychain is unlocked — so the Orbit
pipeline can call `gh` headless with no auth workaround. (This does NOT extend to
LaunchDaemons or ssh sessions.) Also: `gh auth status` may display a stale account name;
`gh api user --jq .login` is the truth for which account the token acts as.

**2. One-commit multi-file push without a clone: the git-data API via `gh api`.**
A daily archive push must not `git clone` (even `--depth 1` downloads every previously
archived file at HEAD — cost grows forever) and must not use the contents API (one commit
PER file). The stateless recipe, constant cost per run:

```
gh api repos/R/git/ref/heads/BR   --jq .object.sha          # base commit
gh api repos/R/git/commits/SHA    --jq .tree.sha             # base tree
gh api repos/R/git/blobs  --method POST --input blob.json    # per file (base64)
gh api repos/R/git/trees  --method POST --input tree.json    # base_tree + entries
gh api repos/R/git/commits --method POST --input commit.json # parents=[base]
gh api repos/R/git/refs/heads/BR --method PATCH --input ref.json  # fast-forward, no force
```

Payloads MUST ride via `--input <file>` — a ~430KB base64 page as an argv element risks
the OS argv limit, and `subproc.run_with_timeout` sets `stdin=DEVNULL` so `--input -` is
unavailable. A concurrent-push race surfaces as a non-zero PATCH (non-fast-forward) —
treat it as a fail-soft skip, never `force: true`. Blobs uploaded before a failed step
are unreferenced objects GitHub GCs on its own — no cleanup path needed. Related:
[[launchd-scheduler-install-gotchas]] (the runner that hosts this), and the encoding rule
for `claude.ai/new?q=` links in [[headless-artifact-publish-spike-go-nogo]] (AC4:
percent-encode the ENTIRE prompt, `urllib.parse.quote(prompt, safe="")`).
