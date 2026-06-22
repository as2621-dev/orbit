# Orbit — design language

**Why this doc exists:** it is the single, self-contained source of truth for the
digest's look. `/run-phase` and the renderer read THIS file, not the remote
library — so every token is copied in here verbatim. **When to update it:** when
the chosen design system changes, or when a tier/section needs a new token.

This supersedes the earlier "no remote design-references" decision recorded in
`plans/master-plan.md` and `references/design-brief.md`: Orbit now adopts a base
design system, but only by **inlining its tokens** — the page stays 100%
self-contained (no `<link>`, no `<script src>`, no web-font fetch), so the
master-plan's framework/weight rationale is preserved.

## Chosen base system

**Aura Editorial Features** — from the remote design-references library.

- URL: <https://www.aura.build/design-systems/aura-editorial-features>
- Library file: `design-systems/aura-editorial-features.md`
  (`https://raw.githubusercontent.com/ashesh2621/design-references/main/design-systems/aura-editorial-features.md`)
- Aura ID: `382423a7-421c-4184-bff4-bfcabae22d64`
- **Credit:** Meng To (@mengto), via aura.build "Neuform — top creators, featured".

**Why it fits:** the digest is a reading artifact — a ranked column of episodes a
person scans once a day. Aura Editorial Features is an editorial system: gold
accent on near-black, zinc borders, serif body, mono metadata. That maps cleanly
onto "rank controls density": serif card titles read as headlines, the gold
accent marks the deep-links (the product), and mono labels tag the metadata
(timestamps, TL;DR, section headings) without competing with the content.

## Tokens (lifted, inlined in `scripts/lib/html_render.py`)

Colors — anchored on the source palette (background `#000000`, text `#FFFFFF`,
accent `#E0A94E`, border `#27272A`). The source's `surface` is a light `#E5E5E5`
used on black; for the digest's dark cards it is adapted to a dark raised surface
that keeps the same role separation (bg → surface → border → text).

| Role | Dark (default) | Light (`prefers-color-scheme`) | Source token |
|---|---|---|---|
| `--bg` | `#000000` | `#faf8f4` | background `#000000` |
| `--surface` | `#141414` | `#ffffff` | surface (adapted to dark) |
| `--surface-2` | `#1c1c1c` | `#f3f1ec` | — (second raised tone) |
| `--text` | `#ffffff` | `#18181b` | text-primary `#FFFFFF` |
| `--muted` | `#a1a1aa` | `#52525b` | text-secondary `#A1A1AA` |
| `--accent` | `#e0a94e` | `#9a6b1e` | primary/accent `#E0A94E` (darkened for light-mode contrast) |
| `--border` | `#27272a` | `#e4e4e7` | border `#27272A` |

Spacing / shape — `--radius: 8px` (source `rounded.card: 8px`); spacing base 8px,
card padding 16–18px, max reading column 820px.

Typography — the source's web fonts are **adapted to system stacks** (design brief
§4 forbids web-font fetches), preserving the editorial intent:

| Source font | Role | System stack (`--font-*`) | Applied to |
|---|---|---|---|
| Playfair Display (serif) | body/display | `--font-serif`: Iowan Old Style / Palatino / Georgia / Times / serif | `.card-title` |
| Inter (sans) | UI | `--font-ui`: -apple-system / Segoe UI / Roboto / sans-serif | `body` base |
| JetBrains Mono | labels/metadata | `--font-mono`: ui-monospace / SF Mono / Menlo / Consolas / monospace | `.tldr-label`, `.section-heading`, `.chapter-time` |

## Sections to adopt

The four tier classes (`.hero` / `.standard` / `.compact` / `.index`) and the M3
placeholders (scoops strip, overlap block, trending rail) already exist in
`html_render.py`. This system's role is purely the palette + type layer over that
existing density structure — no markup change.

## Guardrails carried from the source

- Keep background / surface / text / border roles distinct (contrast pattern).
- Gold accent is reserved for links and active markers — it must read as clickable.
- Do not introduce a web-font fetch to chase Playfair/JetBrains exactly; the
  system fallbacks are the intended, self-contained substitute.
