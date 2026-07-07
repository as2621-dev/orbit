# Phase 8: Spec alignment (timestamps, gating, X virality, cron install)

**Milestone:** M5+ — the digest matches the 2026-07-06 product rulings
**Status:** Not started
**Estimated effort:** M

## Goal
Close the gap between the shipped digest and the product decisions locked on 2026-07-06:
chapter chips become **clickable `watch?v=ID&t=Ns` deep-links** and blurbs are **grounded
in chapter content** (not just titles); YouTube goes **long-form only (≥600s)** with a
fixed **category taxonomy** third classify axis; the X half switches from
density-not-inclusion to **top-N virality selection** (quote-tweet down-weight,
quote_count wired into the blend, absolute-engagement term, hard cap ~8); and setup
**installs the crontab entry itself** at a fixed 7am default.

## Decisions locked with the user (2026-07-06) — do NOT re-litigate
- **Top-N inclusion beats density for X.** Key Decision 6 ("rank controls density, never
  inclusion") is **superseded for the X half only** — pick one, don't blend (Rule 7).
  YouTube keeps the density ladder. Sub-phase 4 records this in master-plan.md.
- **iMessage-only delivery.** Email is explicitly out of scope — document it, don't build it.
- **Local auto-cron at 7am.** The wizard installs `0 7 * * *` itself; schedule stops being
  a config knob.
- **10-minute long-form floor.** YouTube uploads under 600s are dropped in Stage 1.
- **P0s:** clickable timestamps, transcript-grounded blurbs, quote-tweet down-weight.

## Current code map (verify before editing — Rule 8)
- `scripts/lib/chapterize.py` — `Chapter` dataclass **already has** `start_seconds` +
  `deep_link` (`watch?v=ID&t=Ns`, always from a real cue/creator offset). The URL exists;
  it's dropped downstream.
- `scripts/lib/render.py:237` `_chapter_rows` — builds `tiles.ChapterRow(chip, text)` from
  each chapter; discards `chapter.deep_link`. `render.py:156-169` + `333-350` — card-url
  fallbacks hardcode `watch?v=ID&t=0s`.
- `scripts/lib/tiles.py:88` `ChapterRow(NamedTuple)` — `(chip, text)` only.
  `_render_kp_rows` (line 157) renders plain `<span>`s. `_render_more_chapters` (176)
  already links `card_url` via `safe_href`. File is 805 lines (limit 1000).
- `scripts/lib/summarize.py:183-195` — the blurb prompt's items block is
  `id\tchannel\ttitle` only. Prompt templates in `references/summarize.md`
  (`{items_block}` placeholder).
- `scripts/orbit.py` — **1098 lines, already over the 1000-line CLAUDE.md limit.**
  `run_stage1_build_youtube_items` (~483), `_select_recent_uploads` (429),
  `run_stage1_build_x_items` (334) with the X alpha gate at 408-424
  (`dropped_noise_count`). `LONG_FORM_THRESHOLD_SECONDS` (1200s) gates chapterize
  eligibility — distinct from the new 600s inclusion floor.
- `scripts/lib/classify.py` — two binary axes (`axis_a_signal`, `axis_b_on_topic`),
  prompt in `references/classify.md` (61 lines), `Classification` dataclass +
  `is_also_posted`. Design decision 5 ("never dropped") already has an X alpha-gate
  exception from commit `55bd00c`.
- `scripts/lib/bird_x.py` — `Tweet` has `quote_count` (parsed at 607) but **no quote-tweet
  flag**; `_parse_tweets` (554) reads only id/author/text/created_at/counts from the CLI
  JSON. Retweets are query-filtered + `RT @`-prefix dropped (55bd00c).
- `scripts/lib/rerank.py` — `from_tweet` (220) maps retweet→`view_count`,
  like→`like_count`, reply→`comment_count` and **silently drops `quote_count`**.
  `engagement_blend` (330) = weighted `log1p_safe` of the three counts. Score =
  priority + recency + uniqueness + (blend − creator's own median baseline). No
  absolute/percentile term. Weight constants at module top (~62).
- `scripts/lib/setup_wizard.py` — `generate_cron_entry` (64, pure), `_gather_schedule`
  (345, prompts for a cron string), `run_setup_wizard` step 5 **prints** the entry for
  manual `crontab -e` pasting. `lib/config.py` has `DEFAULT_SCHEDULE` +
  `is_valid_cron_expression`.
- `plans/master-plan.md:70` — Key Decision 6.

## Sub-phases

### Sub-phase 1: Clickable timestamps + grounded blurbs (the headline fix)
- **Files touched:** `scripts/lib/tiles.py`, `scripts/lib/render.py`,
  `scripts/lib/summarize.py`, `references/summarize.md`, `tests/test_render.py`,
  `tests/test_summarize.py`
- **What ships:**
  - `tiles.ChapterRow` gains `url: str = ""`. `_render_kp_rows`: when `url` is non-empty
    and passes `safe_href`, the chip renders as
    `<a class="chip" href="{safe_href(url)}">…</a>` (keep the visual class); empty/unsafe
    url keeps today's inert `<span>` (degrade, don't break — Rule 12).
  - `render.py::_chapter_rows`: pass `chapter.deep_link` through as `ChapterRow.url`.
  - Card/title deep-links: the two `watch?v=ID&t=0s` fallbacks (`render.py:156-169`,
    `333-350`) become "first chapter's `deep_link` when the item has chapters, else
    `&t=0s`" — clicking a card lands where the content starts.
  - `summarize.py`: the items block gains a 4th tab column — the item's chapter titles
    joined `" · "` (first ~6, truncated to keep Rule 6 token discipline); empty for
    chapterless items. `references/summarize.md` prompt updated to instruct grounding the
    blurb in the chapter outline, never inventing content, and to keep the existing
    ≤140-char/no-restating-title rules. **Transcripts are NOT retained after chapterize**,
    so chapter titles are the grounding source — do not thread transcripts through
    (see Open questions).
- **Definition of done:** tests: a chapter with a `deep_link` renders an `<a>` chip whose
  href is the escaped `watch?v=ID&t=Ns` URL; a chapter row with empty url renders the old
  `<span>` (no `<a href="">`); a `javascript:` url is neutralized to the span form; card
  url for a chaptered item uses the first chapter offset, chapterless keeps `&t=0s`;
  `summarize_items` prompt (captured via mock `llm_call`) contains the chapter-title
  column and truncates long outlines. `pytest tests/test_render.py tests/test_summarize.py
  tests/test_scoops_and_render.py` green.
- **Dependencies:** none

### Sub-phase 2: Long-form floor + category taxonomy gating
- **Files touched:** `scripts/orbit.py`, `scripts/lib/classify.py`,
  `references/classify.md`, `tests/test_classify.py`, `tests/test_classify_x.py`,
  `tests/test_orbit_youtube_producer.py`
- **What ships:**
  - **600s floor:** new module-top constant `MIN_UPLOAD_DURATION_SECONDS = 600` in
    `orbit.py`; `run_stage1_build_youtube_items` drops uploads with
    `duration is not None and duration < 600` BEFORE the classify call (saves the LLM
    budget too). `duration is None` → keep (fail-open — commit `654f0fb` taught us missing
    metadata must not nuke items) and log. Log `youtube_stage1_short_form_dropped` with a
    per-run count. **`orbit.py` is over the 1000-line limit: implement the filter as a
    small pure helper in `lib/` (e.g. alongside `_select_recent_uploads` logic) or extract
    the stage-1 YouTube helpers to `lib/stage1_youtube.py` if the diff pushes the file
    further over — do not grow orbit.py.**
  - **Category axis:** `Classification` gains `category: str = "other"` — third axis with
    the FIXED taxonomy `{ai, business, tech, sports, other}` defined in
    `references/classify.md` (one new prompt section/lines + one-token-ish answer format
    extension; keep the two binary axes unchanged). Parser tolerates a missing/garbled
    category → `"other"`? NO — fail-open to `"ai-adjacent unknown"` is fabrication;
    a missing category defaults to **keep** (treat as pass) and logs, so a prompt
    regression never silently empties the digest (Rule 12).
  - **Gating:** Stage 1 (both YouTube and X — shared classify path) drops items whose
    parsed `category == "other"` outright, logged with counts
    (`youtube_stage1_category_dropped` / `x_stage1_category_dropped`), extending the
    existing X alpha-gate pattern at `orbit.py:408`. `interests` remain a soft Axis-B
    sharpener (derank via `is_also_posted`), NOT a drop condition — unchanged.
- **Definition of done:** tests: a 400s upload never reaches the classifier; a
  `duration=None` upload survives to classify; `category: other` → dropped and counted;
  each of ai/business/tech/sports → kept; unparseable category → kept + logged; the X
  path gates identically; the classify prompt renders the taxonomy from
  `references/classify.md`. Full `pytest tests/` green.
- **Dependencies:** none (parallel-safe with Sub-phase 1)

### Sub-phase 3: X virality selection (quote handling, absolute term, top-8 cap)
- **Files touched:** `scripts/lib/bird_x.py`, `scripts/lib/rerank.py`,
  `scripts/orbit.py` (X stage-1 tail), `tests/test_bird_x_delta.py`,
  `tests/test_rerank.py`
- **What ships:**
  - **Quote detection:** `Tweet` gains `is_quote: bool = False`. `_parse_tweets` sets it
    from the CLI payload — probe the vendored CLI's tweet JSON for the actual field
    (candidates: `quotedTweet` / `quoted_status` / `quoted_status_id` / `isQuote`;
    use `_first_present`-style tolerant reads and default False). **First implementation
    step: inspect the vendored bird CLI source/fixtures to confirm the field name(s) —
    Rule 8; if the payload genuinely never carries quote info, say so loudly in the
    report rather than shipping dead code (Rule 12).**
  - **Blend:** `RankableItem` gains `quote_count: Optional[int] = None` +
    `is_quote: bool = False`; `from_tweet` maps both (YouTube's `from_parts` leaves
    defaults). `engagement_blend` adds a `ENGAGEMENT_QUOTE_WEIGHT` term (small — quotes
    signal discourse, weight below likes). New `QUOTE_TWEET_MULTIPLIER` (e.g. 0.5) applied
    to the final score when `is_quote` — a quote of someone else's take ranks below the
    creator's own takes.
  - **Absolute-engagement term:** new pure `compute_batch_engagement_percentile(items) ->
    dict[item_external_id, float]` (percentile of `engagement_blend` across the X items in
    the batch, 0.0–1.0) and a weighted `ABSOLUTE_ENGAGEMENT_WEIGHT` term added to the
    score alongside the creator-baseline-relative term — a banger from a quiet account
    now outranks a median post from a loud one.
  - **Hard cap:** after scoring, only the top `X_DIGEST_TWEET_CAP = 8` X items survive
    into the digest; the rest are dropped and counted in the stage log
    (`x_stage1_cap_dropped` or equivalent at the rank seam — put the cap where scored
    X items are already separable, likely the stage-3 rank seam in `orbit.py`, NOT
    inside `assign_density_tiers` whose nothing-dropped invariant stays true for what it
    receives). YouTube items are untouched by the cap.
- **Definition of done:** tests: a payload entry with a quoted-tweet marker yields
  `is_quote=True`, plain tweets False; `from_tweet` carries `quote_count`/`is_quote`;
  blend includes quotes (item with quotes > identical item without);
  `QUOTE_TWEET_MULTIPLIER` ranks a quote below an otherwise-identical original;
  percentile map is 1.0 for the top item / monotone / handles a single-item batch;
  9 scored tweets → exactly 8 survive, drop logged; a batch of ≤8 is untouched;
  YouTube count unaffected. Full `pytest tests/` green.
- **Dependencies:** none (parallel-safe; touches different rerank regions than SP1/SP2 —
  if run in the same worktree as SP2, orbit.py stage-1-X tail overlaps: coordinate or
  serialize those two edits)

### Sub-phase 4: Cron auto-install + scope trims + decision record
- **Files touched:** `scripts/lib/setup_wizard.py`, `scripts/lib/config.py` (only if a
  constant moves), `plans/master-plan.md`, `README.md`, `tests/test_setup_wizard.py`
- **What ships:**
  - **Auto-install:** new `install_cron_entry(cron_entry, *, crontab_runner) -> bool` in
    `setup_wizard.py` — reads the current crontab (`crontab -l`, tolerating the
    "no crontab for user" empty case), **replaces** any existing line containing the
    Orbit marker (tag the line with a trailing `# orbit-daily-digest` comment so re-runs
    are idempotent, never duplicated), appends the new entry, and pipes the result to
    `crontab -`. `crontab_runner` is the injectable subprocess boundary (tests never
    touch the real crontab). On failure: fail soft — log
    `setup_cron_install_failed` with `fix_suggestion` and fall back to today's
    print-and-paste output (a sandboxed/CI run must still complete setup).
  - **Fixed schedule:** delete the `_gather_schedule` prompt; `run_setup_wizard` uses
    `DEFAULT_SCHEDULE` (`0 7 * * *`) directly. Keep writing `schedule` into
    `orbit.config.json` (the contract shape survives; it's just no longer asked).
    `generate_cron_entry` and its validation stay as-is.
  - **Email out of scope:** README delivery section states iMessage is the delivery
    channel and email is explicitly out of scope (decision 2026-07-06) — documentation
    only, no code.
  - **Decision record:** `plans/master-plan.md` Key Decision 6 gets a dated supersession
    note: X half now selects top-N by virality (Phase 8); YouTube keeps
    density-not-inclusion. Superseded, not blended (Rule 7).
- **Definition of done:** tests (with a scripted `crontab_runner`): fresh install writes
  the marker-tagged line; a second run replaces rather than duplicates; "no crontab for
  user" is treated as empty; a failing `crontab` binary degrades to the printed entry and
  logs the failure; the wizard no longer prompts for a schedule and the written config
  carries `0 7 * * *`. `pytest tests/test_setup_wizard.py tests/test_readme_packaging.py`
  green. README + master-plan diffs reviewed.
- **Dependencies:** none (fully parallel-safe)

## Phase-level definition of done
Full `pytest tests/` green. A stubbed pipeline run renders `out/today.html` where: every
chapter chip is an `<a>` to a `watch?v=ID&t=Ns` URL; a chaptered card's title link lands
on the first chapter offset; no YouTube item under 600s and no `other`-category item
appears; at most 8 tweets appear; a quote tweet ranks below a comparable original. A
scripted wizard run installs (via the injected runner) exactly one marker-tagged
`0 7 * * *` crontab line. Master-plan Key Decision 6 carries the supersession note.
README updated (cron auto-install, long-form floor, category taxonomy, email
out-of-scope). Ruff passes. `orbit.py` did not grow past its current line count.

## Out of scope
- Email delivery (explicit ruling — README note only).
- Retaining transcripts past chapterize for blurb grounding (chapter titles are the
  grounding source this phase; see Open questions).
- Interest-taxonomy learning / per-user category customization — the taxonomy is fixed.
- Historical engagement store for absolute scoring (batch percentile only).
- Uninstall/`--remove-cron` tooling.

## Open questions
- **Quote-tweet field name:** unknown until the vendored CLI payload is inspected
  (Sub-phase 3 step 1). If absent from the payload entirely, ship `quote_count` wiring +
  the absolute term + the cap, and report the quote-flag gap loudly instead of faking it.
- **Blurb grounding depth:** if chapter titles prove too thin for good blurbs, a later
  phase can retain a per-item transcript excerpt (~500 chars) through Stage 1 — deliberate
  deferral, not an oversight.
- **Cap placement:** the top-8 cap must sit where X items are scored but before tiering;
  the stage-3 rank seam in `orbit.py` looks right — confirm while implementing that the
  density invariant (`len(out) == len(items)`) is preserved for what tiering receives.

## How to resume in a new session
1. `cat plans/phase-8-spec-alignment.md` (this file); memory `orbit-product-pivot-decisions`
   has the 2026-07-06 rulings.
2. Sub-phases 1/2/3/4 are near-independent; 2 and 3 both touch `orbit.py` stage-1 —
   serialize those two or coordinate the merge.
3. Drive it with the repo workflow: `/run-phase plans/phase-8-spec-alignment.md`.
4. Auth caveats for live runs: `orbit-x-auth-setup`, `orbit-llm-wiring-and-gaps`.
