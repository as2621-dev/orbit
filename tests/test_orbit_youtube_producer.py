"""DoD test for the YouTube Stage 1-2 producer wired into orbit.py (run_stage1_build_youtube_items).

Per Rule 9, this encodes WHY the producer matters, not merely that it runs. Before this
wiring a daily ``/orbit`` run was X-only: the YouTube delta/classify/chapterize building
blocks existed and were unit-tested, but no orbit.py stage assembled them, so a
YouTube-only user got an empty digest. These tests pin the now-load-bearing contracts of
the assembled producer:

  * a new upload is classified (two-axis) and reaches the unified stream as a RankableItem
    carrying its ``duration`` (no transcript/chapterize here anymore — those moved to the
    winner-only Stage 5);
  * each built upload is marked ``seen`` so the delta engine never resurfaces it next run;
  * the winner-summarize stage transcribes + summarizes ONLY winners, and its floor of
    >=8 video summaries holds even at ``quick`` depth (force-fetch), with every
    summary-bullet offset snapped to a REAL transcript cue (the deep-link invariant).

Every boundary is mocked: NO network, NO cookies, NO live model.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Make ``scripts`` importable. Mirrors tests/test_orbit_unified_digest.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
import store  # noqa: E402
from lib import paths  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402
from lib.transcribe import Transcript, TranscriptCue  # noqa: E402
from lib.youtube_yt import Upload  # noqa: E402


def _fresh_store_with_channel(tmp_dir: Path, *, category: str = "signal") -> int:
    """Point the store at a temp DB, init it, and persist one YouTube source.

    Returns the source_id so a test can assert against the ``seen`` table for it.
    """
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()
    return store.upsert_source(
        platform="youtube",
        external_id="UC_chan",
        display_name="A YT Channel",
        category=category,
    )


def _upload(
    video_id: str,
    *,
    duration: int | None,
    chapters: list[dict] | None = None,
) -> Upload:
    """Build an Upload (mocks one yt-dlp delta entry)."""
    return Upload(
        video_id=video_id,
        title="A talk",
        description="about ai agents",
        # Reason: dated "today" so it clears the stage-1 recency gate (last N days). The
        # date is incidental to what this fixture exercises (classify/chapterize/mark_seen);
        # a dynamic value keeps the test from going stale as wall-clock time advances.
        upload_date=datetime.now(timezone.utc).strftime("%Y%m%d"),
        view_count=10_000,
        like_count=200,
        comment_count=20,
        duration=duration,
        channel_name="A YT Channel",
        chapters=chapters,
    )


def test_youtube_producer_classifies_carries_duration_and_marks_seen(tmp_path: Path) -> None:
    """The producer builds classified RankableItems (carrying duration) and marks each seen.

    WHY: this is the wiring the milestone needs — a YouTube-only user must get real items.
    Post-reorder, Stage 1 no longer transcribes/chapterizes (that is winner-only Stage 5),
    so here we assert: both uploads become RankableItems carrying their ``duration`` and
    classification; NEITHER carries chapters yet; and BOTH ids are marked ``seen`` so next
    run's delta skips them. A regression that dropped classify, duration, or the seen-mark
    fails here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store_with_channel(Path(tmp))

        long_upload = _upload("vidLONG", duration=1800)  # > 1200s
        short_upload = _upload("vidSHORT", duration=300)

        def _mock_delta(source, depth):  # noqa: ANN001 — test stub
            return [long_upload, short_upload]

        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        items = orbit.run_stage1_build_youtube_items(
            config,
            depth="default",
            upload_delta=_mock_delta,
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
        )

        seen_ids = store.get_seen_ids(source_id)

    # Both uploads reach the unified stream (nothing dropped), carrying duration.
    assert [item.item_external_id for item in items] == ["vidLONG", "vidSHORT"]
    assert items[0].duration == 1800
    assert items[1].duration == 300

    # Classified (two-axis verdict carried through); no chapters yet (Stage 5 does those).
    assert items[0].classification is not None and items[0].classification.axis_a_signal == 1
    assert items[0].chapters == [] and items[1].chapters == []

    # Both uploads are now marked seen so the delta engine never resurfaces them.
    assert seen_ids == {"vidLONG", "vidSHORT"}


