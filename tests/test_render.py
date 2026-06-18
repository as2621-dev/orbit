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

# Make ``skills/orbit/scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_density.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import html_render, render  # noqa: E402
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


def test_chapterized_hero_chapter_deep_link_survives_to_html() -> None:
    """A Hero chapter's watch?v=...&t=90s deep-link reaches the HTML (DoD #1).

    WHY: the deep-link into the exact moment is the product's headline feature. This
    asserts the FULL path — Chapter.deep_link -> render -> escaped href — preserves a
    clickable, correctly-timestamped link. A wrong timestamp, a dropped chapter list,
    or an over-escaped href would fail here, not silently degrade the one feature
    that justifies the digest.
    """
    chapters = [
        _chapter("Intro", 0.0, "vidHERO"),
        _chapter("The point", 90.0, "vidHERO"),
    ]
    tiered = [_tiered("vidHERO", TIER_HERO, chapters=chapters)]

    output_html = render.render_digest_html(tiered)

    # &t=90s present (correct timestamp), as a real href (escaped & -> &amp;).
    assert 'href="https://www.youtube.com/watch?v=vidHERO&amp;t=90s"' in output_html
    assert "The point" in output_html


def test_hero_gets_chapters_index_goes_to_also_posted_section() -> None:
    """Tier controls density: Hero renders a card+chapters; Index renders in the strip (DoD #2).

    WHY: "rank controls density, never inclusion." A regression that flattened the
    tiers (no chapter lists, or no separate index section) would erase the visual
    hierarchy that IS the ranking. We assert structural tier markers AND that the
    index item lands specifically in the "they also posted" strip, not in a card.
    """
    hero = _tiered("vidHERO", TIER_HERO, chapters=[_chapter("Seg", 30.0, "vidHERO")])
    index = _tiered("vidINDEX", TIER_INDEX, title="Minor clip")
    output_html = render.render_digest_html([hero, index])

    # Hero rendered as a hero card carrying its chapter list.
    assert 'class="card hero"' in output_html
    assert 'class="chapters"' in output_html

    # The index item lands in the "they also posted" strip, not a card. Slice the
    # index section and assert the id appears there (and not as a hero card).
    assert 'class="index-strip"' in output_html
    index_section = output_html.split('class="index-strip"', 1)[1]
    assert "vidINDEX" in index_section
    # The hero's chapter link is in a CARD, before the index strip — not in the strip.
    cards_section = output_html.split('class="index-strip"', 1)[0]
    assert "vidHERO&amp;t=30s" in cards_section


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
    output_html = render.render_digest_html(tiered)

    # The javascript: scheme is never emitted as a clickable href.
    assert 'href="javascript:alert(1)"' not in output_html
    assert "javascript:alert" not in output_html
    # The unsafe chapter href fell back to the non-clickable placeholder.
    assert 'class="chapter-link" href="#"' in output_html
    # No raw executable tags survived — titles/channel are escaped to inert text.
    assert "<script>" not in output_html
    assert "<img src=x onerror" not in output_html
    assert "&lt;script&gt;" in output_html


def test_tldr_header_present() -> None:
    """The one-line TL;DR header renders with a correct pure-count summary (DoD #4).

    WHY: the TL;DR is the glance that tells the user whether to read on. The count is
    deterministic (Rule 5, no LLM) — 2 items from 2 distinct creators must read
    "2 episodes from 2 creators". A regression in counting or a missing header fails.
    """
    items = [_tiered("vidA", TIER_HERO), _tiered("vidB", TIER_STANDARD)]
    output_html = render.render_digest_html(items)

    assert 'class="tldr"' in output_html
    assert "2 episodes from 2 creators today" in output_html


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
    output_html = render.render_digest_html(items)

    assert output_html.startswith("<!DOCTYPE html>")
    assert "</html>" in output_html
    assert "<style>" in output_html
    # Self-contained: no external stylesheet or script fetches.
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
    assert "0 episodes from 0 creators today" in output_html


def test_hero_without_chapters_renders_card_without_chapter_list() -> None:
    """A Hero item with NO chapters renders a card and NO empty chapter container (DoD #5 edge).

    WHY: not every long-form item gets chapters (short videos, no transcript). The
    card must still render, without an empty ``<ul class="chapters">`` dangling. A
    regression that always emitted the container, or crashed on an empty chapter
    list, would fail.
    """
    tiered = [_tiered("vidNoChap", TIER_HERO, chapters=[])]
    output_html = render.render_digest_html(tiered)

    assert 'class="card hero"' in output_html
    assert "vidNoChap" in output_html
    # No chapter list container emitted for a chapter-less card.
    assert 'class="chapters"' not in output_html


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

    pages = render.render_digest_pages(tiered)
    assert len(pages) == 1
    assert "Continued on page 2" not in pages[0]
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

    pages = render.render_digest_pages(tiered)
    assert len(pages) == 2
    page1, page2 = pages

    # Page 1 carries the continued link; Hero + Standard stayed on page 1.
    assert "Continued on page 2" in page1
    assert "HEROID" in page1
    assert "HEROID&amp;t=90s" in page1  # the hero chapter deep-link survives on page 1
    assert "STDID" in page1

    # The Hero must NOT have leaked onto page 2 (spill-the-low-tiers, not arbitrary).
    assert "HEROID" not in page2

    # Compact + Index moved to page 2 and are NOT in page 1.
    assert "CMP0" in page2 and "CMP0" not in page1
    assert "IDX0" in page2 and "IDX0" not in page1


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

    pages = render.render_digest_pages(tiered)
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
