# Orbit digest — design brief (HTML one-pager, Stage 7)

The self-contained look spec for the rendered daily digest. Authored here from the
master-plan brief §3 Stage 7; iteration expected (this is M1's riskiest-assumption
surface — running it on real subs tunes the look and the tier laddering). The
palette/type layer is lifted from the "Aura Editorial Features" design system
(see `reference/design-language.md`), but **tokens-only and inlined**: the page
stays a single self-contained HTML file with an inline `<style>`, NO external/CDN
fetches, NO `<link>` / `<script src>`, NO web-font fetch.

## 1. The one principle

**Rank controls density, never inclusion.** Every item the pipeline saw appears
somewhere on the page. A high-rank item gets a big, scannable card; a low-rank or
classification-failed item shrinks to a one-line "they also posted" entry — but it
is never dropped. The visual hierarchy IS the ranking, made legible at a glance.

## 2. Tier → density mapping

| Tier | Source | Visual treatment |
|---|---|---|
| `hero` | top of the passing distribution | Large card: big title, channel, engagement line, full chapter list (each chapter a deep-link), card links to the video at `t=0s`. |
| `standard` | next band | Medium card: title, channel, engagement, full chapter list, card deep-link. Same anatomy as hero, smaller type / tighter spacing. |
| `compact` | next band | Condensed row: one line — title (linked) + channel + a short meta. No chapter list. |
| `index` | bottom band + all classification-failed items | "They also posted" strip at the page bottom: one compact line per item, title linked to the video. |

The four tier names map 1:1 to CSS classes `.hero` / `.standard` / `.compact` /
`.index`. Within a tier, items keep their descending rank order (the input is
already rank-ordered by `derank_items`).

## 3. Page layout (top to bottom)

> **Layout note (Tiles, build-from-prefs):** the main cards now render as a
> responsive **tile grid** split into two source sections — **Videos** then
> **From X** — each keeping its rank-based size ladder (Hero tile spans the full
> row; Standard/Compact are single-column tiles). Index-tier items still share
> the bottom "they also posted" strip. This is an interim pass pending the final
> "Orbit - Tiles" design; the section anatomy below still holds per tier.

```
+------------------------------------------------------------------+
|  TL;DR:  N episodes from M creators today                        |  <- one-line header
+------------------------------------------------------------------+
|  [ Scoops strip ]   (M3 — renders empty/absent in M1)            |
+-------------------------------------------+----------------------+
|  CREATOR EPISODE CARDS (main column)      |  Right rail:         |
|                                           |  Trending            |
|  +-------------------------------------+  |  (M3 — empty in M1)  |
|  | HERO CARD                           |  |                      |
|  |  Title (links to watch?v=ID&t=0s)   |  |                      |
|  |  Channel · 12.3k views              |  |                      |
|  |  Chapters:                          |  |                      |
|  |   - 0:00  Intro   (deep-link)       |  |                      |
|  |   - 1:30  Topic   (&t=90s deep-link)|  |                      |
|  +-------------------------------------+  |                      |
|  +-------------------------------------+  |                      |
|  | STANDARD CARD (chapter list too)    |  |                      |
|  +-------------------------------------+  |                      |
|  | compact row · compact row · ...     |  |                      |
+-------------------------------------------+----------------------+
|  THEY ALSO POSTED  (index strip)                                 |
|   · title · title · title ...   (each links to its video)        |
+------------------------------------------------------------------+
```

### TL;DR header
A single line derived by pure counting (no LLM, Rule 5): the number of items and
the number of distinct creators in the batch, e.g. `7 episodes from 4 creators
today`. It is the "is this worth my time" glance.

### Scoops strip (M3 placeholder)
A reserved section that renders empty/absent in M1 — clustering / scoops are M3.
Do not fabricate content. The section is omitted from the body when empty.

### Creator episode cards
The spine of the page. Hero and Standard tiers render full cards: a deep-linked
title, the channel name, an engagement meta line, and — when the item is a
long-form chapterized episode — a chapter list where **each chapter is an `<a>`
to its `watch?v=ID&t=Ns` deep-link** (the headline feature). Compact tier renders
condensed single-line rows with a linked title, no chapter list. An item with no
chapters simply renders its card without a chapter list (no empty container).

### Right-rail trending (M3 placeholder)
Reserved column, renders empty/absent in M1.

### "They also posted" index strip
The page bottom collects all `index`-tier items (low-rank + classification-failed)
as compact lines, each linking to its video. This is where "rank controls density
never inclusion" is most visible: nothing was dropped, it just shrank.

## 4. Visual style

- Self-contained inline `<style>`. Dark-friendly neutral palette with a light
  fallback via `prefers-color-scheme`. System font stack (no web-font fetch).
- A constrained max-width reading column for the main cards; the index strip and
  TL;DR span full width.
- Cards have clear tier-distinguishing weight: hero largest, standard medium,
  compact a single row, index a dense line list.
- Links are visually obvious (underline / accent color) — the deep-links are the
  product, so they must read as clickable.

## 5. Safety (non-negotiable)

- Every URL that becomes an `<a href>` passes a scheme allowlist
  (`http` / `https` / `mailto`); anything else (e.g. a malicious `javascript:`
  title used as a link) is dropped to a non-clickable `#`.
- Every user-controlled string (title, channel name, chapter title) is
  `html.escape`-d so a `<script>` in a title renders as inert text, never markup.
- Chapter deep-links are trusted constructed URLs (built by `build_deep_link`),
  so they pass the allowlist and survive escaping intact.

## 6. Out of scope for M1 (render empty/absent)

Scoops strip, right-rail trending, clustering/overlap block (all M3). Page-2 spill
and the per-tier height-estimate budget are Stage 7b (Sub-phase 4) — added on top
of this renderer without changing the card/tier anatomy above.
