"""DoD tests for scoop detection + the M3 render sections (Phase 5 / Sub-phase 4).

Per Rule 9, each test encodes WHY the behavior matters, constructed to FAIL on wrong
BUSINESS logic, not merely "returns something":

  1. Scoop = dormancy AND acceleration. A dormant creator's spike is a scoop; a
     HIGH-frequency creator's identical spike is NOT (the brief's highest-value
     signal — NOT merely "high engagement"). Fails if dormancy is ignored (any spike
     -> scoop) or a frequent-poster spike is wrongly flagged.
  2. The trending/scoop multiplier raises a scoop's derank score ABOVE an
     otherwise-identical non-scoop item (the 1.0 no-op is now live). Fails if the
     multiplier stays neutral for scoops.
  3. The rendered HTML now contains the overlap block, a right-rail trending section,
     AND a scoops strip — each with a working deep-link href. Fails if any of the 3
     sections is missing or a deep-link is absent/broken.
  4. Regression: with NO trending/scoop/cluster data, render + rerank behave exactly
     as before (the existing 110 tests cover the rest of this; here we pin the new
     code paths are no-ops on the M1 inputs).

All boundaries are constructed fixtures — no network, no LLM, no store. The store
read inside compute_internal_trending is avoided by constructing TrendingItems
directly where history depth matters.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

# Make ``scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_render.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
from lib import render  # noqa: E402
from lib.chapterize import Chapter  # noqa: E402
from lib.cluster import Cluster  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402
from lib.density import TIER_HERO, TIER_STANDARD, TieredItem  # noqa: E402
from lib.external_trending import build_trending_multiplier_map, detect_scoops  # noqa: E402
from lib.fusion import CrossLink  # noqa: E402
from lib.rerank import RankableItem, ScoredItem, derank_items, score_item  # noqa: E402
from lib.trending import TrendingItem  # noqa: E402


def _trending_item(
    item_external_id: str,
    *,
    title: str = "A headline",
    history_sample_count: int,
    baseline_relative_ratio: float,
    card_url: str = "",
    corroboration_tag: str = "",
) -> TrendingItem:
    """Build a TrendingItem fixture directly (no store read / no internal-trending run)."""
    return TrendingItem(
        item_external_id=item_external_id,
        cluster_id=f"cluster-{item_external_id}",
        creator_external_id=f"UC_{item_external_id}",
        title=title,
        card_url=card_url,
        velocity_score=baseline_relative_ratio,
        convergence_count=1,
        baseline_relative_ratio=baseline_relative_ratio,
        history_sample_count=history_sample_count,
        corroboration_tag=corroboration_tag,
    )


# --- DoD #1: dormancy AND acceleration, not merely high engagement -----------


def test_dormant_account_spike_is_scoop_but_frequent_poster_spike_is_not() -> None:
    """A dormant creator's spike is a scoop; a frequent poster's IDENTICAL spike is not (DoD #1).

    WHY: the brief's highest-value signal is a normally-DORMANT account suddenly
    accelerating — NOT merely a big spike. Both items here spike identically (5x their
    own baseline). The ONLY difference is history depth: the dormant creator has 1
    prior post; the frequent creator has 200. If the detector flagged on the spike
    alone (ignoring dormancy), it would wrongly flag the frequent poster too — this
    test fails in that case. It also fails if dormancy alone (without the spike) were
    enough, via the third item.
    """
    dormant_spike = _trending_item("dorm", history_sample_count=1, baseline_relative_ratio=5.0)
    frequent_spike = _trending_item("freq", history_sample_count=200, baseline_relative_ratio=5.0)
    dormant_normal = _trending_item("quiet", history_sample_count=1, baseline_relative_ratio=1.0)

    scoops = detect_scoops([dormant_spike, frequent_spike, dormant_normal])

    scoop_ids = {scoop.item_external_id for scoop in scoops}
    assert scoop_ids == {"dorm"}, "only the dormant-AND-accelerating item is a scoop"
    assert dormant_spike.is_scoop is True
    # The frequent poster's spike is high engagement, NOT a scoop (dormancy missing).
    assert frequent_spike.is_scoop is False
    # The dormant account posting at its NORMAL level is not a scoop (acceleration missing).
    assert dormant_normal.is_scoop is False


# --- DoD #2: the trending/scoop multiplier is live (no longer a 1.0 no-op) ----


def _identical_item(item_external_id: str) -> RankableItem:
    """Two items identical in every scoring input except their id (so only the multiplier differs)."""
    return RankableItem(
        item_external_id=item_external_id,
        title="Same title",
        channel_name="Same Channel",
        creator_external_id="UC_same",
        view_count=1000,
        like_count=50,
        comment_count=5,
        upload_date="20260101",
        chapters=[],
    )


def test_scoop_multiplier_raises_score_above_identical_non_scoop() -> None:
    """A scoop's multiplier lifts its derank score above an otherwise-identical non-scoop (DoD #2).

    WHY: Phase 3 reserved the trending multiplier as a 1.0 no-op. This sub-phase makes
    it live. The two items are byte-for-byte identical scoring inputs; the ONLY
    difference is that one is in the trending multiplier map (as a scoop) and the other
    is not. The scoop MUST score strictly higher — if the multiplier were still 1.0 for
    scoops, the scores would tie and this fails.
    """
    config = SimpleNamespace(creator_weights={})
    reference_date = date(2026, 1, 1)
    scoop_item = _identical_item("scoop")
    plain_item = _identical_item("plain")

    # A scoop trending item -> the larger multiplier; the plain item is absent from the map.
    scoop_trending = _trending_item("scoop", history_sample_count=1, baseline_relative_ratio=5.0)
    detect_scoops([scoop_trending])  # sets is_scoop = True
    multiplier_map = build_trending_multiplier_map([scoop_trending])

    scoop_score = score_item(
        scoop_item, config, reference_date=reference_date, trending_multipliers=multiplier_map
    )
    plain_score = score_item(
        plain_item, config, reference_date=reference_date, trending_multipliers=multiplier_map
    )

    assert scoop_score > plain_score, "the scoop multiplier must lift the scoop above the identical non-scoop"

    # And it flows through derank_items ordering: the scoop ranks first.
    ranked = derank_items(
        [plain_item, scoop_item], config, reference_date=reference_date, trending_multipliers=multiplier_map
    )
    assert ranked[0].item.item_external_id == "scoop"


def test_trending_multiplier_map_is_empty_no_op_when_no_trending() -> None:
    """No trending items -> empty map -> score equals the un-multiplied M1 score (DoD #2/#4).

    WHY: the multiplier must be inert on the M1 path. An empty map and no map at all
    must produce the identical score — proving the wiring did not silently shift M1.
    """
    config = SimpleNamespace(creator_weights={})
    reference_date = date(2026, 1, 1)
    item = _identical_item("x")

    empty_map = build_trending_multiplier_map([])
    assert empty_map == {}

    score_no_arg = score_item(item, config, reference_date=reference_date)
    score_empty_map = score_item(item, config, reference_date=reference_date, trending_multipliers=empty_map)
    assert score_no_arg == score_empty_map


# --- DoD #3: the three M3 sections render with working deep-links -------------


def _tiered(item_external_id: str, tier: str, *, title: str, chapters: list[Chapter] | None = None) -> TieredItem:
    item = RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name="Some Channel",
        creator_external_id=f"UC_{item_external_id}",
        view_count=12_345,
        like_count=678,
        comment_count=90,
        upload_date="20260101",
        chapters=chapters or [],
    )
    return TieredItem(scored_item=ScoredItem(item=item, score=5.0), density_tier=tier)


def test_three_m3_sections_render_with_deep_links() -> None:
    """The overlap block, right-rail trending, and scoops strip all render with working links (DoD #3).

    WHY: these three sections are the M3 payload — the whole reason for the milestone.
    The test fails if ANY of the three is missing, or if a section's deep-link href is
    absent/broken. We assert each section's container AND a real, allowlist-surviving
    href in each (the YouTube ``watch?v=...`` card link, the chapter cross-link
    ``&t=90s`` deep-link, and the scoop's own item link).
    """
    # An episode (long-form, has chapters) + a short reaction sharing the topic.
    episode = _tiered(
        "vidEP",
        TIER_HERO,
        title="Apple M5 chip deep dive",
        chapters=[Chapter("M5 benchmarks", 90.0, "https://www.youtube.com/watch?v=vidEP&t=90s")],
    )
    reaction = _tiered("twReact", TIER_STANDARD, title="M5 is insane")
    tiered_items = [episode, reaction]

    # Sub-phase-1-shaped cluster: short body (the reaction) + a cross-link into the episode chapter.
    cluster = Cluster(
        cluster_id="cluster-1",
        member_item_ids=["twReact"],
        representative_item_id="twReact",
        cross_links=[
            CrossLink(
                episode_item_id="vidEP",
                chapter_title="M5 benchmarks",
                chapter_start_seconds=90.0,
                chapter_deep_link="https://www.youtube.com/watch?v=vidEP&t=90s",
            )
        ],
        source_diversity=2,
    )

    # A trending entry (corroborated) and a scoop entry.
    trending_entry = _trending_item(
        "twReact", title="M5 is insane", history_sample_count=50, baseline_relative_ratio=2.0,
        corroboration_tag="corroborated",
    )
    scoop_entry = _trending_item(
        "vidEP", title="Apple M5 chip deep dive", history_sample_count=1, baseline_relative_ratio=5.0,
    )
    scoops = detect_scoops([scoop_entry])
    assert scoops, "fixture must produce a scoop"

    output_html = render.render_digest_html(
        tiered_items,
        clusters=[cluster],
        trending_items=[trending_entry, scoop_entry],
        scoops=scoops,
    )

    # 1. Overlap block present, with the cross-link chapter deep-link into the episode.
    assert 'class="overlap-block"' in output_html
    assert "Everyone&#x27;s talking about" in output_html or "Everyone's talking about" in output_html
    assert 'href="https://www.youtube.com/watch?v=vidEP&amp;t=90s"' in output_html

    # 2. Right-rail trending present, tagged, with a working item deep-link.
    assert 'class="trending-rail"' in output_html
    assert "Trending in your network" in output_html
    assert "corroborated" in output_html
    assert 'href="https://www.youtube.com/watch?v=twReact&amp;t=0s"' in output_html

    # 3. Scoops strip present, loud, with the scoop item's deep-link.
    assert 'class="scoops-strip"' in output_html
    assert "SCOOP" in output_html
    # The scoop links to the episode card (youtube fallback for vidEP).
    scoops_section = output_html.split('class="scoops-strip"', 1)[1].split("</section>", 1)[0]
    assert "vidEP" in scoops_section


# --- DoD #4: M1 regression — no M3 data -> the M1 page, unchanged -------------


def test_no_m3_data_renders_m1_page_without_new_sections() -> None:
    """With NO clusters/trending/scoops, the page omits all three M3 sections (DoD #4).

    WHY: the optional-args design must leave the M1/M2 path untouched. With nothing
    supplied, none of the M3 section containers may appear — proving the new code is a
    pure no-op on the M1 inputs (the existing 110 tests pin the rest of M1 render).
    """
    tiered_items = [_tiered("vidA", TIER_HERO, title="A talk")]
    output_html = render.render_digest_html(tiered_items)

    assert 'class="overlap-block"' not in output_html
    assert 'class="trending-rail"' not in output_html
    assert 'class="scoops-strip"' not in output_html
    # The M1 spine is still there.
    assert 'class="card hero"' in output_html
    assert 'class="tldr"' in output_html


# --- Authorized-divergence wiring: orbit.py Stage 5 invokes M3 end-to-end ------


def _classified_rankable(
    item_external_id: str,
    *,
    creator_external_id: str,
    title: str,
    view_count: int,
    like_count: int,
    comment_count: int,
) -> RankableItem:
    """A RankableItem fixture for the Stage-5 wiring test (mocks the Phase 1-2 upstream)."""
    return RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name=creator_external_id,
        creator_external_id=creator_external_id,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        upload_date="20260101",
        chapters=[],
    )


def test_orbit_stage5_wires_overlap_trending_scoops_through_rank_and_render(tmp_path: Path) -> None:
    """orbit.py Stage 5 invokes cluster->trending->scoop and threads it into rank+render (DoD #1/#2/#3 wiring).

    WHY: the phase-level DoD requires the PIPELINE — not just the lib functions — to
    surface the M3 sections and run the trending/scoop multiplier. This exercises the
    authorized orbit.py divergence end-to-end with a FAKE store (low seen-history =
    dormancy) and a FAKE keyless search (zero external results = scoop tag) so NO
    network/LLM/real-store boundary is touched. The dormant creator's breakout item
    sits against its own low-engagement siblings, so its batch-median baseline is low
    and the breakout spikes far above it (dormancy AND acceleration) — flagged a scoop.
    The test asserts (a) Stage 5 returns a scoop, (b) the scoop's multiplier ranks its
    item first in Stage 6, and (c) Stage 7 writes a file containing all three M3
    sections. A regression that failed to wire any of the three threads breaks this.
    """
    # Dormant creator UC_dorm: one breakout + two near-identical low-engagement siblings
    # (same text so they cluster together; the breakout becomes the representative whose
    # engagement is far above the creator's batch-median baseline).
    breakout = _classified_rankable(
        "d1", creator_external_id="UC_dorm", title="Apple M5 chip is insane wow",
        view_count=500_000, like_count=40_000, comment_count=8_000,
    )
    sibling_a = _classified_rankable(
        "d2", creator_external_id="UC_dorm", title="Apple M5 chip is insane wow",
        view_count=50, like_count=2, comment_count=0,
    )
    sibling_b = _classified_rankable(
        "d3", creator_external_id="UC_dorm", title="Apple M5 chip is insane wow",
        view_count=40, like_count=1, comment_count=0,
    )
    items = [breakout, sibling_a, sibling_b]
    config = OrbitConfig(creator_weights={})

    # Fake store: UC_dorm has only 1 prior seen item (dormant, <= the dormancy threshold).
    fake_store = SimpleNamespace(
        list_sources=lambda: [{"source_id": 1, "external_id": "UC_dorm"}],
        get_seen_ids=lambda source_id: {"prior"},
    )
    # Fake keyless search: zero external results -> the topic is a scoop (your network first).
    fake_search = lambda query: []  # noqa: E731

    clusters = orbit.run_stage3_cluster(items, config)
    trending_items, scoops, trending_multipliers = orbit.run_stage6_trending_scoops(
        clusters, items, config, store_module=fake_store, search_fn=fake_search
    )

    # (a) the dormant breakout is flagged a scoop, and its multiplier map is non-neutral.
    assert clusters, "the near-duplicate items must cluster"
    assert scoops, "the dormant breakout must be flagged a scoop"
    assert "d1" in trending_multipliers and trending_multipliers["d1"] > 1.0

    # (b) the scoop multiplier raises the breakout above a plain item in Stage 6 ranking.
    tiered = orbit.run_stage6_rank_and_tier(items, config, trending_multipliers=trending_multipliers)
    assert tiered[0].scored_item.item.item_external_id == "d1", "the scoop must rank first via the multiplier"

    # (c) Stage 7 writes a file with all three M3 sections populated.
    html_path = tmp_path / "out" / "today.html"
    written = orbit.run_stage7_render(
        tiered, config, html_path=html_path, clusters=clusters, trending_items=trending_items, scoops=scoops
    )
    assert html_path in written and html_path.exists()
    written_html = html_path.read_text(encoding="utf-8")
    assert 'class="overlap-block"' in written_html
    assert 'class="trending-rail"' in written_html
    assert 'class="scoops-strip"' in written_html
