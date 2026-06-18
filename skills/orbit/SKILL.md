---
name: orbit
version: "0.1.0"
description: "Load your YouTube and X subscriptions, then surface a ranked daily digest of what's actually worth your attention from the feeds you already follow."
user-invocable: true
allowed-tools: Bash, Read, Write
---

# Orbit

Orbit reads the feeds you already follow (YouTube subscriptions, X follows), tracks
what you have already seen, and surfaces a ranked digest of the new, on-topic, high-signal
items — so you open one digest instead of ten feeds.

`scripts/orbit.py` is the entrypoint. This SKILL.md is a thin Bash-orchestration stub;
the full pipeline (delta fetch, classification, ranking, render) lands in later milestones.

## Entrypoint

Run the pipeline driver:

```bash
SKILL_DIR="<absolute path of the directory containing this SKILL.md>"
python3 "${SKILL_DIR}/scripts/orbit.py" --depth default
```

Flags:

- `--depth {quick,default,deep}` — how much work the pipeline does per run (default: `default`).
- `--setup` — run first-time setup (cookie source, interests, delivery). Stub for now.

## Status

Scaffold only (Phase 1, Sub-phase 1). Each stage currently logs a structured
`not yet implemented` notice. Stage 0 (subscription loading), the SQLite state store,
classification, ranking, and render are implemented in subsequent phases.
