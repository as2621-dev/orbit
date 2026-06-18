"""DoD tests for chapterizing long-form videos (Phase 2 / Sub-phase 4).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Chapterize is Stage 3 — it turns a long-form video into timestamped chapters that
power the headline feature (jump to the exact moment). So the tests assert the
intents the product depends on:

  1. Creator chapters verbatim + deep-link survival: when a creator supplied chapters
     we use them as-is, and each deep-link's ``&t=Ns`` equals the chapter's
     ``start_time`` — so the link drops the user into the right moment. The LLM is NOT
     called on this deterministic path (Rule 5).
  2. Short-item rule: a video at/under the 20-min threshold gets NO chapters and the
     segmenter is never called (short stays short — design decision 7's boundary).
  3. Timestamp traces to a real cue: for the LLM-segmentation path, every returned
     chapter's ``start_seconds`` is one of the REAL transcript cue offsets (never an
     invented number), and the deep-link is built from that cue offset. This encodes
     the Phase-level invariant: a deep-link's ``t=`` equals a real cue offset.
  4. Defensive: an unparseable segmenter verdict falls back without crashing.

All external boundaries are mocked: the segmentation LLM boundary is injected per
call (there is no live model in this build env). No store / yt-dlp is touched —
chapterize is pure given an Upload + Transcript.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make ``skills/orbit/scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_classify.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import chapterize  # noqa: E402
from lib.transcribe import Transcript, TranscriptCue, build_deep_link  # noqa: E402
from lib.youtube_yt import Upload  # noqa: E402


def _upload(video_id: str = "vid_long_001", *, duration: int | None, chapters=None) -> Upload:
    """A minimal long-form-shaped Upload carrying only the fields chapterize reads."""
    return Upload(
        video_id=video_id,
        title=f"Long talk {video_id}",
        description="A long-form episode.",
        upload_date="20260101",
        view_count=None,
        like_count=None,
        comment_count=None,
        duration=duration,
        channel_name="Some Channel",
        chapters=chapters,
    )


def test_creator_chapters_used_verbatim_with_correct_deep_links() -> None:
    """Creator chapters are used verbatim and each deep-link ends in the right &t=Ns.

    WHY: when a creator already chaptered their video, we must honor those boundaries
    exactly — and the deep-link MUST carry that chapter's ``start_time`` so the link
    lands on the right moment (deep-link survival). This is a deterministic path
    (Rule 5): the LLM segmenter must NOT be called. A regression that re-segmented or
    mis-built the link would break the headline feature for chaptered videos.
    """
    creator_chapters = [
        {"title": "Intro", "start_time": 0, "end_time": 300},
        {"title": "Topic A", "start_time": 300, "end_time": 1800},
    ]
    upload = _upload(duration=1800, chapters=creator_chapters)
    segmenter_must_not_be_called = MagicMock(
        side_effect=AssertionError("LLM segmenter must not be called on the creator-chapter path")
    )

    chapters = chapterize.chapterize_episode(
        upload, transcript=None, segmenter=segmenter_must_not_be_called
    )

    segmenter_must_not_be_called.assert_not_called()
    assert len(chapters) == 2
    assert chapters[0].title == "Intro"
    assert chapters[0].start_seconds == 0.0
    assert chapters[0].deep_link.endswith("&t=0s")
    assert chapters[1].title == "Topic A"
    assert chapters[1].start_seconds == 300.0
    assert chapters[1].deep_link.endswith("&t=300s")
    # Full URL survival, not just the suffix.
    assert chapters[1].deep_link == build_deep_link("vid_long_001", 300.0)


def test_short_item_returns_no_chapters_and_never_calls_segmenter() -> None:
    """A video under the 20-min threshold gets NO chapters and the LLM isn't called.

    WHY: design decision 7's boundary — short items are single units with no
    sub-navigation. duration=600s (10 min) is under the 1200s threshold, so the result
    must be ``[]`` and the segmenter must never run (no wasted model call, no chapters
    on a short clip). A regression that chaptered short items would clutter the feed.
    """
    upload = _upload(duration=600, chapters=None)
    segmenter_must_not_be_called = MagicMock(
        side_effect=AssertionError("LLM segmenter must not be called for a short item")
    )

    chapters = chapterize.chapterize_episode(
        upload, transcript=None, segmenter=segmenter_must_not_be_called
    )

    assert chapters == []
    segmenter_must_not_be_called.assert_not_called()


def test_segmented_chapters_trace_to_real_cue_offsets() -> None:
    """LLM-segmented chapters carry a REAL cue offset, never an invented timestamp.

    WHY: the Phase-level invariant — a deep-link's ``t=`` must equal a real transcript
    cue offset (design decision 4 + 7). The model may propose an approximate timestamp;
    we SNAP it to the nearest real cue offset so the link is always anchored to a moment
    that exists in the transcript. Here the model returns 125 and 590 (off by a few
    seconds); they must snap to the real cues 120.0 and 600.0, and the deep-link must be
    built from those snapped offsets. A regression that trusted the model's raw number
    would point the user at a moment that was never in the transcript.
    """
    cues = [
        TranscriptCue(cue_start_seconds=0.0, cue_end_seconds=10.0, text="welcome"),
        TranscriptCue(cue_start_seconds=120.0, cue_end_seconds=130.0, text="topic a"),
        TranscriptCue(cue_start_seconds=600.0, cue_end_seconds=610.0, text="topic b"),
    ]
    transcript = Transcript(video_id="vid_long_001", cues=cues)
    upload = _upload(duration=1800, chapters=None)

    # Model proposes near-but-not-exact offsets; the snap must map them to real cues.
    raw_segments = (
        '[{"title": "Intro", "start_seconds": 0},'
        ' {"title": "Topic A", "start_seconds": 125},'
        ' {"title": "Topic B", "start_seconds": 590}]'
    )
    segmenter = MagicMock(return_value=raw_segments)

    chapters = chapterize.chapterize_episode(upload, transcript=transcript, segmenter=segmenter)

    segmenter.assert_called_once()
    real_cue_offsets = {0.0, 120.0, 600.0}
    assert len(chapters) == 3
    for chapter in chapters:
        # The invariant: every start_seconds is a REAL cue offset, not the raw number.
        assert chapter.start_seconds in real_cue_offsets, (
            f"chapter start_seconds {chapter.start_seconds} did not trace back to a real cue"
        )
        # And the deep-link is built from that real cue offset.
        assert chapter.deep_link == build_deep_link("vid_long_001", chapter.start_seconds)
    # Specifically: 125 -> 120.0, 590 -> 600.0 (nearest real cue).
    assert chapters[1].start_seconds == 120.0
    assert chapters[1].deep_link.endswith("&t=120s")
    assert chapters[2].start_seconds == 600.0
    assert chapters[2].deep_link.endswith("&t=600s")


def test_unparseable_verdict_falls_back_without_crashing() -> None:
    """An unparseable segmenter verdict falls back to a single chapter, never crashes.

    WHY: the chapterize run processes many long-form videos; one bad model line must
    not crash the whole run (Rule 12). We feed junk and assert a single chapter anchored
    at the FIRST real cue is returned (never an invented timestamp), not an exception.
    """
    cues = [
        TranscriptCue(cue_start_seconds=3.0, cue_end_seconds=8.0, text="hello"),
        TranscriptCue(cue_start_seconds=200.0, cue_end_seconds=205.0, text="later"),
    ]
    transcript = Transcript(video_id="vid_long_001", cues=cues)
    upload = _upload(duration=1800, chapters=None)
    segmenter = MagicMock(return_value="this is not json at all")

    chapters = chapterize.chapterize_episode(upload, transcript=transcript, segmenter=segmenter)

    assert len(chapters) == 1
    # Anchored at the first real cue offset (3.0), never an invented value.
    assert chapters[0].start_seconds == 3.0
    assert chapters[0].deep_link == build_deep_link("vid_long_001", 3.0)


def test_long_form_no_cues_returns_empty_without_inventing_timestamp() -> None:
    """A long-form video with no transcript cues yields no chapters (never invents one).

    WHY: we never fabricate a timestamp. With no cues to anchor to, there is nothing to
    chapter — the item is kept without chapters rather than getting a guessed t=0 link.
    """
    upload = _upload(duration=1800, chapters=None)
    segmenter_must_not_be_called = MagicMock(
        side_effect=AssertionError("segmenter must not run when there are no cues")
    )

    # Empty transcript and None transcript both mean "no cues".
    assert chapterize.chapterize_episode(
        upload, transcript=Transcript("vid_long_001", []), segmenter=segmenter_must_not_be_called
    ) == []
    assert chapterize.chapterize_episode(
        upload, transcript=None, segmenter=segmenter_must_not_be_called
    ) == []
    segmenter_must_not_be_called.assert_not_called()


def test_default_segmenter_fails_loud() -> None:
    """The default segmentation boundary must raise NotImplementedError — no faked segments.

    WHY: there is no live LLM in this build env. A default that fabricated segments would
    silently chapterize on garbage. Fail loud (Rule 12): the default must raise so a
    missing runtime wiring is impossible to ignore.
    """
    raised = None
    try:
        chapterize._default_chapter_segmenter("any prompt")
    except NotImplementedError as exc:
        raised = exc

    assert raised is not None, "the default chapter segmenter must raise NotImplementedError"


def _run_all_standalone() -> int:
    """Run every ``test_*`` function in this module without pytest. Returns exit code."""
    test_functions = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for test_function in test_functions:
        try:
            test_function()
            print(f"PASS {test_function.__name__}")
        except Exception as exc:  # noqa: BLE001 — standalone runner surfaces any failure
            failures.append(f"FAIL {test_function.__name__}: {exc!r}")
            print(failures[-1])
    print(f"\n{len(test_functions) - len(failures)}/{len(test_functions)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all_standalone())
