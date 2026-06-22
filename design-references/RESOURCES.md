# design-references — local pointer + fetch recipes

The design library is a **separate public repo** (~1 GB), not vendored here.
`/cto` and `/run-phase` fetch from it on demand. This file is the local pointer
they read first (referenced from `CLAUDE.md`, `commands/cto.md`,
`commands/run-phase.md`).

Repo: <https://github.com/ashesh2621/design-references>
Raw base: `https://raw.githubusercontent.com/ashesh2621/design-references/main/`

Contents: 86 skills · 511 design systems · 2,827 components · 20,660 shared_code
templates (scraped from aura.build).

## ⚠️ Do NOT `curl` the indexes wholesale

The entry-point indexes are large and will flood your context with a wall of raw
markdown if catted directly:

| Index | Size | Don't |
|---|---|---|
| `design-systems/INDEX.md` | ~113 KB / 511 rows | ❌ `curl .../design-systems/INDEX.md` |
| `skills/INDEX.md` | ~17 KB | ❌ cat the whole thing |
| `components/INDEX.md` | large | ❌ cat the whole thing |

**Always narrow FIRST, then fetch only the 2-3 items you actually chose.** Each
individual `<slug>.md` is small (~4-5 KB) — those are fine to fetch in full.

## Recipe A — narrow by keyword, then fetch (preferred)

Filter the index server-side-ish with `grep` so only matching rows enter context:

```bash
BASE=https://raw.githubusercontent.com/ashesh2621/design-references/main
# 1. Filter the index to candidate rows only (NOT the whole file):
curl -s "$BASE/design-systems/INDEX.md" | grep -iE 'editorial|dense|dashboard' | head -15
# 2. Fetch ONLY the chosen slug(s) in full (small files):
curl -s "$BASE/design-systems/<slug>.md"
curl -s "$BASE/design-systems-html-previews/<slug>.html"   # optional visual
```

## Recipe B — GitHub code search (no index download at all)

Best when you have a keyword and want paths without touching the index. Use the
GitHub MCP `search_code` tool, or `gh` if available:

```bash
gh api -X GET search/code \
  -f q="<keyword> repo:ashesh2621/design-references path:design-systems" \
  --jq '.items[].path'
```

## Library layout

| Path | What |
|---|---|
| `design-systems/<slug>.md` | One system: color/type/spacing tokens (YAML front-matter) + guidance + creator credit. **This is what `/cto` lifts tokens from.** |
| `design-systems-html-previews/<slug>.html` | Rendered preview of a system. |
| `skills/<name>.md` | Animation / layout / interaction patterns. |
| `components/html/<slug>.html` | Starter HTML (hero, pricing, dashboard, card, …). |
| `components/<slug>.json` | Component metadata incl. `credit_name`. |
| `shared_code/` | Reusable code templates. |

## Token-lifting workflow (what `/cto` does)

1. Narrow (Recipe A or B) → pick 2-3 candidate systems.
2. Fetch each candidate's `<slug>.md` in full.
3. Propose ONE to the user with a one-line rationale each.
4. Copy the chosen system's **actual token values** into `reference/design-language.md`
   — that file is the self-contained source of truth the renderer reads; the
   remote system is never fetched at render time.
5. Credit the original creator (front-matter author / `credit_name`) in
   `reference/design-language.md` and in any lifted-HTML code comments.

> Self-contained constraint (Orbit): if a chosen system uses web fonts, adapt
> them to system-font stacks — do not add a `<link>`/`@import` font fetch. See
> `reference/design-language.md` for the live example.
