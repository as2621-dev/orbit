"""DoD tests for VTT transcript fetch retaining cue timestamps (Phase 2 / Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
The whole point of this module is the design decision that VTT cue timestamps
SURVIVE the fetch (the reference flattens them away). So the tests assert the
intents the rest of the pipeline depends on:

  1. Cue survival: a cue at ``00:01:30.000`` must parse to ``cue_start_seconds ==
     90.0`` (the EXACT float, with the right text) — not merely "a list came
     back". This is the core product invariant: chapterize + deep-links trace
     back to these offsets. A regression that drops/zeroes offsets fails loudly.
  2. Tag stripping: inline ``<...>`` cue tags must not pollute cue text.
  3. Deep-link exactness: ``build_deep_link("abc", 90.0)`` must be byte-exact and
     truncate floats (90.7 -> ``t=90s``) — the headline feature's URL contract.
  4. Quick depth: ``depth="quick"`` fetches 0 transcripts and NEVER calls yt-dlp
     (the depth=0 gate — a regression here would burn the transcription budget).
  5. Happy fetch path: a fixture VTT flows through the real fetch function to a
     Transcript with intact cues, with the yt-dlp call AND the .vtt file read
     mocked at the boundary (no real yt-dlp, no network).

All external boundaries are mocked: ``lib.subproc.run_with_timeout`` is patched
and the produced ``.vtt`` read is patched, so no real yt-dlp runs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# Make ``scripts`` importable so ``from lib import transcribe``
# resolves regardless of the working directory.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import subproc, transcribe  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample.vtt"


def test_parse_vtt_cues_preserves_timestamps_as_exact_floats() -> None:
    """parse_vtt_cues must keep cue offsets as exact floats (the core invariant).

    WHY: this is design decision 4. The reference flattens VTT to plaintext and
    DELETES every offset; Orbit must do the opposite or the headline "jump to the
    moment" deep-link is impossible. We assert the EXACT floats (5.0 and 90.0) and
    the cue text — a regression that returns a non-empty list with wrong/zeroed
    offsets must fail here, not merely "a list was returned".
    """
    vtt_text = FIXTURE_PATH.read_text(encoding="utf-8")
    cues = transcribe.parse_vtt_cues(vtt_text)

    assert len(cues) == 2, "fixture has exactly two cues"

    # First cue: 00:00:05.000 -> 5.0 with known text.
    assert cues[0].cue_start_seconds == 5.0
    assert cues[0].cue_end_seconds == 8.0
    assert cues[0].text == "Welcome to the show"

    # The cue at 90.0 must exist with cue_start_seconds == 90.0 (the invariant).
    cue_at_ninety = next(
        (cue for cue in cues if cue.cue_start_seconds == 90.0), None
    )
    assert cue_at_ninety is not None, "the 00:01:30.000 cue must parse to 90.0"
    assert cue_at_ninety.cue_end_seconds == 94.0
    # Text must survive AND the inline <...> tags must be stripped clean.
    assert cue_at_ninety.text == "Now we get to the good part"


def test_parse_vtt_cues_strips_inline_tags() -> None:
    """Inline ``<...>`` cue tags (karaoke timing / styling) must be removed.

    WHY: yt-dlp auto-subs embed ``<00:00:01.000>`` and ``<c>`` tags inside cue
    text; if they leaked into the text, classify/embed and the rendered chapter
    label would be polluted with markup. We assert no ``<`` remains in any cue.
    """
    vtt_text = FIXTURE_PATH.read_text(encoding="utf-8")
    cues = transcribe.parse_vtt_cues(vtt_text)

    for cue in cues:
        assert "<" not in cue.text and ">" not in cue.text


def test_build_deep_link_is_byte_exact() -> None:
    """build_deep_link must produce the EXACT watch?v=ID&t=Ns string.

    WHY: this URL is the headline feature. The string is consumed verbatim by the
    renderer; any drift (wrong param order, missing ``s``) breaks every deep-link.
    """
    assert transcribe.build_deep_link("abc", 90.0) == "https://www.youtube.com/watch?v=abc&t=90s"


def test_build_deep_link_truncates_float_seconds() -> None:
    """A fractional cue offset must int-truncate (90.7 -> t=90s).

    WHY: cue offsets are floats (millisecond precision) but YouTube's ``t=`` is
    whole seconds. Truncation must be deterministic so the link lands on the cue.
    """
    assert transcribe.build_deep_link("abc", 90.7) == "https://www.youtube.com/watch?v=abc&t=90s"


def test_fetch_transcript_with_cues_quick_depth_returns_none_without_calling_yt_dlp() -> None:
    """depth='quick' must fetch 0 transcripts and NEVER invoke yt-dlp.

    WHY: the depth=0 gate is the per-run transcription budget's hard floor. A
    regression that ran yt-dlp anyway would burn time/network on every quick run.
    We patch the subprocess boundary and assert it is NOT called and None comes
    back.
    """
    with patch.object(transcribe.subproc, "run_with_timeout") as mock_run:
        result = transcribe.fetch_transcript_with_cues("vid000001", depth="quick")

    assert result is None
    mock_run.assert_not_called()


def test_fetch_transcript_with_cues_happy_path_yields_intact_cues() -> None:
    """A fixture VTT must flow through the fetch to a Transcript with intact cues.

    WHY: this is the end-to-end intent of Stage 1b — fetch then parse with
    timestamps surviving. We mock BOTH boundaries (the yt-dlp subprocess succeeds,
    and the produced .vtt read returns the fixture text) so no real yt-dlp/network
    is touched, then assert the resulting Transcript carries the 5.0 and 90.0 cues
    and a working plain_text (which must NOT have destroyed the cues).
    """
    fixture_vtt = FIXTURE_PATH.read_text(encoding="utf-8")
    fake_result = subproc.SubprocResult(returncode=0, stdout="", stderr="")

    with patch.object(transcribe.subproc, "run_with_timeout", return_value=fake_result):
        with patch.object(transcribe, "_read_produced_vtt", return_value=fixture_vtt):
            transcript = transcribe.fetch_transcript_with_cues("vid000001", depth="default")

    assert transcript is not None
    assert transcript.video_id == "vid000001"
    assert len(transcript.cues) == 2
    assert transcript.cues[0].cue_start_seconds == 5.0
    assert any(cue.cue_start_seconds == 90.0 for cue in transcript.cues)
    # plain_text is a derived view — the cues must still be intact afterwards.
    assert "Welcome to the show" in transcript.plain_text()
    assert len(transcript.cues) == 2, "plain_text must not destroy the cues"


def test_fetch_transcript_with_cues_no_captions_returns_none() -> None:
    """yt-dlp producing no .vtt (no captions) must return None, not crash.

    WHY: yt-dlp exits 0 with no file for a video that genuinely lacks captions.
    That is a no-captions case (the item is kept, just un-chapterizable), not a
    fatal error — fail gracefully (still loud via a logged warning), return None.
    """
    fake_result = subproc.SubprocResult(returncode=0, stdout="", stderr="")

    with patch.object(transcribe.subproc, "run_with_timeout", return_value=fake_result):
        with patch.object(transcribe, "_read_produced_vtt", return_value=None):
            transcript = transcribe.fetch_transcript_with_cues("vid000001", depth="default")

    assert transcript is None


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
