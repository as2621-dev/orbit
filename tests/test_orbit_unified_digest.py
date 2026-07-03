"""DoD test for the M2 unified digest (Phase 4 / Sub-phase 4) — the milestone deliverable.

Per Rule 9, this encodes WHY the unified pipeline matters, not merely that it runs. The
M2 promise is ONE digest spanning BOTH sources: a YouTube video card AND an X tweet card
in the SAME rendered HTML, each linking to its own platform. The riskiest seam is that
X items reach render at all and render with a CORRECT x.com link (render.py historically
hardcoded a youtube.com link for every card — a broken-link regression would silently
ship X cards pointing at youtube.com). So this test:

  * mocks BOTH subprocess boundaries (YouTube loader + X following loader + X delta) and
    the LLM — NO network, NO cookies, NO live model;
  * drives orbit.py Stage 0 (load both sources) -> Stage 1 X build (delta + shared
    classify + RankableItem.from_tweet) -> merge with a YouTube RankableItem -> the REAL
    run_stage6_rank_and_tier -> run_stage7_render writing to a temp path;
  * asserts the WRITTEN HTML contains BOTH a YouTube ``watch?v=`` card AND an X
    ``x.com/<handle>/status/<id>`` card.

It must FAIL if X items never reach render, or render with a broken/youtube URL.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make ``scripts`` importable. Mirrors tests/test_orbit_pipeline.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
import store  # noqa: E402
from lib import paths  # noqa: E402
from lib.bird_x import Follow, Tweet  # noqa: E402
from lib.classify import Classification  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402
from lib.youtube_yt import Subscription  # noqa: E402


def _fresh_store(tmp_dir: Path) -> None:
    """Point the store at a temp DB and init it (no real ~/.local/share write)."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()


def _youtube_rankable() -> RankableItem:
    """A classified YouTube RankableItem (mocks the still-stubbed YouTube Stage-1 producer).

    The YouTube delta/classify producer is upstream-stubbed; we construct its OUTPUT
    directly so the merge has a real YouTube item to render alongside the X item.
    """
    classification = Classification(
        item_external_id="ytVIDEO01",
        axis_a_signal=1,
        axis_b_on_topic=1,
        is_user_override=0,
    )
    return RankableItem(
        item_external_id="ytVIDEO01",
        title="A YouTube deep dive",
        channel_name="Some YT Channel",
        creator_external_id="UC_yt",
        view_count=50_000,
        like_count=2_000,
        comment_count=150,
        upload_date="20260101",
        classification=classification,
        chapters=[],
    )  # card_url defaults "" -> YouTube watch?v= fallback in render


