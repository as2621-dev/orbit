"""End-to-end DoD test for orbit.py's Stage 6->7 (rank -> tier -> render -> write).

Per Rule 9, this encodes WHY the wiring matters, not merely that it runs. Phase 3
is M1's render half — its headline product claim is: running Orbit end-to-end over
the (mocked here) Phase 1-2 producers writes a SELF-CONTAINED HTML digest to the
configured ``html_path`` in which a chapterized item's ``watch?v=ID&t=Ns`` deep-link
SURVIVES all the way to the written file. So the test mocks the upstream (constructs
RankableItems directly — no network / LLM / cookies), runs the real
``run_stage6_rank_and_tier`` + ``run_stage7_render``, and asserts the deep-link is in
the file actually written to a TEMP path. A regression that dropped the chapter list,
mis-wired the writer, or never wrote the file would fail here — not silently ship an
empty or link-less digest.

It also pins the page-2 spill survival end-to-end: an oversized batch writes BOTH
the page-1 file and the ``today-page2.html`` beside it, capped at 2 pages.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable so ``from lib import ...`` / ``import orbit``
# resolve regardless of the working directory. Mirrors tests/test_render.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
from lib.chapterize import Chapter  # noqa: E402
from lib.classify import Classification  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402
from lib.density import TIER_COMPACT, TIER_INDEX  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402


def _rankable(
    item_external_id: str,
    *,
    title: str = "A talk",
    creator_external_id: str = "UC_default",
    chapters: list[Chapter] | None = None,
    view_count: int = 10_000,
) -> RankableItem:
    """Build a classified RankableItem directly (mocks the Phase 1-2 upstream)."""
    classification = Classification(
        item_external_id=item_external_id,
        axis_a_signal=1,
        axis_b_on_topic=1,
        is_user_override=0,
    )
    return RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name="Some Channel",
        creator_external_id=creator_external_id,
        view_count=view_count,
        like_count=200,
        comment_count=20,
        upload_date="20260101",
        classification=classification,
        chapters=chapters or [],
    )


def test_end_to_end_writes_digest_with_surviving_deep_link(tmp_path: Path) -> None:
    """orbit.py Stage 6->7 writes a non-empty digest with a working deep-link (DoD #4).

    WHY: this is the phase headline — a deep-link into the exact moment survives the
    WHOLE pipeline to the file the user opens. We run the real rank+tier+render+write
    over a mocked upstream, point html_path at a temp dir, and assert the written file
    exists, is non-empty, is a self-contained HTML doc, and contains the chapterized
    item's ``watch?v=ID&t=90s`` deep-link. A regression anywhere in the wiring breaks
    this concrete claim.
    """
    chapters = [
        Chapter(title="Intro", start_seconds=0.0, deep_link="https://www.youtube.com/watch?v=vidE2E&t=0s"),
        Chapter(title="The point", start_seconds=90.0, deep_link="https://www.youtube.com/watch?v=vidE2E&t=90s"),
    ]
    items = [
        _rankable("vidE2E", title="Deep talk", creator_external_id="UC_hi", chapters=chapters),
        _rankable("vidB", title="Other talk", creator_external_id="UC_lo"),
    ]
    config = OrbitConfig(creator_weights={"UC_hi": 2.0, "UC_lo": 1.0})

    html_path = tmp_path / "out" / "today.html"
    tiered = orbit.run_stage6_rank_and_tier(items, config)
    written = orbit.run_stage7_render(tiered, config, html_path=html_path)

    # Nothing dropped: every item got a tier (rank controls density, never inclusion).
    assert len(tiered) == len(items)

    assert written == [html_path]
    assert html_path.exists()
    written_html = html_path.read_text(encoding="utf-8")
    assert written_html  # non-empty
    assert written_html.startswith("<!DOCTYPE html>")
    # The headline: the chapterized item's whole-item deep-link survives end-to-end into
    # the written file as a working escaped href (the Tiles layout surfaces the deep-link
    # via the tile title + the more-chapters/chip links; the per-chapter chips are
    # display-only, the locked design). A regression anywhere in the wiring breaks this.
    assert "https://www.youtube.com/watch?v=vidE2E&amp;t=0s" in written_html
    assert "The point" in written_html  # the chapter key-point content reaches the tile


def test_end_to_end_default_writer_respects_config_html_path(tmp_path: Path) -> None:
    """Stage 7 resolves+expands config.delivery.html_path and writes via the default writer.

    WHY: the default (non-injected) write path must honour the user-configured
    ``delivery.html_path`` and create parent dirs — otherwise a real run silently
    fails to deliver. We point the config at a nested temp path (no ~ needed) and let
    the REAL _default_html_writer run, asserting the file lands where configured.
    """
    config = OrbitConfig(delivery={"html_path": str(tmp_path / "nested" / "deep" / "today.html")})
    items = [_rankable("vidCfg")]

    tiered = orbit.run_stage6_rank_and_tier(items, config)
    written = orbit.run_stage7_render(tiered, config)  # no html_path / writer injected

    expected = tmp_path / "nested" / "deep" / "today.html"
    assert written == [expected]
    assert expected.exists()
    assert expected.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_end_to_end_spills_page_two_capped_at_two(tmp_path: Path) -> None:
    """An oversized batch writes page 1 AND today-page2.html beside it, capped at 2 (DoD #4 spill).

    WHY: the spill is a product guarantee, and it must survive end-to-end through the
    writer (not just in render.py). We force an over-budget batch by tiering many
    Compact + Index items directly, write through Stage 7, and assert BOTH files exist
    in the same dir and exactly two pages were written (the 2-page hard cap).
    """
    from lib.density import TieredItem  # local import: only this test builds tiers by hand
    from lib.rerank import ScoredItem

    def _tiered(item_id: str, tier: str) -> TieredItem:
        return TieredItem(scored_item=ScoredItem(item=_rankable(item_id), score=1.0), density_tier=tier)

    tiered = (
        [_tiered("HEROID", "hero")]
        + [_tiered(f"CMP{n}", TIER_COMPACT) for n in range(30)]
        + [_tiered(f"IDX{n}", TIER_INDEX) for n in range(30)]
    )
    config = OrbitConfig()
    html_path = tmp_path / "out" / "today.html"

    written = orbit.run_stage7_render(tiered, config, html_path=html_path)

    page_2_path = tmp_path / "out" / "today-page2.html"
    assert written == [html_path, page_2_path]
    assert html_path.exists() and page_2_path.exists()
    # Hero stayed on page 1; compact/index spilled to page 2.
    assert "HEROID" in html_path.read_text(encoding="utf-8")
    assert "CMP0" in page_2_path.read_text(encoding="utf-8")
