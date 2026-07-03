"""DoD test for the YouTube Stage 1-2 producer wired into orbit.py (run_stage1_build_youtube_items).

Per Rule 9, this encodes WHY the producer matters, not merely that it runs. Before this
wiring a daily ``/orbit`` run was X-only: the YouTube delta/classify/chapterize building
blocks existed and were unit-tested, but no orbit.py stage assembled them, so a
YouTube-only user got an empty digest. These tests pin the now-load-bearing contracts of
the assembled producer:

  * a new upload is classified (two-axis) and reaches the unified stream as a RankableItem;
  * a long-form upload WITHOUT creator chapters is chapterized via the injected segmenter,
    with every chapter offset snapped to a REAL transcript cue (the deep-link invariant);
  * the per-run transcript budget is honoured — ``quick`` depth (limit 0) fetches NONE;
  * each built upload is marked ``seen`` so the delta engine never resurfaces it next run.

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
    title: str = "A talk",
) -> Upload:
    """Build an Upload (mocks one yt-dlp delta entry).

    ``title`` defaults to ``"A talk"`` (unchanged for existing callers); a distinct title
    lets a test route a mocked classifier failure to one specific upload, since the title
    is what reaches the classify prompt body.
    """
    return Upload(
        video_id=video_id,
        title=title,
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


def test_youtube_producer_classifies_chapterizes_and_marks_seen(tmp_path: Path) -> None:
    """The producer builds classified+chapterized RankableItems and marks each upload seen.

    WHY: this is the wiring the milestone needs — a YouTube-only user must get real items.
    We feed one long-form upload (no creator chapters) and one short upload through the
    producer with mocked delta/transcript/LLM boundaries, then assert: both become
    RankableItems; the long-form item carries chapters whose deep-link points at a REAL
    snapped cue offset (t=90s); and BOTH video ids are now in ``seen`` so next run's delta
    skips them. A regression that dropped classify, chapters, or the seen-mark fails here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store_with_channel(Path(tmp))

        long_upload = _upload("vidLONG", duration=1800)  # > 1200s, no creator chapters
        short_upload = _upload("vidSHORT", duration=300)  # short -> no chapters

        def _mock_delta(source, depth):  # noqa: ANN001 — test stub
            return [long_upload, short_upload]

        # The transcript anchors chapters at REAL cue offsets 0.0 and 90.0.
        transcript = Transcript(
            video_id="vidLONG",
            cues=[
                TranscriptCue(cue_start_seconds=0.0, cue_end_seconds=5.0, text="intro"),
                TranscriptCue(cue_start_seconds=90.0, cue_end_seconds=95.0, text="the point"),
            ],
        )

        def _mock_transcript_fetcher(video_id, depth):  # noqa: ANN001 — test stub
            return transcript

        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        items = orbit.run_stage1_build_youtube_items(
            config,
            depth="default",
            upload_delta=_mock_delta,
            transcript_fetcher=_mock_transcript_fetcher,
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
            segmenter=lambda prompt: '[{"title": "Intro", "start_seconds": 0}, {"title": "Point", "start_seconds": 88}]',
        )

        seen_ids = store.get_seen_ids(source_id)

    # Both uploads reach the unified stream (nothing dropped).
    assert [item.item_external_id for item in items] == ["vidLONG", "vidSHORT"]

    # The long-form item was classified (two-axis verdict carried through).
    long_item = items[0]
    assert long_item.classification is not None
    assert long_item.classification.axis_a_signal == 1

    # It was chapterized, and the model's start_seconds=88 SNAPPED to the real cue at 90.0
    # so the deep-link traces back to a real cue (the headline invariant).
    assert long_item.chapters, "long-form upload must be chapterized"
    deep_links = [chapter.deep_link for chapter in long_item.chapters]
    assert "https://www.youtube.com/watch?v=vidLONG&t=90s" in deep_links

    # The short upload stays short — no chapters.
    assert items[1].chapters == []

    # Both uploads are now marked seen so the delta engine never resurfaces them.
    assert seen_ids == {"vidLONG", "vidSHORT"}