def test_unified_digest_contains_both_youtube_and_x_cards(tmp_path: Path) -> None:
    """ONE rendered digest carries BOTH a YouTube watch?v= card AND an X x.com card (M2 DoD).

    WHY: this is the M2 milestone deliverable. We mock every boundary (YT loader, X
    following loader, X delta, LLM), drive orbit.py Stage 0 -> Stage 1 (X) -> merge ->
    rank -> render -> write, and assert the WRITTEN HTML has both a YouTube
    ``watch?v=ytVIDEO01`` link and the X tweet's ``x.com/alice/status/<id>`` link. A
    regression that (a) never merged X items into the stream, or (b) rendered the X card
    with the hardcoded youtube link, fails this assertion.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))

        # --- Stage 0: load BOTH sources (mocked loaders, no network) ---
        def _mock_yt_loader(cookie_source: str) -> list[Subscription]:
            return [Subscription(channel_id="UC_yt", display_name="Some YT Channel")]

        def _mock_x_loader(cookie_source: str) -> list[Follow]:
            return [Follow(creator_handle="alice", display_name="Alice", rest_id="1001")]

        config = OrbitConfig(
            creator_weights={"UC_yt": 1.0, "alice": 1.0},
            interests=["ai"],
        )
        orbit.run_stage0_load_sources(
            config,
            loader=_mock_yt_loader,
            x_loader=_mock_x_loader,  # real bird_x.persist_following persists to the temp store
        )

        # X following is queryable as an x-platform source (Stage 0 persisted it).
        x_sources = store.list_sources(platform="x")
        assert [row["external_id"] for row in x_sources] == ["alice"], "Stage 0 must persist the X following"

        # --- Stage 1 (X half): mock the delta + the LLM; build classified RankableItems ---
        x_tweet = Tweet(
            text="A sharp X take on model scaling",
            tweet_id="1900000000000000042",
            handle="alice",
            created_at="2026-06-18T00:00:00Z",
            like_count=300,
            retweet_count=80,
            reply_count=12,
            quote_count=4,
        )

        def _mock_x_delta(sources, depth, run_day_ordinal):  # noqa: ANN001 — test stub
            return [x_tweet]

        x_items = orbit.run_stage1_build_x_items(
            config,
            depth="default",
            run_day_ordinal=0,
            x_delta=_mock_x_delta,
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
        )

        # --- Merge YouTube + X into ONE unified stream, then the REAL rank/tier/render ---
        youtube_items = [_youtube_rankable()]
        unified_items = youtube_items + x_items

        html_path = tmp_path / "out" / "today.html"
        tiered = orbit.run_stage6_rank_and_tier(unified_items, config)
        # Stub the image-inline seam so the tweet avatar (unavatar.io) is NOT fetched —
        # the render path must stay offline in tests (no network).
        written = orbit.run_stage7_render(
            tiered, config, html_path=html_path, inline_image=lambda url: None
        )

        written_html = html_path.read_text(encoding="utf-8")

    # Nothing dropped: both the YouTube and the X item are tiered.
    assert len(tiered) == len(unified_items) == 2, "both sources must reach render (nothing dropped)"
    assert written == [html_path]

    # THE M2 DELIVERABLE: BOTH cards in the SAME digest.
    assert "https://www.youtube.com/watch?v=ytVIDEO01" in written_html, "YouTube card must carry a watch?v= link"
    assert "https://x.com/alice/status/1900000000000000042" in written_html, "X card must carry an x.com status link"

    # The X card must NOT render with the broken hardcoded youtube link for the tweet id
    # (the historical render.py gap this sub-phase fixes).
    assert "watch?v=1900000000000000042" not in written_html, "X tweet must NOT render a youtube watch link"


def test_stage0_x_auth_failure_does_not_abort_youtube_only_run(tmp_path: Path) -> None:
    """An X auth/config failure is swallowed so a YouTube-only digest still renders.

    WHY: X is an ADDITIVE source (Phase 4). A user with no X cookies / X_USER_ID must
    still get their YouTube digest — Orbit must not abort Stage 0 because the optional X
    source is unconfigured. We make the X loader raise XAuthError and assert Stage 0 does
    NOT propagate it and the YouTube source still persisted.
    """
    from lib.bird_x import XAuthError

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))

        def _mock_yt_loader(cookie_source: str) -> list[Subscription]:
            return [Subscription(channel_id="UC_yt", display_name="Some YT Channel")]

        def _raising_x_loader(cookie_source: str) -> list[Follow]:
            raise XAuthError("X user id not configured: set the X_USER_ID environment variable. See README.")

        config = OrbitConfig()
        # Must NOT raise — the X failure is swallowed (logged), YouTube proceeds.
        orbit.run_stage0_load_sources(config, loader=_mock_yt_loader, x_loader=_raising_x_loader)

        youtube_sources = store.list_sources(platform="youtube")
        x_sources = store.list_sources(platform="x")

    assert [row["external_id"] for row in youtube_sources] == ["UC_yt"], "YouTube source must persist despite X failure"
    assert x_sources == [], "no X source persisted when the X loader failed (swallowed, not fatal)"
