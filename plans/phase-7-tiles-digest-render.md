# Phase 7: Tiles digest render (newspaper layout from real data)

**Milestone:** M5 — the digest looks like the product
**Status:** Sub-phase 0 (foundation) done; Sub-phases 1–4 not started
**Estimated effort:** L

## Goal
Orbit's digest renders as the **`Orbit - Tiles.dc.html`** newspaper layout (imported from the claude.ai Design project `/orbit`, projectId `08e7a7a1-1c4c-4ba5-bc21-1b2e737c1270`) driven by **real pipeline data** — masthead, an LLM editorial verdict, an "Ahead of the curve" trio (scoop / trending-now / hidden-gem), a 3-column ranked masonry of hero/standard/compact/tweet tiles with **base64-inlined YouTube thumbnails**, chapter deep-link chips, "same story, also covered" cross-links, **unavatar.io profile pics on tweet tiles**, and a footer — all **fully self-contained** (no CDN at open; fonts already inlined).

## Decisions locked with the user (2026-06-29) — do NOT re-litigate
- **Images: base64-inlined.** Self-contained; fetched at build time on the run machine, never at digest open. YouTube thumbs derive from `video_id`; X avatars from `https://unavatar.io/twitter/{handle}`.
- **Avatars are an extension.** The design itself has NO profile pics (tweet tiles are text-only). The user chose to add unavatar avatars to tweet tiles — a deliberate deviation.
- **Prose: LLM-generated** via the existing claude-CLI path (`lib/llm.py`), Rule 5 (summarization/judgment is a valid LLM use). The top verdict sentence + a one-line per-tile blurb.
- **Fonts: base64 woff2 inlined**, latin subset only, variable-font deduped to one `@font-face` per family (~308KB). Already built.
- **Graceful degradation (no fabrication, Rule 12):** subscriber counts are NOT captured → the hidden-gem "3.1k subs" is **omitted**; per-item clock times are NOT available (only `upload_date`) → omitted; if the LLM is unavailable, verdict + blurbs render **empty/absent**, not faked.

## Current code map (verify before editing — Rule 8)
- `scripts/lib/render.py` — orchestration. Has `render_digest_pages` / `render_digest_html`, `group_items_by_tier`, `_render_scoops_strip`, `_render_overlap_block`, `_render_trending_rail`, page-spill logic. Consumes `density.TieredItem`.
- `scripts/lib/html_render.py` — markup/CSS primitives: `escape`, `is_safe_link_url`, `safe_href`, `render_link`, `render_chapter_list`, `render_card`, `render_compact_row`, `wrap_page`, `HTML_TEMPLATE`, `CSS`.
- `scripts/lib/rerank.py` — `RankableItem` (`item_external_id`, `title`, `channel_name`, `creator_external_id`, `view_count`, `like_count`, `comment_count`, `duration`?, `chapters`, `card_url`), `from_upload`, `from_tweet`, `ScoredItem`.
- `scripts/lib/density.py` — `TIER_HERO/STANDARD/COMPACT/INDEX`, `TieredItem`, `assign_density_tiers`.
- `scripts/lib/trending.py` — `TrendingItem` (`title`, `item_external_id`, `card_url`, `corroboration_tag`, `is_scoop`, velocity field).
- `scripts/lib/cluster.py` — `Cluster` (`member_item_ids`, `cross_links`, `representative_item_id`, `source_diversity`).
- `scripts/lib/llm.py` — claude-CLI subscription path (no API key); see [[orbit-llm-wiring-and-gaps]] and headless-auth fix commit `bad40eb`.
- `scripts/orbit.py` — pipeline driver; calls `render` + `deliver`; `_build_delivery_summary` (no-LLM).

## Sub-phase 0 — DONE (foundation, already committed-pending)
Font vendoring + self-contained static reference. Files: `scripts/build_fonts.py` (Google Fonts → `scripts/assets/fonts/fonts-inline.css`, latin-only, variable-font deduped), `scripts/build_tiles_reference.py` (strips `<x-dc>`/`<helmet>`/`support.js`, inlines fonts → `out/orbit-tiles-reference.html`), `scripts/assets/orbit-tiles.dc.html` (raw design). Verified: renders offline, 3 valid woff2 `@font-face`, 14 tiles. **This `out/orbit-tiles-reference.html` is the visual target.**

## Sub-phases

