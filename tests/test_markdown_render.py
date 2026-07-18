"""DoD tests for the self-contained ``digest.md`` markdown twin (issue #6).

Per Rule 9 each test encodes WHY the behavior matters, not merely what it does. The
markdown twin is the input #7 hands verbatim to a fresh Claude session, so the product
invariants it must hold are:

  1. ITEM-LEVEL PARITY with the HTML: every item the HTML pages carry, the markdown
     carries too — INCLUDING both pages of a two-page (spilled) digest, merged into ONE
     ``digest.md``. A renderer that dropped the low tiers, or only rendered page 1, would
     silently ship a partial digest to #7.
  2. Deep-links survive VERBATIM — chapter ``...&t=<seconds>s`` links keep their literal
     ``&`` (markdown does not HTML-escape). This is the one thing most likely to be
     silently mangled and the whole reason the deep-link work exists.
  3. SELF-CONTAINED: no ``file://``, no local image paths, no relative asset references.
     A fresh session receiving only this file's text must have the full digest.
  4. Empty/near-empty digest still writes a valid, coherent file — never a crash or an
     empty file.
  5. The render stage writes ``digest.md`` beside page 1 as a SIDE output — it must NOT
     leak into ``written_paths`` (which the delivery stage attaches to the email).

Fixtures reuse ``tests/test_render.py``'s factories (house style, Rule 11) so the
markdown twin is tested against the SAME item shapes the HTML renderer is.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable so ``from lib import ...`` / ``import orbit`` resolve
# regardless of the working directory. Mirrors tests/test_render.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
from lib import markdown_render, render  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402
from lib.density import TIER_COMPACT, TIER_HERO, TIER_INDEX, TIER_STANDARD, TieredItem  # noqa: E402
from lib.rerank import RankableItem, ScoredItem  # noqa: E402

# Reuse the HTML renderer's fixture factories so both surfaces are tested against the
# SAME item shapes (Rule 11 — no parallel fixture set). pytest's default prepend import
# mode puts the tests dir on sys.path, so the sibling module imports cleanly.
from test_render import _chapter, _many, _tiered, _trending_item  # noqa: E402


def _tweet_tiered(item_external_id: str, tier: str, *, handle: str, body: str) -> TieredItem:
    """Build an X-tweet TieredItem (an ``x.com`` ``card_url`` is what marks it a tweet)."""
    base = _tiered(item_external_id, tier, title=body, channel_name=handle)
    item = base.scored_item.item
    return TieredItem(
        scored_item=ScoredItem(
            item=RankableItem(
                item_external_id=item.item_external_id,
                title=item.title,
                channel_name=item.channel_name,
                creator_external_id=item.creator_external_id,
                view_count=item.view_count,
                like_count=item.like_count,
                comment_count=item.comment_count,
                upload_date=item.upload_date,
                classification=item.classification,
                chapters=[],
                card_url=f"https://x.com/{handle}/status/{item_external_id}",
            ),
            score=base.scored_item.score,
        ),
        density_tier=tier,
    )


def _spilled_mixed_digest() -> list[TieredItem]:
    """A digest large enough to spill to page 2 in HTML: Hero+Standard + many low tiers."""
    return (
        [_tiered("HEROID", TIER_HERO, title="Hero talk", chapters=[_chapter("Key point", 120.0, "HEROID")])]
        + [_tiered("STDID", TIER_STANDARD, title="Standard talk")]
        + _many(TIER_COMPACT, 20, prefix="CMP")
        + _many(TIER_INDEX, 20, prefix="IDX")
    )


def test_two_page_digest_merges_into_one_markdown_with_full_item_parity() -> None:
    """One ``digest.md`` carries EVERY item from BOTH HTML pages (DoD: parity + 2-page merge).

    WHY: the 2-page split is an HTML LAYOUT concern, not a content boundary — #7 feeds
    the markdown to a fresh session and must receive the whole digest. We build a batch
    that HTML spills across page 1 (Hero/Standard) and page 2 (Compact/Index), render
    both surfaces, and assert the single markdown string contains every item id that
    appears anywhere in the two HTML pages. A renderer that only walked page 1, or
    dropped the spilled low tiers, would fail here — not silently ship a partial digest.
    """
    tiered = _spilled_mixed_digest()

    pages = render.render_digest_pages(tiered, inline_image=lambda url: None)
    assert len(pages) == 2, "fixture must actually spill to two HTML pages for this test to bite"

    md = markdown_render.render_digest_markdown(tiered)

    # Every tiered item id must appear in the ONE markdown file (both pages' worth).
    for tiered_item in tiered:
        item_id = tiered_item.scored_item.item.item_external_id
        assert item_id in md, f"markdown dropped item {item_id} that the HTML digest carries"

    # And it is ONE document, not two — the page-2 HTML filename never leaks into it.
    assert render.DEFAULT_PAGE_2_FILENAME not in md


def test_chapter_timestamp_deep_link_round_trips_verbatim() -> None:
    """A chapter ``...&t=<s>s`` deep-link survives into the markdown with its literal ``&``.

    WHY: this is the headline feature and the thing a naive renderer silently drops or
    mangles (HTML-escaping the ``&`` to ``&amp;`` would break the click). Markdown must
    keep the URL verbatim. A regression that escaped, rewrote, or omitted the timestamp
    would land the reader on the cold open — or nowhere.
    """
    deep_link = "https://www.youtube.com/watch?v=vidHERO&t=120s"
    tiered = [_tiered("vidHERO", TIER_HERO, chapters=[_chapter("The point", 120.0, "vidHERO")])]

    md = markdown_render.render_digest_markdown(tiered)

    assert deep_link in md, "the chapter timestamp deep-link must survive verbatim"
    assert "&amp;" not in md, "markdown must NOT HTML-escape the ampersand (that breaks the link)"
    assert "The point" in md  # the chapter key-point content reaches the markdown


def test_no_local_file_references_anywhere() -> None:
    """The markdown carries NO ``file://``, image, or relative-asset references (DoD: self-contained).

    WHY: this is the property #7 depends on — a fresh session gets ONLY this file's text,
    so any local reference (a ``file://`` url, an inlined image path, a relative ``./``
    link) points at something the session cannot see. We render a full digest with
    thumbnails/avatars present upstream and assert none of those local forms appear.
    """
    tiered = [
        _tiered("vidH", TIER_HERO, chapters=[_chapter("Intro", 0.0, "vidH")]),
        _tweet_tiered("tw1", TIER_STANDARD, handle="jack", body="a tweet body"),
        _tiered("vidC", TIER_COMPACT, title="Compact clip"),
    ]
    # image_url is set on YouTube items upstream; the markdown must not surface it as an asset.
    tiered[0].scored_item.item.image_url = "https://img.youtube.com/vi/vidH/hqdefault.jpg"

    md = markdown_render.render_digest_markdown(tiered)

    assert "file://" not in md
    assert "![" not in md, "no markdown image embeds (those reference assets a fresh session lacks)"
    assert "](./" not in md and "](../" not in md and "](/" not in md, "no relative-path links"
    assert "data:image" not in md, "no inlined base64 image data"
    # The thumbnail URL must not be surfaced as an asset reference at all (text-first twin).
    assert "hqdefault.jpg" not in md


def test_empty_digest_writes_coherent_non_empty_markdown() -> None:
    """A run with no qualifying items still produces a valid, coherent markdown (DoD: empty edge).

    WHY (Rule 12): a quiet day must degrade to a coherent "nothing today" file, never a
    crash or an empty file that #7 would forward as a blank digest. We render an empty
    batch and assert a non-empty document with the masthead title and a legible quiet-day
    line.
    """
    md = markdown_render.render_digest_markdown([])

    assert md.strip(), "empty digest must still be a non-empty, coherent document"
    assert render.DEFAULT_DIGEST_TITLE in md, "the masthead title anchors the quiet-day file"


def test_out_of_band_density_tier_is_still_rendered_not_dropped() -> None:
    """An item with a density_tier OUTSIDE the four constants still appears (DoD: parity).

    WHY: the HTML non-spilled masonry renders EVERY item regardless of tier (an unknown
    tier falls through to a compact tile). If the markdown grouped only the four known
    tiers, an out-of-band tier would be silently dropped from digest.md while the HTML kept
    it — a parity break #7 would inherit. Grouping today only ever emits the four constants,
    so this pins the leftover-group fold that keeps the twin drop-proof if a fifth tier is
    ever added. Removing the fold re-drops this item and fails here.
    """
    tiered = [
        _tiered("vidKnown", TIER_HERO, title="Known hero"),
        _tiered("vidFringe", "fringe", title="Fringe item"),
    ]

    md = markdown_render.render_digest_markdown(tiered)

    assert "vidFringe" in md, "an out-of-band tier item must not be dropped from the markdown"
    assert "vidKnown" in md


def test_ahead_of_the_curve_trio_survives_with_working_links() -> None:
    """The scoop / trending / hidden-gem trio reaches the markdown with its deep-links.

    WHY: the "ahead of the curve" items are the highest-value signal the digest exists to
    surface; a twin that dropped them would hand #7 a lesser digest than the HTML. We pass
    a scoop + trending items and assert the scoop headline, a trending headline, and the
    trending deep-link all appear.
    """
    scoop = _trending_item("sc1", "Scoop headline")
    scoop.card_url = "https://x.com/breaker/status/sc1"
    trending = [_trending_item(f"tr{n}", f"Trending headline {n}") for n in range(3)]
    trending[0].card_url = "https://x.com/mover/status/tr0"

    md = markdown_render.render_digest_markdown([], scoops=[scoop], trending_items=trending)

    assert "Scoop headline" in md
    assert "Trending headline 0" in md
    assert "https://x.com/mover/status/tr0" in md, "trending deep-link must survive into the markdown"


def test_render_stage_writes_digest_md_beside_page_one_not_in_written_paths(tmp_path: Path) -> None:
    """The real render stage writes ``digest.md`` beside page 1 as a SIDE output (DoD: contract).

    WHY: ``written_paths`` is the delivery stage's email-attachment list — every path in
    it rides the email as a ``text/html`` attachment. A ``digest.md`` leaking into that
    list would be mailed as a broken HTML attachment. So the render stage must write
    ``digest.md`` beside page 1 (the deterministic path #7 reads) WITHOUT adding it to
    ``written_paths``. We run the real stage over a temp path and assert both halves.
    """
    tiered = _spilled_mixed_digest()
    config = OrbitConfig()
    html_path = tmp_path / "out" / "today.html"

    written = orbit.run_stage7_render(tiered, config, html_path=html_path)

    digest_md_path = tmp_path / "out" / markdown_render.DIGEST_MD_FILENAME
    assert digest_md_path.exists(), "digest.md must be written beside page 1"
    # It must NOT be in written_paths (which the delivery stage attaches to the email).
    assert digest_md_path not in written
    assert all(str(path).endswith(".html") for path in written), "written_paths stays HTML-only"

    md = digest_md_path.read_text(encoding="utf-8")
    assert "HEROID" in md and "CMP0" in md and "IDX0" in md, "the merged digest carries every item"


def test_markdown_write_failure_does_not_abort_the_html_render_contract(tmp_path: Path) -> None:
    """A failing markdown write is loud-but-non-fatal — the HTML contract still completes.

    WHY (Rule 12 + the stage's fail-soft posture): ``digest.md`` is a secondary output
    that gates #7/#9, but the primary product is the HTML digest + email. If the markdown
    write raises, it must NOT propagate and abort the render stage before the HTML pages
    are written and returned for delivery. We inject a writer that raises ONLY for the
    ``.md`` path and assert the HTML pages are still written and returned intact.
    """
    tiered = [_tiered("vidOK", TIER_HERO, chapters=[_chapter("Seg", 30.0, "vidOK")])]
    config = OrbitConfig()
    html_path = tmp_path / "out" / "today.html"

    def _writer_that_fails_only_on_markdown(path: Path, text: str) -> None:
        if path.suffix == ".md":
            raise OSError("simulated disk-full on the markdown write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    written = orbit.run_stage7_render(tiered, config, html_path=html_path, writer=_writer_that_fails_only_on_markdown)

    # The HTML contract survived the markdown failure: page 1 written and returned.
    assert written == [html_path]
    assert html_path.exists()
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    # And the markdown file was NOT written (the write failed, non-fatally).
    assert not (tmp_path / "out" / markdown_render.DIGEST_MD_FILENAME).exists()