def test_summarize_winners_quick_depth_force_fetches_and_summarizes(tmp_path: Path) -> None:
    """Stage 5 summarizes winners (transcript force-fetched, snapped) even at ``quick`` depth.

    WHY: summaries are the digest's headline value, so the floor of >=8 video topics must
    hold at every depth — including ``quick`` (where the OLD transcript budget was 0). We
    crown one long-form winner and run the winner-summarize stage at ``quick`` depth with a
    spy fetcher, then assert: the transcript fetcher WAS called with ``force=True`` (the
    quick gate is bypassed for the bounded winner set); and the winner carries exactly 5
    summary bullets, each deep-linked to a REAL snapped cue offset. A regression that let
    the quick=0 gate starve summaries fails here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_channel(Path(tmp))

        winner = orbit.RankableItem.from_parts(
            _upload("vidLONG", duration=1800), None, [], creator_external_id="UC_chan"
        )

        transcript = Transcript(
            video_id="vidLONG",
            cues=[
                TranscriptCue(cue_start_seconds=0.0, cue_end_seconds=5.0, text="intro"),
                TranscriptCue(cue_start_seconds=90.0, cue_end_seconds=95.0, text="the point"),
            ],
        )

        fetch_calls: list[tuple[str, str, bool]] = []

        def _spy_transcript_fetcher(video_id, depth, *, force=False):  # noqa: ANN001 — test stub
            fetch_calls.append((video_id, depth, force))
            return transcript

        # 5-bullet summary; the model's start_seconds=88 must snap to the real cue at 90.0.
        five_bullets = (
            '[{"text":"a","start_seconds":0},{"text":"b","start_seconds":88},'
            '{"text":"c","start_seconds":0},{"text":"d","start_seconds":90},{"text":"e","start_seconds":0}]'
        )
        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        orbit.run_stage5_summarize_winners(
            [winner],
            depth="quick",
            config=config,
            transcript_fetcher=_spy_transcript_fetcher,
            summarizer=lambda prompt: five_bullets,
        )

    # The transcript was force-fetched despite quick depth (floor must hold at quick).
    assert fetch_calls == [("vidLONG", "quick", True)]
    # The winner carries exactly 5 bullets; bullet at 88 snapped to the real cue 90.0.
    assert winner.summary is not None and len(winner.summary.bullets) == 5
    deep_links = [bullet.deep_link for bullet in winner.summary.bullets]
    assert "https://www.youtube.com/watch?v=vidLONG&t=90s" in deep_links


def test_youtube_producer_no_sources_is_noop(tmp_path: Path) -> None:
    """With no persisted YouTube sources the producer returns [] (YouTube-less setups).

    WHY: an X-only user must not crash the YouTube half. We init an empty store and assert
    the producer is a clean no-op.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "orbit.db"
        os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
        store._db_override = db_path
        store.init_db()

        items = orbit.run_stage1_build_youtube_items(OrbitConfig(), depth="default")

    assert items == []


def test_crown_winners_picks_one_per_cluster_and_footnotes_the_rest() -> None:
    """Stage 4 crowns ONE winner per cluster and folds the losers in as its footnotes.

    WHY: the dedup contract — five videos on one topic become one summarized winner plus
    footnotes, not five cards. We feed three same-topic items (one clearly bigger + longer)
    plus one unrelated item, then assert: two winners (the big topic item + the singleton);
    the topic winner is the high-engagement long one; and its ``.footnotes`` are exactly the
    two losers. A regression that crowned the wrong member or left duplicates as separate
    winners fails here.
    """
    def _ri(item_id, title, creator, views, duration):  # noqa: ANN001 — test stub
        return orbit.RankableItem(
            item_external_id=item_id, title=title, channel_name=creator, creator_external_id=creator,
            view_count=views, like_count=views // 10, comment_count=views // 100,
            upload_date="20260101", duration=duration,
        )

    big = _ri("big", "Apple M5 chip is insane wow", "c1", 500_000, 1800)
    small_a = _ri("sa", "Apple M5 chip is insane wow", "c2", 50, 600)
    small_b = _ri("sb", "Apple M5 chip is insane wow", "c3", 40, 600)
    solo = _ri("solo", "A completely unrelated bread baking tutorial", "c4", 100, 300)
    items = [big, small_a, small_b, solo]
    config = OrbitConfig(creator_weights={})

    clusters = orbit.run_stage3_cluster(items, config)
    winners = orbit.run_stage4_crown_winners(items, clusters, config)

    winner_ids = {winner.item_external_id for winner in winners}
    assert len(winners) == 2, "one winner per cluster (the topic cluster + the singleton)"
    assert "big" in winner_ids, "the high-engagement, long video must win its topic cluster"
    assert "solo" in winner_ids, "a singleton is its own winner"

    topic_winner = next(winner for winner in winners if winner.item_external_id == "big")
    assert {footnote.item_external_id for footnote in topic_winner.footnotes} == {"sa", "sb"}
    solo_winner = next(winner for winner in winners if winner.item_external_id == "solo")
    assert solo_winner.footnotes == [], "a singleton winner has no footnotes"
