# Phase 8 · Sub-phase 1 — Clickable timestamps + grounded blurbs

**Status: SUCCESS**

## What shipped
- **Clickable chapter chips.** `tiles.ChapterRow` gained `url: str = ""`. New helper
  `tiles._render_kp_chip` renders `<a class="chip" href="{safe_href(url)}">` when the url
  is non-empty AND passes `is_safe_link_url`; otherwise it degrades to the original inert
  `<span class="chip">` (Rule 12). The visual `.chip` class is kept in both forms, so the
  two look identical.
- **Deep-link threaded through render.** `render._chapter_rows` now passes each
  `chapter.deep_link` into `ChapterRow.url` (the `Chapter` dataclass already carried a
  real `watch?v=ID&t=Ns` deep-link — verified, not rebuilt).
- **Card link lands on content start.** `render._card_deep_link` now returns the FIRST
  chapter's `deep_link` for a chaptered item, keeping the `card_url` short-circuit for X
  items and the `&t=0s` fallback for chapterless items. `_trending_deep_link` inherits
  this for free (it already delegates to `_card_deep_link` for resolved items; its own
  unresolved fallback has no chapters, so it correctly stays `&t=0s`).
- **Grounded blurbs.** `summarize.summarize_items` adds a 4th tab column — the item's
  chapter-title outline (`_chapter_outline`, first 6 titles joined `" · "`, hard-capped
  at 160 chars; empty for chapterless items). `references/summarize.md` now instructs the
  model to ground each blurb in the chapter outline, never invent content, and keeps the
  existing ≤140-char / no-restating-title rules. Transcripts are NOT threaded through
  (they aren't retained past chapterize — chapter titles are the grounding source, per
  the phase's Out-of-scope note).

## Files modified (only the assigned six)
- `scripts/lib/tiles.py` — `ChapterRow.url`, `_render_kp_chip`, `is_safe_link_url` import.
- `scripts/lib/render.py` — `_chapter_rows` url pass-through, `_card_deep_link` first-chapter logic.
- `scripts/lib/summarize.py` — `_chapter_outline`, `MAX_OUTLINE_CHAPTERS/CHARS`, 4th block column.
- `references/summarize.md` — grounding instructions + 4th column header.
- `tests/test_render.py` — 5 new tests; updated 2 assertions for the new clickable/first-chapter behavior.
- `tests/test_summarize.py` — 2 new tests (outline column + chapterless empty column).

## Code review (self)
- **Security (href escaping) — checked, no findings.** Chip anchors gate on
  `is_safe_link_url` then escape via `safe_href` (`&`→`&amp;`, quotes escaped). A
  `javascript:` chapter link degrades to the span form. `_card_deep_link` may now return
  an unsafe first-chapter link, but every consumer wraps it in `safe_href` at render time
  (title link, more-chapters link, compact chip), so `test_malicious_title_and_url_are_neutralized`
  still passes — the unsafe scheme collapses to `#`, never a clickable payload.
- **Logic — checked.** Chaptered→first offset, X→card_url, chapterless→`&t=0s`. Outline
  handles both object and dict items, caps at 6 titles / 160 chars, empty when none.
- No critical/high/medium issues found.

## Validation
- `pytest tests/test_render.py tests/test_summarize.py tests/test_scoops_and_render.py -q` → **43 passed**.
- `pytest tests/ -q` (full suite) → **225 passed**.
- `ruff check` on all touched files → **All checks passed!**
- `ruff format --check` → the 5 code/test files "would be reformatted", BUT this drift is
  **pre-existing** (confirmed via `git stash`: HEAD already fails format on all five, from
  single-vs-double quote styling the repo never normalized). Per Rule 3 I did not reformat
  lines I didn't author. My own added/edited lines are format-clean (verified with
  `ruff format --diff` — no hunk falls in my new functions). Making `ruff format --check`
  green would require a repo-wide reformat (~50 cosmetic hunks across untouched code),
  which is out of scope for this surgical sub-phase.

## Definition of done: PASS
- chapter with `deep_link` → escaped `<a>` chip — PASS (`test_chapter_chip_with_deep_link_renders_clickable_anchor`)
- empty url → old `<span>`, no `<a href="">` — PASS (`test_chapter_chip_without_url_degrades_to_inert_span`)
- `javascript:` url → span form — PASS (`test_chapter_chip_with_javascript_url_is_neutralized_to_span`)
- chaptered card uses first chapter offset; chapterless keeps `&t=0s` — PASS (`test_card_link_lands_on_first_chapter_offset_when_chaptered`, `test_card_link_falls_back_to_video_start_when_chapterless`)
- prompt carries chapter-title column + truncates long outlines — PASS (`test_summarize_items_prompt_carries_chapter_outline_and_truncates`)
- three named test files green — PASS (43 passed)

## Concerns for the orchestrator
1. **Pre-existing `ruff format` drift (not mine).** All five touched files fail
   `ruff format --check` at HEAD already. If a phase-level gate requires format-green,
   consider a separate repo-wide `ruff format` commit — do NOT attribute it to this
   sub-phase's behavior change.
2. **`_card_deep_link` can surface an unsafe first-chapter link.** Safe today because
   every render-time consumer wraps it in `safe_href`. If a future caller uses the raw
   return without `safe_href`, that guard is gone — noting for defense-in-depth.