def test_youtube_producer_quick_depth_fetches_no_transcripts(tmp_path: Path) -> None:
    """``quick`` depth (transcript budget 0) never calls the transcript fetcher.

    WHY: the per-run transcript budget is a cost guarantee — a quick run must not transcribe
    the feed. We assert the injected fetcher is NEVER invoked for a long-form upload at
    ``quick`` depth, and the item still renders (without chapters). A regression that
    ignored the budget would silently run expensive transcription on every quick run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_channel(Path(tmp))

        def _mock_delta(source, depth):  # noqa: ANN001 — test stub
            return [_upload("vidLONG", duration=1800)]

        fetch_calls: list[str] = []

        def _spy_transcript_fetcher(video_id, depth):  # noqa: ANN001 — test stub
            fetch_calls.append(video_id)
            return None

        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        items = orbit.run_stage1_build_youtube_items(
            config,
            depth="quick",
            upload_delta=_mock_delta,
            transcript_fetcher=_spy_transcript_fetcher,
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
            segmenter=lambda prompt: "[]",
        )

    assert fetch_calls == [], "quick depth (budget 0) must not fetch any transcript"
    assert [item.item_external_id for item in items] == ["vidLONG"]
    assert items[0].chapters == [], "with no transcript fetched there are no cues to chapter"


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


def test_youtube_producer_skips_item_when_classify_times_out(capsys) -> None:  # noqa: ANN001
    """A transient classify LLM timeout skips ONE upload, never aborts the digest.

    WHY (Rule 9): a single ``claude -p`` timeout raised by ``classify.classify_item`` for
    one upload must degrade to skipping that item — the run keeps going and every other
    upload still reaches the digest. Before the per-item try/except a lone timeout aborted
    the ENTIRE pipeline (a YouTube-only user got nothing). We inject a classifier that
    raises the real ``LlmCliError`` for one upload (routed by its distinct title) and
    returns a valid verdict otherwise, then assert: the producer returns WITHOUT raising;
    the failing upload is ABSENT from the returned items; the healthy upload is present;
    the ``youtube_stage1_item_classify_skipped`` warning was logged; and — the correctness
    crux — the skipped upload was NOT marked seen, so the delta engine reconsiders it next
    run. Reverting the try/except re-raises here and fails the test.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_channel(Path(tmp))

        good_upload = _upload("vidGOOD", duration=300, title="A good talk")
        doomed_upload = _upload("vidDOOMED", duration=300, title="TIMEOUT this one")

        def _mock_delta(source, depth):  # noqa: ANN001 — test stub
            return [good_upload, doomed_upload]

        def _flaky_classifier(prompt: str) -> str:
            # Route the failure by the upload's title, which reaches the prompt body — the
            # doomed upload times out, the good one classifies cleanly.
            if "TIMEOUT this one" in prompt:
                raise orbit.LlmCliError("claude -p timed out")
            return '{"axis_a_signal": 1, "axis_b_on_topic": 1}'

        seen_marks: list[tuple[int, str]] = []

        def _spy_mark_seen(source_id, video_id):  # noqa: ANN001 — test stub
            seen_marks.append((source_id, video_id))

        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        items = orbit.run_stage1_build_youtube_items(
            config,
            depth="default",
            upload_delta=_mock_delta,
            transcript_fetcher=lambda video_id, depth: None,  # noqa: ANN001,ARG005
            llm_classifier=_flaky_classifier,
            segmenter=lambda prompt: "[]",
            mark_seen=_spy_mark_seen,
        )

    # The run survived the timeout and dropped ONLY the doomed upload.
    built_ids = [item.item_external_id for item in items]
    assert built_ids == ["vidGOOD"], "the timed-out upload must be skipped, the healthy one kept"
    assert "vidDOOMED" not in built_ids

    # The skip was surfaced (Rule 12), not swallowed.
    assert "youtube_stage1_item_classify_skipped" in capsys.readouterr().out

    # The correctness crux: a skipped upload is left UNSEEN so it is reconsidered next run.
    marked_ids = {video_id for _source_id, video_id in seen_marks}
    assert "vidDOOMED" not in marked_ids, "a skipped upload must NOT be marked seen"
    assert "vidGOOD" in marked_ids, "a successfully built upload is still marked seen"


def test_youtube_producer_degrades_chapters_when_chapterize_times_out(capsys) -> None:  # noqa: ANN001
    """A transient chapterize LLM timeout degrades to NO chapters, still builds the item.

    WHY (Rule 9): chapter segmentation is best-effort. A ``claude -p`` timeout raised by
    ``chapterize_episode`` for a long-form upload must degrade to an empty chapter list —
    the tile still renders and the run never aborts. Before the try/except this timeout
    propagated and killed the whole digest. We inject a segmenter that raises the real
    ``LlmCliError`` while the classifier stays healthy, then assert: the item is STILL
    returned (classified), its ``chapters`` are empty, the run does not raise, and the
    ``youtube_stage1_item_chapterize_degraded`` warning was logged. Reverting the
    try/except re-raises here and fails the test.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_channel(Path(tmp))

        long_upload = _upload("vidLONG", duration=1800)  # long-form, no creator chapters

        def _mock_delta(source, depth):  # noqa: ANN001 — test stub
            return [long_upload]

        transcript = Transcript(
            video_id="vidLONG",
            cues=[TranscriptCue(cue_start_seconds=0.0, cue_end_seconds=5.0, text="intro")],
        )

        def _mock_transcript_fetcher(video_id, depth):  # noqa: ANN001 — test stub
            return transcript

        def _timeout_segmenter(prompt: str) -> str:
            raise orbit.LlmCliError("claude -p timed out during segmentation")

        config = OrbitConfig(creator_weights={"UC_chan": 1.0}, interests=["ai"])

        items = orbit.run_stage1_build_youtube_items(
            config,
            depth="default",
            upload_delta=_mock_delta,
            transcript_fetcher=_mock_transcript_fetcher,
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
            segmenter=_timeout_segmenter,
        )

    # The item survived the chapterize timeout — built and classified, just chapter-less.
    assert [item.item_external_id for item in items] == ["vidLONG"], "the item must still build"
    assert items[0].classification is not None
    assert items[0].chapters == [], "a chapterize timeout degrades to no chapters"

    # The degrade was surfaced (Rule 12), not swallowed.
    assert "youtube_stage1_item_chapterize_degraded" in capsys.readouterr().out
