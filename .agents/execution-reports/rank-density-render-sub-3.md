# Execution report — Phase 3, Sub-phase 3: Render the HTML one-pager (Stage 7a)

## What was built
The HTML one-pager renderer (Stage 7a), split into a primitives layer and an
orchestration layer per the file-size discipline, plus the design brief and DoD tests.

- **`skills/orbit/scripts/lib/html_render.py`** (498 LOC) — HTML/CSS primitives:
  - `HTML_TEMPLATE` (sentinel `__TITLE__`/`__CSS__`/`__BODY__`) + `CSS` (self-contained
    inline dark/light stylesheet, tier classes `.hero`/`.standard`/`.compact`/`.index`).
    No external/CDN fetches, no `<link>`/`<script src>`.
  - `wrap_page(title, body_html)` — the sentinel swap.
  - XSS layer: `_SAFE_LINK_SCHEMES` allowlist, `is_safe_link_url(url)` (lifted/adapted
    from the reference), `safe_href(url)` (validates raw URL FIRST, then escapes; unsafe
    -> `"#"`), `escape(text)` wrapper.
  - Element builders: `render_card`, `render_compact_row`, `render_chapter_list`,
    `render_index_line`, `render_index_strip`, `render_tldr`, `render_meta_line`,
    `render_link`, plus `_format_timestamp` (`M:SS`/`H:MM:SS`) and `_format_count`.
- **`skills/orbit/scripts/lib/render.py`** (242 LOC) — orchestration:
  - `render_digest_html(tiered_items, config=None) -> str` — assembles the design-brief
    layout: TL;DR header, (absent scoops strip M3), Hero/Standard full cards WITH
    deep-link chapter lists, Compact rows, (absent right-rail M3), bottom "they also
    posted" Index strip. Logs `render_completed` with per-tier counts.
  - Seam helpers for Sub-phase 4: `group_items_by_tier`, `_render_main_cards_section`,
    `_render_index_section`, `_count_distinct_creators`, `_card_deep_link` (whole-video
    `&t=0s`).
- **`skills/orbit/references/design-brief.md`** — self-contained look spec (tier->density
  table, top-to-bottom layout sketch, style notes, safety rules, M3/Sub-phase-4 scope).
- **`tests/test_render.py`** — 8 tests covering the sub-phase-3 DoD (below).

## Divergences from the reference (and why)
- The reference `render.py` is markdown-report rendering; not reused. Only
  `html_render.py`'s sentinel-template shape and the link-safety allowlist were lifted,
  restyled fresh for Orbit's four-tier card system (the reference has no tier system).
- `safe_href` validates the **raw** URL against the allowlist BEFORE `html.escape`,
  rather than relying on the reference's "must be pre-escaped" precondition. This is
  strictly safer here: a literal `javascript:` colon is detected on the raw string, so
  there is no entity-decode bypass risk. Kept the control-char rejection from the
  reference.
- `config` is optional (`config=None`) and only consulted for an optional `digest_title`
  via `getattr` — `lib.config.OrbitConfig` has no title field today, so the default
  `"Orbit · Today"` is used. Defensive, no crash if config is absent (orbit.py wiring
  is Sub-phase 4).

## Self-review findings + fixes
- **[High] XSS** — verified raw `javascript:alert(1)` and `data:text/html,<script>` both
  collapse to `"#"`; `<script>`/`<img onerror>` in title/channel are escaped to inert
  text. Covered by `test_malicious_title_and_url_are_neutralized` + a unit test on
  `safe_href`. No fix needed; design held.
- **[Low] doctest log noise** — `render_digest_html`'s docstring example emitted the
  `render_completed` log line to stdout during doctest. Added `# doctest: +SKIP` to that
  one example, matching the rerank.py/density.py convention for logging functions.
- **[Low] empty containers** — confirmed `render_chapter_list([])` and
  `render_index_strip([])` return `""` so chapter-less cards and item-less days emit no
  dangling empty `<ul>`/`<section>`. Covered by edge tests.
- File sizes: 242 / 498 LOC — both well under the 1000 (and html_render under render's
  500 target was met for render.py).

## Validation results
- `python3 -c "... from lib import render, html_render"` -> `import ok`.
- `uv run --with pytest pytest tests/ -q` -> **66 passed** (58 pre-existing + 8 new). PASS.
- Doctests: html_render 15/15 pass; render example skipped (logs to stdout, assertion
  still correct).
- Eyeball sample written to gitignored `out/sample-digest.html` (NOT in repo tree);
  confirmed correct `&t=0s`/`&t=90s`/`&t=3725s` hrefs, timestamps `0:00`/`1:30`/`1:02:05`,
  hero+standard cards with chapter lists, compact row, index strip.

## Definition of done — PASS
1. **Deep-link survives to HTML** — PASS. `test_chapterized_hero_chapter_deep_link_survives_to_html`
   asserts `href="https://www.youtube.com/watch?v=vidHERO&amp;t=90s"` in output (Chapter
   `start_seconds=90.0`).
2. **Tier -> density** — PASS. `test_hero_gets_chapters_index_goes_to_also_posted_section`
   asserts `card hero` + `chapters` markers, and the index id appears specifically inside
   the sliced `index-strip` section while the hero chapter link sits in the cards section.
3. **XSS allowlist** — PASS. `javascript:` never emitted as a clickable href (collapses to
   `chapter-link href="#"`); `<script>`/`<img onerror>` escaped (`&lt;script&gt;` present,
   no raw tag).
4. **TL;DR present** — PASS. `test_tldr_header_present` asserts `class="tldr"` and the
   pure-count summary `2 episodes from 2 creators today`.
5. **Happy/empty/no-chapters** — PASS. Valid `<!DOCTYPE html>...</html>` always; empty
   list -> `0 episodes...`; chapter-less hero card renders with no `chapters` container.

## Concerns + the structure note for Sub-phase 4
**No blocking concerns.** render.py is structured so pagination plugs in WITHOUT a rewrite:

- **The seam:** `render_digest_html` builds the body from three already-split pieces:
  `render_tldr(...)`, `_render_main_cards_section(grouped)` (Hero+Standard+Compact), and
  `_render_index_section(grouped)` (Index). `grouped` comes from the public
  `group_items_by_tier(tiered_items) -> dict[str, list[TieredItem]]`.
- **How Sub-phase 4 plugs in:** add `estimate_page_height(tiered_items) -> int` and a
  paginating wrapper inside (or alongside) `render_digest_html`. When the estimate crosses
  `PAGE_1_BUDGET_PX`, build TWO `grouped`-like dicts — page 1 keeps Hero+Standard (call the
  same `_render_main_cards_section` with Compact/Index emptied + a "continued on page 2 ->"
  link), page 2 reuses `_render_main_cards_section` (Compact only) + `_render_index_section`
  (Index). Both pages still go through `wrap_page`. The `render_completed` log call is a
  single statement with kwargs only — add `page_count`/`spilled` as additive kwargs, no
  restructure.
- **Suggested signature for the spill:** keep `render_digest_html(tiered_items, config)`
  returning page-1 HTML; consider returning a small `(page1_html, page2_html | None)` or
  writing page 2 in orbit.py — Sub-phase 4's call, both fit the current seam since the
  section builders are pure functions of `grouped`.
- One naming note for whoever wires orbit.py: the page-2 filename in the plan is
  `today-page2.html`; render.py currently does no file I/O (returns a string) — that
  belongs in orbit.py per the sub-phase-4 file boundary.
