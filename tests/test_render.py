"""DoD tests for the HTML one-pager renderer (Phase 3 / Sub-phase 3).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Rendering (Stage 7a) is where the ranked/tiered items become the openable digest —
so the tests assert the product invariants the digest depends on (the sub-phase
Definition of Done), each constructed to FAIL on wrong logic, not just "renders
something":

  1. Deep-link survives end-to-end (THE headline feature): a chapterized Hero item's
     chapter renders a working ``<a href="...watch?v=...&t=90s">`` in the OUTPUT
     HTML. A regression that dropped the chapter list, or escaped the href into
     uselessness, or built the wrong timestamp, would lose the one thing the digest
     exists to deliver — a click into the exact moment.
  2. Tier -> visual density: Hero/Standard items render full cards WITH chapter
     lists; Index items render in the bottom "they also posted" section. A
     regression that flattened tiers (everything a row, or chapters everywhere)
     would erase "rank controls density".
  3. XSS safety (non-negotiable): a malicious ``javascript:`` link target is NOT
     emitted as a clickable ``javascript:`` href (scheme allowlist), and a
     ``<script>`` in a title is html-escaped (no raw executable tag). A regression
     here is a stored-XSS hole in a file the user opens in their browser.
  4. TL;DR header present (the "is this worth my time" glance).
  5. Happy path / empty / no-chapters edges: valid HTML always, no crash.

Inputs are constructed TieredItem fixtures (no network / LLM / rerank run) — we
build RankableItem + Classification + Chapter directly. Mirrors the import header
of tests/test_density.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_density.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import html_render, render, trending  # noqa: E402
from lib.chapterize import Chapter  # noqa: E402
from lib.classify import Classification  # noqa: E402
from lib.density import TIER_HERO, TIER_INDEX, TIER_STANDARD, TieredItem  # noqa: E402
from lib.rerank import RankableItem, ScoredItem  # noqa: E402


def _tiered(
    item_external_id: str,
    tier: str,
    *,
    title: str = "A talk",
    channel_name: str = "Some Channel",
    chapters: list[Chapter] | None = None,
    score: float = 5.0,
) -> TieredItem:
    """Build a TieredItem fixture directly (no rerank / density run).

    A passing Classification is attached so the fixture mirrors a real top-line
    item; the tier is set explicitly to control which section it renders in.
    """
    classification = Classification(
        item_external_id=item_external_id,
        axis_a_signal=1,
        axis_b_on_topic=1,
        is_user_override=0,
    )
    item = RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name=channel_name,
        creator_external_id=f"UC_{item_external_id}",
        view_count=12_345,
        like_count=678,
        comment_count=90,
        upload_date="20260101",
        classification=classification,
        chapters=chapters or [],
    )
    return TieredItem(scored_item=ScoredItem(item=item, score=score), density_tier=tier)


def _chapter(title: str, start_seconds: float, video_id: str) -> Chapter:
    """Build a Chapter with a real watch?v=ID&t=Ns deep-link (the headline feature)."""
    return Chapter(
        title=title,
        start_seconds=start_seconds,
        deep_link=f"https://www.youtube.com/watch?v={video_id}&t={int(start_seconds)}s",
    )


def test_chapterized_hero_chapter_content_and_deep_link_survive_to_html() -> None:
    """A Hero tile surfaces its chapter content + the whole-item deep-link (DoD #1).

    WHY: the Tiles layout shows chapter key-points as ``.kp`` chip rows and surfaces the
    whole-video deep-link via the tile title + the "+ N more chapters" overflow link
    (the design does not per-chapter-link every row). This asserts the FULL path — the
    chapter timestamps/text reach the tile AND a clickable, correctly-built whole-item
    deep-link survives (escaped & -> &amp;). A dropped chapter list, a wrong timestamp
    chip, or an over-escaped href would fail here, not silently degrade the feature.
    """
    chapters = [_chapter(f"Ch{n}", float(n * 60), "vidHERO") for n in range(6)]
    chapters[2] = _chapter("The point", 120.0, "vidHERO")
    tiered = [_tiered("vidHERO", TIER_HERO, chapters=chapters)]

    output_html = render.render_digest_html(tiered, inline_image=lambda url: None)

    # Chapter content reaches the tile as chip + text.
    assert "The point" in output_html
    assert '<span class="chip">2:00</span>' in output_html
    # Six chapters, four shown -> the "+ 2 more chapters" overflow link to the whole item.
    assert "+ 2 more chapters" in output_html
    # The whole-item deep-link survives as a real escaped href (title + more-chapters link).
    assert 'href="https://www.youtube.com/watch?v=vidHERO&amp;t=0s"' in output_html


def test_tier_controls_tile_density_every_item_appears() -> None:
    """Tier controls density: a Hero is a chapter-bearing feature tile, an Index a compact tile (DoD #2).

    WHY: "rank controls density, never inclusion." Every item must appear as exactly
    one tile — a Hero as a loud feature tile carrying its ``.kp`` chapter rows, an Index
    item as a thumbnail-less compact tile. A regression that flattened the tiers (chapters
    everywhere, or an item silently dropped) would erase the hierarchy that IS the ranking.
    """
    hero = _tiered("vidHERO", TIER_HERO, chapters=[_chapter("Seg", 30.0, "vidHERO")])
    index = _tiered("vidINDEX", TIER_INDEX, title="Minor clip")
    output_html = render.render_digest_html([hero, index], inline_image=lambda url: None)

    # Exactly two tiles (one per item) — nothing dropped, nothing duplicated.
    assert output_html.count('class="tile"') == 2
    # The Hero carries its chapter ``.kp`` row; the Index item appears as a compact tile.
    assert 'class="kp"' in output_html
    assert "vidHERO" in output_html
    assert "vidINDEX" in output_html
    assert "Minor clip" in output_html
    # The Index item, having no chapters, contributes NO ``.kp`` chapter row of its own —
    # the only kp row is the Hero's (density differs by tier).
    assert output_html.count('class="kp"') == 1


def test_malicious_title_and_url_are_neutralized() -> None:
    """A javascript: link is dropped and a <script> title is escaped (DoD #3).

    WHY: the artifact is opened in a browser. A creator-controlled title or a
    chapter deep-link carrying ``javascript:alert(1)`` or a literal ``<script>`` tag
    is a stored-XSS payload. The scheme allowlist must drop the javascript: href to
    a non-clickable "#", and html.escape must render the <script> as inert text. A
    regression here is an exploitable hole, not a cosmetic bug.
    """
    malicious_chapter = Chapter(
        title="<script>alert('xss')</script>",
        start_seconds=10.0,
        deep_link="javascript:alert(1)",
    )
    tiered = [
        _tiered(
            "vidEVIL",
            TIER_HERO,
            title="<script>alert('title')</script>",
            channel_name="<img src=x onerror=alert(2)>",
            chapters=[malicious_chapter],
        )
    ]
    output_html = render.render_digest_html(tiered, inline_image=lambda url: None)

    # The javascript: scheme is never emitted as a clickable href (the whole-item link
    # is the safe constructed watch?v= URL; the malicious chapter deep-link never reaches
    # an href — chapters render as inert chip+text).
    assert 'href="javascript:alert(1)"' not in output_html
    assert "javascript:alert" not in output_html
    # No raw executable tags survived — titles/channel are escaped to inert text.
    assert "<script>" not in output_html
    assert "<img src=x onerror" not in output_html
    assert "&lt;script&gt;" in output_html


def test_masthead_present_with_pure_counts() -> None:
    """The masthead renders the wordmark + a correct pure-count line (DoD #4).

    WHY: the masthead is the glance that tells the user the day's shape. The counts are
    deterministic (Rule 5, no LLM) — 2 items from 2 distinct creators must read
    "2 sources · 2 accounted for". A regression in counting or a missing masthead fails.
    """
    items = [_tiered("vidA", TIER_HERO), _tiered("vidB", TIER_STANDARD)]
    output_html = render.render_digest_html(items, inline_image=lambda url: None)

    assert ">Orbit</div>" in output_html  # the masthead wordmark
    assert "2 sources · 2 accounted for" in output_html


def test_happy_path_is_valid_html_document() -> None:
    """A populated digest is a well-formed self-contained HTML document (DoD #5 happy).

    WHY: the file must open standalone in a browser. We assert the doctype, the
    closing </html>, an inline <style> (self-contained, no external fetch), and that
    no external resource is linked.
    """
    items = [
        _tiered("vidH", TIER_HERO, chapters=[_chapter("Intro", 0.0, "vidH")]),
        _tiered("vidS", TIER_STANDARD),
        _tiered("vidI", TIER_INDEX),
    ]
    output_html = render.render_digest_html(items, inline_image=lambda url: None)

    assert output_html.startswith("<!DOCTYPE html>")
    assert "</html>" in output_html
    assert "<style>" in output_html
    # Self-contained Tiles page: inlined fonts + tile markup, no external fetch / CDN.
    assert "@font-face" in output_html
    assert 'class="tile"' in output_html
    assert "fonts.googleapis.com" not in output_html
    assert "<link" not in output_html
    assert "<script src" not in output_html


def test_empty_tiered_items_still_valid_page() -> None:
    """An empty digest renders a valid page (no crash) reading "0 episodes" (DoD #5 failure/edge).

    WHY: a quiet day (no items) must not crash the pipeline — it should produce a
    valid, openable page that honestly says nothing happened. A regression that
    indexed [0] or divided by creator count would crash here.
    """
    output_html = render.render_digest_html([])

    assert output_html.startswith("<!DOCTYPE html>")
    assert "</html>" in output_html
    assert ">Orbit</div>" in output_html  # a valid masthead still renders
    assert "0 sources · 0 accounted for" in output_html


def test_hero_without_chapters_renders_card_without_chapter_list() -> None:
    """A Hero item with NO chapters renders a card and NO empty chapter container (DoD #5 edge).

    WHY: not every long-form item gets chapters (short videos, no transcript). The
    card must still render, without an empty ``<ul class="chapters">`` dangling. A
    regression that always emitted the container, or crashed on an empty chapter
    list, would fail.
    """
    tiered = [_tiered("vidNoChap", TIER_HERO, chapters=[])]
    output_html = render.render_digest_html(tiered, inline_image=lambda url: None)

    assert 'class="tile"' in output_html
    assert "vidNoChap" in output_html
    # No chapter ``.kp`` rows emitted for a chapter-less tile (no empty container).
    assert 'class="kp"' not in output_html


from lib.density import TIER_COMPACT  # noqa: E402


def _many(tier: str, count: int, *, prefix: str, chapters_each: int = 0) -> list[TieredItem]:
    """Build ``count`` tiered items in ``tier`` with distinct ids (for budget tests)."""
    items: list[TieredItem] = []
    for index in range(count):
        item_id = f"{prefix}{index}"
        chapters = [_chapter(f"Ch{n}", float(n * 60), item_id) for n in range(chapters_each)]
        items.append(_tiered(item_id, tier, channel_name=f"Ch{prefix}{index}", chapters=chapters))
    return items


def test_small_digest_is_single_page_with_no_spill_link() -> None:
    """A digest under the budget renders ONE page with no page-2 link (DoD #1, spill).

    WHY: the one-pager is the product. A quiet day must NOT gain a "page 2" link or a
    phantom second page — that would be a regression that splits content the user
    could see at a glance. estimate_page_height must stay within PAGE_1_BUDGET_PX and
    render_digest_pages must return exactly one string with no continued-link.
    """
    tiered = [
        _tiered("vidH", TIER_HERO, chapters=[_chapter("Intro", 0.0, "vidH")]),
        _tiered("vidS", TIER_STANDARD),
        _tiered("vidI", TIER_INDEX),
    ]
    assert render.estimate_page_height(tiered) <= render.PAGE_1_BUDGET_PX

    pages = render.render_digest_pages(tiered, inline_image=lambda url: None)
    assert len(pages) == 1
    assert "Full archive · page 2" not in pages[0]
    assert render.DEFAULT_PAGE_2_FILENAME not in pages[0]


def test_oversized_digest_spills_low_tiers_to_page_two() -> None:
    """An over-budget digest spills Compact+Index to page 2; Hero/Standard stay (DoD #2, spill).

    WHY: this is "spill-the-low-tiers", NOT arbitrary splitting. The whole point of
    the tier ladder is that the high-value Hero/Standard cards stay on the screen the
    user opens first. A regression that pushed a Hero to page 2 (or split mid-list)
    would defeat the ranking. We assert the hero id is on page 1 and ABSENT from page
    2, and a compact + an index id are on page 2 and absent from page 1's card body.
    """
    # Enough Compact + Index rows to blow the budget while Hero/Standard alone fit.
    tiered = (
        [_tiered("HEROID", TIER_HERO, chapters=[_chapter("Seg", 90.0, "HEROID")])]
        + [_tiered("STDID", TIER_STANDARD)]
        + _many(TIER_COMPACT, 20, prefix="CMP")
        + _many(TIER_INDEX, 20, prefix="IDX")
    )
    assert render.estimate_page_height(tiered) > render.PAGE_1_BUDGET_PX

    pages = render.render_digest_pages(tiered, inline_image=lambda url: None)
    assert len(pages) == 2
    page1, page2 = pages

    # Compare against the BODY region (after the inlined base64 font ``<style>``) so a
    # short fixture id can't false-collide with the base64 font blob (which contains
    # arbitrary alnum runs). The ids live in tile titles/links in the body, not the CSS.
    page1_body = page1.split("</style>", 1)[1]
    page2_body = page2.split("</style>", 1)[1]

    # Page 1 carries the footer page-2 link; Hero + Standard stayed on page 1.
    assert "Full archive · page 2" in page1
    assert "HEROID" in page1_body
    assert "watch?v=HEROID&amp;t=0s" in page1_body  # the hero's whole-item deep-link survives on page 1
    assert "STDID" in page1_body

    # The Hero must NOT have leaked onto page 2 (spill-the-low-tiers, not arbitrary).
    assert "HEROID" not in page2_body

    # Compact + Index moved to page 2 and are NOT in page 1's body.
    assert "CMP0" in page2_body and "CMP0" not in page1_body
    assert "IDX0" in page2_body and "IDX0" not in page1_body


def test_two_page_hard_cap_holds_even_when_page_two_overflows() -> None:
    """Even an enormous digest is capped at exactly 2 pages — never page 3 (DoD #3, spill).

    WHY: the design hard-caps at 2 pages. Everything past page 1's Hero/Standard band
    goes to page 2, even if page 2 ITSELF would overflow the budget. A regression that
    recursively paginated (page 3, 4, ...) would break the "one pager + overflow" model.
    We build a Compact/Index set whose page-2 estimate alone exceeds the budget and
    assert exactly two pages still come back.
    """
    tiered = (
        [_tiered("HEROID", TIER_HERO)]
        + _many(TIER_COMPACT, 80, prefix="CMP")
        + _many(TIER_INDEX, 80, prefix="IDX")
    )
    # Page 2's own content (compact+index) alone exceeds the budget — yet still 2 pages.
    page2_only = _many(TIER_COMPACT, 80, prefix="CMP") + _many(TIER_INDEX, 80, prefix="IDX")
    assert render.estimate_page_height(page2_only) > render.PAGE_1_BUDGET_PX

    pages = render.render_digest_pages(tiered, inline_image=lambda url: None)
    assert len(pages) == 2  # hard cap: never 3


def test_safe_href_allowlist_unit() -> None:
    """Unit-level guard on the link allowlist primitive (defense in depth for DoD #3).

    WHY: the renderer's XSS safety rests entirely on html_render.safe_href. This
    pins its contract directly: real YouTube deep-links pass (escaped), and unsafe
    schemes collapse to "#". If this primitive regresses, every card/chapter link
    becomes a potential payload.
    """
    safe = html_render.safe_href("https://www.youtube.com/watch?v=abc&t=90s")
    assert safe == "https://www.youtube.com/watch?v=abc&amp;t=90s"
    assert html_render.safe_href("javascript:alert(1)") == "#"
    assert html_render.safe_href("data:text/html,<script>") == "#"


# === Phase 7 / Sub-phase 3: Tiles markup + CSS builders ======================
#
# These pin the Tiles-layout builders (lib.tiles, re-exported via lib.html_render).
# Per Rule 9 each test encodes WHY the behavior matters:
#   * Escaping a <script> title is the stored-XSS guard for a file the user opens.
#   * safe_img_src on every <img> is the data:-URI XSS guard at the image sink.
#   * The .ph fallback on an empty image_url is the "never a broken <img>" invariant.
#   * The trending markers encode the WHERE-the-signal-comes-from semantics (◆/↗/○).
#   * Empty verdict/blurb omitting their element is the no-fabrication degrade path.
#   * The assembled page carrying @font-face + class="tile" and NOT googleapis is the
#     self-contained-offline invariant the whole "base64-inlined" decision rests on.

from lib import tiles  # noqa: E402

_XSS_TITLE = "<script>alert('pwn')</script>"
_INERT_TITLE = "&lt;script&gt;alert(&#x27;pwn&#x27;)&lt;/script&gt;"


def test_tiles_every_builder_escapes_script_title_to_inert_text() -> None:
    """Every Tiles builder html-escapes a ``<script>`` title to inert text (XSS guard).

    WHY: the digest is opened in a browser straight off disk. A creator title /
    tweet text containing ``<script>`` MUST render as visible text, never an
    executable tag — a regression in any single builder is a stored-XSS hole.
    """
    chapters = [tiles.ChapterRow(chip="04:20", text=_XSS_TITLE)]
    cross = [tiles.CrossLink(label=_XSS_TITLE, url="https://x.com")]
    builders_output = [
        tiles.render_masthead(_XSS_TITLE, 1, 1, 1, 1, 1),
        tiles.render_verdict(_XSS_TITLE),
        tiles.render_scoop_tile(_XSS_TITLE, _XSS_TITLE, _XSS_TITLE, "https://x.com"),
        tiles.render_trending_now([tiles.TrendingRow(title=_XSS_TITLE, category=tiles.CATEGORY_YOURS, your_count=3)]),
        tiles.render_hidden_gem(_XSS_TITLE, 900, _XSS_TITLE, _XSS_TITLE, chip_time="02:50", chip_label=_XSS_TITLE),
        tiles.render_hero_tile(
            meta_label=_XSS_TITLE, title=_XSS_TITLE, summary=_XSS_TITLE, chapters=chapters, cross_links=cross
        ),
        tiles.render_standard_tile(meta_label=_XSS_TITLE, title=_XSS_TITLE, summary=_XSS_TITLE, chapters=chapters),
        tiles.render_compact_tile(meta_label=_XSS_TITLE, title=_XSS_TITLE, summary=_XSS_TITLE, chip_time="1:00",
                                  chip_label=_XSS_TITLE),
        tiles.render_tweet_tile(source_label=_XSS_TITLE, text=_XSS_TITLE),
        tiles.render_footer(_XSS_TITLE, "page2.html"),
    ]
    for output_html in builders_output:
        assert "<script>" not in output_html, output_html[:120]
        assert _INERT_TITLE in output_html, output_html[:200]


def test_tiles_feature_tile_uses_safe_img_src_for_thumbnail() -> None:
    """A feature tile routes its thumbnail through ``safe_img_src`` (image-sink guard).

    WHY: thumbnails are base64 ``data:`` URIs — the ONE place ``data:`` is allowed.
    A ``data:text/html`` payload must never reach an ``<img src>``. A safe base64
    image URI must survive; the html-injection payload must be dropped to the ``.ph``
    fallback (no ``<img>`` at all).
    """
    safe_data_uri = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    good = tiles.render_hero_tile(meta_label="M", title="T", image_url=safe_data_uri)
    assert f'<img src="{safe_data_uri}"' in good

    evil = tiles.render_hero_tile(
        meta_label="M", title="T", image_url="data:text/html,<script>alert(1)</script>",
        placeholder_label="thumb",
    )
    assert "<img" not in evil  # payload rejected, no <img> emitted
    assert 'class="ph"' in evil  # fell back to the hatched placeholder


def test_tiles_empty_image_url_falls_back_to_ph_placeholder() -> None:
    """An empty ``image_url`` falls back to the ``.ph`` placeholder, never a broken <img>.

    WHY: most items will not have an inlinable thumbnail on a given run. The tile
    must still render with the hatched placeholder + caption — emitting ``<img src="">``
    would show a broken-image icon in the user's inbox.
    """
    output_html = tiles.render_standard_tile(meta_label="M", title="T", placeholder_label="thumb · lenny")
    assert "<img" not in output_html
    assert 'class="ph"' in output_html
    assert "thumb · lenny" in output_html


def test_tiles_trending_now_renders_correct_marker_per_category() -> None:
    """Each trending category renders its distinct marker + right-label (signal semantics).

    WHY: the marker is the WHOLE point of the trending tile — ◆ a dormant creator
    breaking silence, ↗ "N of yours" (consensus inside your network), ○ external
    (trending outside it). Collapsing them would erase the "ahead of the curve"
    meaning. We assert all three coexist with the right glyph and label.
    """
    output_html = tiles.render_trending_now(
        [
            tiles.TrendingRow(title="karpathy breaks silence", category=tiles.CATEGORY_DORMANT),
            tiles.TrendingRow(title="Nano Banana 2 pricing", category=tiles.CATEGORY_YOURS, your_count=4),
            tiles.TrendingRow(title="EU AI Act date", category=tiles.CATEGORY_EXTERNAL),
        ]
    )
    assert "◆" in output_html and "dormant" in output_html
    assert "↗" in output_html and "4 of yours" in output_html
    assert "○" in output_html and "external" in output_html


def test_tiles_empty_verdict_and_blurb_omit_their_element() -> None:
    """An empty verdict / blurb omits its element entirely (no-fabrication degrade).

    WHY: when the LLM is unavailable the prose must be ABSENT, not an empty styled
    container hinting content was lost. A regression that always emitted the wrapper
    would leave a dangling empty box on a quiet/LLM-down day.
    """
    assert tiles.render_verdict("") == ""
    assert tiles.render_verdict("   ") == ""

    # A standard tile with no summary must not carry an empty blurb div.
    no_blurb = tiles.render_standard_tile(meta_label="M", title="T", summary="")
    assert "line-height:1.45" not in no_blurb  # the blurb div's signature style is absent
    with_blurb = tiles.render_standard_tile(meta_label="M", title="T", summary="why it matters")
    assert "why it matters" in with_blurb


def test_tiles_verdict_italicizes_handles() -> None:
    """The verdict italicizes ``@handle`` mentions (the design's masthead accent).

    WHY: the masthead verdict styles mentions in italic; this is the deterministic,
    code-driven (Rule 5) accent. The transform must run on the escaped string and not
    introduce active markup.
    """
    output_html = tiles.render_verdict("a scoop from @swyx and @karpathy today")
    assert output_html.count("font-style:italic") == 2
    assert "<script>" not in output_html


def test_tiles_assembled_page_is_self_contained_with_fonts_and_no_cdn() -> None:
    """The assembled page inlines ``@font-face`` + carries ``class="tile"`` + no CDN.

    WHY (the load-bearing structural invariant): the entire "base64-inline everything"
    decision exists so the digest opens identically offline. The page MUST carry the
    inlined ``@font-face`` rules and the Tiles markup, and MUST NOT reference
    ``fonts.googleapis.com`` (or any CDN) — a regression that dropped the inline fonts
    or re-added a Google Fonts ``<link>`` breaks offline rendering.
    """
    body = (
        tiles.render_masthead("MON · 1 JAN 2026 · 06:14", 26, 26, 1, 1, 2)
        + tiles.render_verdict("the only real story is evals")
        + tiles.render_feed_masonry(tiles.render_hero_tile(meta_label="DWARKESH · 1:52 · YouTube", title="Hi"))
        + tiles.render_footer("26 OF 26 SOURCES ACCOUNTED FOR", "")
    )
    page = html_render.wrap_page("Orbit · Today", body)

    assert page.startswith("<!DOCTYPE html>")
    assert "@font-face" in page
    assert 'class="tile"' in page
    assert "fonts.googleapis.com" not in page
    assert "<link" not in page
    assert "<script src" not in page


def test_tiles_footer_omits_page2_link_when_no_href() -> None:
    """The footer omits the page-2 link on a single-page digest (no phantom spill link).

    WHY: a quiet day fits one page; surfacing a "page 2 →" link to nowhere would be a
    broken affordance. With a non-empty href the link must appear (the spill path).
    """
    assert "page 2" not in tiles.render_footer("ALL ACCOUNTED FOR", "")
    assert "page 2" in tiles.render_footer("ALL ACCOUNTED FOR", "page2.html")


def _trending_item(item_external_id: str, title: str) -> "trending.TrendingItem":
    """Build a minimal external-category TrendingItem for the trending-cap test."""
    return trending.TrendingItem(
        item_external_id=item_external_id,
        cluster_id=f"c_{item_external_id}",
        creator_external_id=f"UC_{item_external_id}",
        title=title,
        card_url="",
        velocity_score=1.0,
        convergence_count=0,
        baseline_relative_ratio=1.0,
    )


def test_trending_now_renders_at_most_five_rows() -> None:
    """The "Trending now" tile renders at most 5 rows even when more are ranked.

    WHY (Rule 9): the velocity ranker can surface many trending clusters, but the digest
    is a glance surface — an unbounded list buries the top signal and blows the page budget.
    We pass 7 velocity-ordered items and assert exactly the top 5 titles render in the
    trending tile (and the 6th/7th do NOT). Removing the cap re-renders all 7 and fails this.
    """
    trending_items = [_trending_item(f"tr{n}", f"Trending headline {n}") for n in range(7)]

    trio_html = render._build_ahead_trio(
        scoops=[],
        trending_items=trending_items,
        items_by_id={},
        summaries={},
    )

    # Each trending row carries this distinctive style fragment (the gem tile uses a
    # different ``gap:7px`` ordering), so counting it counts trending rows exactly.
    row_count = trio_html.count("align-items:baseline;gap:10px")
    assert row_count == 5, f"trending tile must cap at 5 rows, rendered {row_count}"
    # The top-5 headlines are present; the tail (6th/7th) are dropped.
    for n in range(5):
        assert f"Trending headline {n}" in trio_html
    assert "Trending headline 5" not in trio_html
    assert "Trending headline 6" not in trio_html
