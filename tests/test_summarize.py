"""DoD tests for the cluster-winner summarizer (lib.summarize).

Per Rule 9, each test encodes WHY the behavior matters:

  * a VIDEO winner must yield exactly 5 bullets, each deep-linked to a REAL snapped cue
    offset — the headline "5 timestamped bullets" feature and the never-invent-a-timestamp
    invariant (a bullet at a made-up second would mislead the reader to the wrong moment);
  * an unparseable model verdict must degrade to an empty-bullet summary, never crash a run;
  * a user-edited summary (is_user_override=1) is SACRED — never regenerated (the model is
    not even called), mirroring classify's override contract;
  * a TWEET winner yields 2-3 timeless bullets (no transcript, no deep-links).

Every boundary is mocked: NO network, NO live model; the store is a temp DB.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths, summarize  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402
from lib.transcribe import Transcript, TranscriptCue  # noqa: E402


def _fresh_store(tmp_dir: Path) -> Path:
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def _video_item(item_id: str = "vidLONG") -> RankableItem:
    return RankableItem(
        item_external_id=item_id,
        title="A deep dive",
        channel_name="Chan",
        creator_external_id="UC1",
        view_count=1000,
        like_count=50,
        comment_count=5,
        upload_date="20260101",
        duration=1800,
    )


def _transcript(item_id: str = "vidLONG") -> Transcript:
    return Transcript(
        video_id=item_id,
        cues=[
            TranscriptCue(cue_start_seconds=0.0, cue_end_seconds=5.0, text="intro"),
            TranscriptCue(cue_start_seconds=90.0, cue_end_seconds=95.0, text="the point"),
        ],
    )


def test_summarize_video_yields_five_snapped_timestamped_bullets() -> None:
    """A video summary is exactly 5 bullets, each snapped to a real cue + deep-linked.

    WHY: the headline feature. The model proposes start_seconds (incl. 88, a non-cue value
    it must NOT be trusted with verbatim); the code snaps each to the nearest real cue so
    every bullet's deep-link lands on a real moment (88 -> 90.0). A regression that emitted
    raw model timestamps, or fewer/more than 5 bullets, fails here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        raw = json.dumps(
            [
                {"text": "a", "start_seconds": 0},
                {"text": "b", "start_seconds": 88},  # must snap to 90.0
                {"text": "c", "start_seconds": 90},
                {"text": "d", "start_seconds": 0},
                {"text": "e", "start_seconds": 0},
                {"text": "f-overflow", "start_seconds": 0},  # 6th -> dropped (cap 5)
            ]
        )
        summary = summarize.summarize_video(_video_item(), _transcript(), summarizer=lambda prompt: raw)

    assert len(summary.bullets) == 5, "a video summary must be exactly 5 bullets"
    assert summary.bullets[1].start_seconds == 90.0, "start_seconds=88 must snap to the real cue 90.0"
    assert summary.bullets[1].deep_link == "https://www.youtube.com/watch?v=vidLONG&t=90s"
    assert [b.text for b in summary.bullets] == ["a", "b", "c", "d", "e"]


def test_summarize_video_no_transcript_returns_empty_never_crashes() -> None:
    """No transcript -> empty-bullet summary, no crash (a winner still renders a card).

    WHY: a missing transcript is a routine fetch outcome, not an error. Failing loud here
    (crash) would lose the whole digest for one bad caption fetch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        called = []
        summary = summarize.summarize_video(
            _video_item(), None, summarizer=lambda prompt: (called.append(1) or "[]")
        )
    assert summary.bullets == []
    assert called == [], "no transcript means the model is not even called"


def test_summarize_video_unparseable_verdict_degrades_to_empty() -> None:
    """An unparseable model verdict yields an empty summary, never raises."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        summary = summarize.summarize_video(_video_item(), _transcript(), summarizer=lambda prompt: "not json")
    assert summary.bullets == []


def test_summarize_user_override_is_sacred_and_skips_the_model() -> None:
    """A user-edited summary (override=1) is returned verbatim; the model is NOT called.

    WHY: a user correction must survive every re-run (mirrors classify's override). A
    regression that re-summarized over an override would silently discard the user's edit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        # Persist a user-override summary first.
        store.set_summary(
            "vidLONG",
            json.dumps([{"text": "user wrote this", "start_seconds": None, "deep_link": None}]),
            is_user_override=1,
        )

        def _must_not_run(prompt):  # noqa: ANN001
            raise AssertionError("override summary must not call the model")

        summary = summarize.summarize_video(_video_item(), _transcript(), summarizer=_must_not_run)

    assert summary.is_user_override == 1
    assert [b.text for b in summary.bullets] == ["user wrote this"]


def test_summarize_tweet_yields_timeless_bullets() -> None:
    """A tweet summary is 2-3 bullets with no timestamps/deep-links.

    WHY: tweets have no transcript, so their bullets carry no moment links. The cap keeps
    a chatty model bounded to 3.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tweet_item = RankableItem(
            item_external_id="123",
            title="a sharp original take about ai",
            channel_name="alice",
            creator_external_id="alice",
            view_count=5,
            like_count=10,
            comment_count=2,
            upload_date="20260101",
            card_url="https://x.com/alice/status/123",
        )
        raw = json.dumps([{"text": "point one"}, {"text": "point two"}, {"text": "point three"}, {"text": "four"}])
        summary = summarize.summarize_tweet(tweet_item, summarizer=lambda prompt: raw)

    assert 2 <= len(summary.bullets) <= 3, "a tweet summary is 2-3 bullets"
    assert all(b.start_seconds is None and b.deep_link is None for b in summary.bullets)
