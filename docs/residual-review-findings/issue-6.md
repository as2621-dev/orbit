# Residual review findings — issue #6 (digest.md markdown twin)

Advisory / human-call items deferred from the multi-agent review panel. All concrete,
evidenced defects the panel raised were fixed in the slice commit; these four are left as
notes because fixing them needs a refactor outside this slice's scope, belongs to a
different issue, or is genuinely low-impact for the LLM consumer.

## 1. `_trend_marker` hardcodes glyphs that duplicate `tiles._TRENDING_MARKER_SPEC` (Low-Medium)

`scripts/lib/markdown_render.py:_trend_marker` types the glyphs `↗ ◆ ○` and the
labels `dormant`/`external`/`N of yours` directly. These mirror
`tiles._TRENDING_MARKER_SPEC` (`scripts/lib/tiles.py:58-62`) and the inline `right_label`
logic in `tiles.render_trending_now` (`scripts/lib/tiles.py:422-427`). If a glyph/label
changes in tiles, the markdown marker drifts.

Deferred because: full reuse needs a small `tiles` refactor (the right-label text is inline
in `render_trending_now`, not a shared helper), and the glyphs are decorative in a text
file. Fix path: extract a `tiles.trending_marker_text(category, your_count) -> str` helper
and call it from both `render_trending_now` and `_trend_marker`.

## 2. Trio sub-derivations duplicated from `_build_ahead_trio` (Low)

`_build_trio` reproduces `render._build_ahead_trio`'s selection because that function
returns HTML (unavoidable). But two *pure* sub-derivations are copied verbatim: the scoop
attribution fallback chain (`render.py:_build_ahead_trio`, channel → creator → "your
network") and the hidden-gem field derivation (`creator.upper()`, `max(0, round(ratio*100))`).

Deferred because: unlike the masthead counts and tweet label (which were extracted and are
now shared), these values are computed *inside* `_build_ahead_trio` interleaved with HTML
assembly, so extracting them is a more invasive refactor of that function for less benefit.
Fix path: pull `render._scoop_attribution(...)` and `render._gem_fields(...)` out of
`_build_ahead_trio` and share them with `_build_trio`.

## 3. Fail-soft markdown write can leave a STALE `digest.md` (Low) — belongs to #7

`orbit._write_digest_markdown` is loud-but-non-fatal by design (a markdown failure must not
abort the HTML digest + email). Consequence: if today's write throws, the *previous run's*
`digest.md` persists while the pipeline still exits success. A future #7 reading the path
by mere existence could consume a stale digest.

Deferred because: this is inherent to the documented fail-soft tradeoff and belongs to #7's
own freshness guard, not this slice. Fix path (in #7): the markdown already carries the
run's dateline in its masthead — have #7 validate/stamp freshness (reject a stale dateline)
rather than trusting file existence.

## 4. `_md_link` does not escape `]`/`)` in link labels (Low)

A title containing `]` (e.g. `Watch [this]`) produces a technically-malformed
`[Watch [this]](url)`. Deferred because the deep-link URL still appears verbatim right after
the label (the tested round-trip contract holds), and the consumer is an LLM reading text
(#7), not a strict markdown renderer. Fix path if a strict renderer ever consumes this:
escape `[` / `]` in the label inside `_md_link`.
