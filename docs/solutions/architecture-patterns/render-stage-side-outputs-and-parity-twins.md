---
title: Render-stage side outputs must stay out of written_paths; parity twins reuse render.py's selection
tags: [render, digest, written_paths, deliver_email, attachments, markdown_render, parity, side-output, gotcha]
problem_type: pattern
symptoms: "a new file the render stage writes (digest.md, a JSON export, an RSS feed) gets silently mailed as a text/html email attachment; a markdown/alt-format twin drifts from the HTML digest (drops an item, shows a stale count)"
root_cause: "run_stage7_render's return value IS the email attachment list; and a twin renderer that reimplements render.py's content selection instead of reusing it drifts."
date: 2026-07-18
---

Two traps hit while adding `digest.md` (issue #6), the self-contained markdown twin, to the
render stage. Both bite the next agent who adds ANY output to `run_stage7_render`.

## 1. `written_paths` is the email attachment list — keep side outputs OUT of it

`orbit.run_stage7_render` returns `written_paths`. `run_stage7_deliver` passes that list
straight to `lib.deliver.deliver_email`, whose `_build_email_message` loops it and attaches
**every** path as a `text/html` attachment (`deliver.py` ~L238). So the natural instinct —
"I wrote a new file, append it to `written_paths`" — silently mails your `digest.md` (or JSON,
or RSS) as a broken HTML attachment, and breaks the two contract tests that assert
`written == [html_path]` / `[html_path, page_2_path]` (`test_orbit_pipeline.py`,
`test_orbit_unified_digest.py`).

Correct pattern for a secondary render output:
- Write it as a **side effect** via the SAME injectable `writer` seam the HTML pages use
  (so tests stay temp-dir-safe), at a **deterministic path** derived from page 1's dir.
- Do **not** append it to `written_paths`. The return value stays HTML-only.
- Make the path an **explicit shared helper** (`markdown_render.resolve_digest_md_path(html_path)`),
  so a downstream consumer (#7) derives it the same way instead of re-joining the path.
- Make the write **loud-but-non-fatal** (try/except → `log.log_error` with `fix_suggestion`,
  no re-raise). The HTML pages are written BEFORE the side write, so a side-write failure must
  not abort the primary HTML digest + email contract. This mirrors `deliver_email`'s own
  loud-but-non-fatal posture. See [[email-mime-delivery-gotchas]].

## 2. A parity "twin" renderer must REUSE render.py's selection, not reimplement it

The markdown twin must show every item the HTML shows. The durable way to guarantee that is
to import and call `render.py`'s own (underscore) content-selection helpers —
`group_items_by_tier`, `_card_deep_link`, `_chapter_rows`, `_trending_deep_link`,
`_masthead_counts`, `_tweet_source_label` — and only reimplement the *presentation*. Every
verbatim copy of selection logic is a drift vector the review panel will (rightly) flag:
duplicated masthead-count math and the tweet source-label were extracted into shared render.py
helpers precisely to kill that drift.

Non-obvious parity trap: `render.group_items_by_tier` uses `setdefault`, so an out-of-band
`density_tier` (anything outside the four constants) creates a fifth key. The HTML non-spilled
masonry renders every item regardless of tier, but a twin that iterates only the four known
tier headings would **silently drop** that item. Fold any leftover group under a generic
heading so the twin can never under-show relative to the HTML.