### Sub-phase 1: Image pipeline + RankableItem fields + safe img-src
- **Files touched:** `scripts/lib/images.py` (new), `scripts/lib/rerank.py`, `scripts/lib/html_render.py`, `tests/test_images.py` (new)
- **What ships:**
  - `lib/images.py`: `derive_youtube_thumb_url(video_id) -> str` (`https://i.ytimg.com/vi/{id}/mqdefault.jpg`), `derive_avatar_url(handle) -> str` (`https://unavatar.io/twitter/{handle}`), and `fetch_and_inline(url, *, max_bytes=150_000) -> str | None` that fetches via stdlib `urllib` (browser UA), sniffs `Content-Type` (must be `image/*`), enforces the size cap, base64-encodes to `data:{mime};base64,...`, **disk-caches** keyed by URL hash under `XDG_CACHE_HOME/orbit/images/` (re-runs don't re-fetch), and **fails soft**: returns `None` + logs `image_inline_failed` with `fix_suggestion` on any error (404, timeout, non-image, oversize). No exception escapes.
  - `rerank.py`: add `image_url: str = ""` and `summary: str = ""` to `RankableItem`; set `image_url` in `from_upload` (YouTube thumb from `video_id`) and `from_tweet` (avatar from `handle`). Producers untouched.
  - `html_render.py`: add `safe_img_src(src) -> str` — allow `http`/`https` and `data:image/(png|jpe?g|webp|gif|avif);base64,`; reject `data:text/html`, `javascript:`, control chars → `""` (img dropped, never a script payload). Mirrors `safe_href` but for the `<img src>` sink.
- **Definition of done:** `tests/test_images.py` (mocking `urllib`): happy path returns a `data:image/jpeg;base64,...` URI; a 404/timeout returns `None` and logs `image_inline_failed`; an oversize/`text/html` response returns `None`; a second `fetch_and_inline` of the same URL reads the disk cache (mock called once). A `safe_img_src` test: `data:image/png;base64,x` passes, `data:text/html,<script>` and `javascript:alert(1)` → `""`. `derive_*` URL builders assert exact format. RankableItem round-trip: `from_upload` sets the ytimg URL, `from_tweet` sets the unavatar URL. No test hits the network.
- **Dependencies:** none (Sub-phase 0 foundation already done)

### Sub-phase 2: LLM verdict + per-item summary blurbs
- **Files touched:** `scripts/lib/summarize.py` (new), `references/summarize.md` (new prompts), `scripts/lib/rerank.py` (populate `summary`), `tests/test_summarize.py` (new)
- **What ships:**
  - `lib/summarize.py`: `summarize_items(items) -> dict[item_external_id, str]` (one ≤140-char editorial blurb per top-tier item) and `synthesize_verdict(tiered_items, scoops, clusters) -> str` (the one masthead sentence — "Quiet day. The only real story is …"). Both go through `lib/llm.py`'s claude-CLI path, batched into a single call where possible (Rule 6 token discipline), prompts pulled from `references/summarize.md`. **Fail-soft:** any LLM error → return `{}` / `""` so the renderer degrades to structural-only. Only Hero/Standard tiers get blurbs (cost control); Compact/Index/tweets do not.
  - Wire results onto `RankableItem.summary` in the orchestration (Sub-phase 4 reads them).
- **Definition of done:** `tests/test_summarize.py` mocks the `llm` call: `summarize_items` returns a blurb keyed per id and truncates >140 chars; `synthesize_verdict` returns the mocked sentence; an LLM exception yields `{}` / `""` (degradation intent — a flaky LLM must never break the digest, Rule 12). Verdict prompt includes scoop + cluster context. No test invokes the real CLI.
- **Dependencies:** Sub-phase 1 (for the `summary` field)

### Sub-phase 3: Tiles markup + CSS in html_render
- **Files touched:** `scripts/lib/html_render.py`, possibly new `scripts/lib/tiles.py` if `html_render.py` would exceed ~900 lines (keep <1000, CLAUDE.md), `tests/test_render.py` (extend)
- **What ships:** Replace the current `CSS` + `HTML_TEMPLATE` with the Tiles stylesheet (the `.tile`/`.ph`/`.chip`/`.kp` classes + inline-style structure from `scripts/assets/orbit-tiles.dc.html`) and a `wrap_page` that injects the inlined font CSS read from `scripts/assets/fonts/fonts-inline.css` (cached read). New builders, each pure + escaped + allowlisted:
  - `render_masthead(date_str, source_total, accounted, scoop_count, dormant_count, cluster_count)`
  - `render_verdict(verdict_html_safe)` — bold/italic accent spans; empty → `""`
  - `render_ahead_trio(scoop_tile, trending_tile, gem_tile)` — the 3-col grid; each sub-builder: `render_scoop_tile`, `render_trending_now` (markers ◆ dormant / ↗ N-of-yours / ○ external), `render_hidden_gem` (velocity %, NO subs)
  - `render_feed_masonry(tiles_html)` + per-tile `render_hero_tile` / `render_standard_tile` / `render_compact_tile` / `render_tweet_tile` — thumbnail via `<img src=safe_img_src(image_url)>` (fallback to the `.ph` hatched placeholder when `image_url` empty), `TOP SIGNAL`/`BEST ON …` flags, chapter `kp` rows + "+N more chapters", "same story, also covered" cross-links, tweet avatar `<img>` (extension)
  - `render_footer(accounted_str, page_2_href)`
- **Definition of done:** extended `tests/test_render.py`: every builder escapes a `<script>` title to inert text; `safe_img_src` is used for every `<img>`; a tile with empty `image_url` falls back to `.ph` (no broken `<img>`); the trio renders the right marker per trending category; an empty verdict/blurb omits its element (no empty container). A golden-ish structural assertion that the assembled page contains `@font-face` + `class="tile"` and no `fonts.googleapis.com`.
- **Dependencies:** Sub-phase 1 (`safe_img_src`)

### Sub-phase 4: Orchestration rewrite + pipeline wiring + sample render
- **Files touched:** `scripts/lib/render.py`, `scripts/orbit.py`, `tests/test_render.py` / `tests/test_orbit_unified_digest.py` (update)
- **What ships:** `render.py` re-orchestrated to assemble the Tiles body from `tiered_items` + `clusters` + `trending_items` + `scoops` + the verdict/summaries: masthead (counts from sources/scoops/clusters) → verdict → ahead-of-curve trio (top scoop, the trending list, top-velocity hidden gem) → ranked masonry (Hero/Standard/Compact tiles by tier, tweet tiles for X items, thumbnails inlined via `images.fetch_and_inline` at render time, cross-links from clusters) → footer. **Invariant preserved:** rank controls density, never inclusion — every item still appears (Hero big tile … down to a compact/tweet tile). Keep or adapt the 2-page spill (`page_2_href` → "Full archive · page 2 →" footer link). `orbit.py`: call `summarize`/`verdict` (Sub-phase 2) before render, pass through; thread `image_url` inlining. Update existing render tests for the new structure.
- **Definition of done:** `pytest tests/` green. Running the pipeline on a stubbed batch (or the existing sample fixtures) produces an `out/today.html` that is byte-checked self-contained (no `fonts.googleapis.com`, no `<script src`), contains real `data:image/...` thumbnails for YouTube items + `data:` avatars for tweets, the verdict sentence, and one tile per input item. A headless-Chrome screenshot (`out/today.png`) visually matches `out/orbit-tiles-reference.html`. Empty-batch still yields a valid page.
- **Dependencies:** Sub-phases 1, 2, 3

## Phase-level definition of done
From a clean checkout: `pytest tests/` passes; `python scripts/orbit.py --depth quick` (mocked sources/network) renders `out/today.html` in the Tiles layout, fully self-contained, with inlined thumbnails + avatars + LLM verdict, degrading gracefully (no subs, no clock times, empty prose if LLM down) without fabrication. Screenshot matches the reference. README updated with the `build_fonts.py` step + the new self-contained-image note.

## Out of scope
- Capturing YouTube subscriber counts or exact post times (would unlock real "3.1k subs" / "14:20" — separate phase).
- Real hidden-gem "outside your network" external velocity beyond what `external_trending` already provides.
- Re-rendering the claude.ai Design project (this is a one-way import).
- Email/iMessage delivery format changes (`deliver.py` untouched beyond passing the new HTML).

## Open questions
- **Font cache vs render-time read:** `wrap_page` reads `fonts-inline.css` from disk each render — fine (single read). If the assets dir is absent (fresh clone before `build_fonts.py`), `wrap_page` must fail loud with a "run `python scripts/build_fonts.py` first" message, not silently drop fonts. Resolve in Sub-phase 3.
- **Masonry vs tier order:** CSS `column-count:3` fills top-to-bottom per column; the design relies on DOM order ≈ rank. Confirm the tier→DOM order in Sub-phase 4 keeps Hero first. Not blocking.

## How to resume in a new session
1. `cat plans/phase-7-tiles-digest-render.md` (this file) and `out/orbit-tiles-reference.html` (the visual target).
2. Run sub-phases in order 1 → 2 → 3 → 4; each is independently testable.
3. Or drive it with the repo workflow: `/run-phase plans/phase-7-tiles-digest-render.md`.
4. Context: memory `orbit-tiles-design-import` has the decisions; `lib/llm.py` auth caveat in `orbit-x-auth-setup` / `orbit-llm-wiring-and-gaps`.
