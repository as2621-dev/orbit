"""DoD tests for delta detection of new uploads (Phase 2 / Sub-phase 1).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:
delta detection is the heart of Stage 1a — its whole job is to surface ONLY uploads
the user has not already been shown. So the tests assert the intents the rest of the
pipeline depends on:

  1. No-resurface: a video_id already in ``seen`` must NOT come back as new (the delta
     invariant — a regression here re-shows old uploads every run, the core failure).
  2. Empty channel: no uploads -> ``[]`` AND a ``delta_fetch_completed`` log with
     ``count=0`` (the run must still announce completion, not die quietly).
  3. Defensive parse: a malformed NDJSON line is skipped, not fatal (one bad line must
     not lose the whole feed).
  4. Loud failure: a yt-dlp timeout raises ``YouTubeFetchError`` (fail loud, Rule 12),
     never a silent empty list.

All external boundaries are mocked: ``lib.subproc.run_with_timeout`` is patched (no
real yt-dlp, no network), and the store is pointed at a temp DB via ``ORBIT_DB_PATH``
+ ``store._db_override`` (no real ``~/.local/share`` write). Mirrors the temp-DB setup
in ``tests/test_youtube_yt.py``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# Make ``skills/orbit/scripts`` importable so ``import store`` and
# ``from lib import youtube_yt`` resolve regardless of the working directory.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths, subproc, youtube_yt  # noqa: E402

# Five upload video_ids the mocked yt-dlp listing returns, in feed order. The delta
# test seeds the first two and asserts EXACTLY the remaining three come back as new.
FIXTURE_VIDEO_IDS = ["vid000001", "vid000002", "vid000003", "vid000004", "vid000005"]
SEEDED_VIDEO_IDS = ["vid000001", "vid000002"]
EXPECTED_NEW_VIDEO_IDS = ["vid000003", "vid000004", "vid000005"]


def _fresh_store(tmp_dir: Path) -> int:
    """Point the store at a temp DB, init it, and insert one youtube source.

    Returns the inserted ``source_id`` (used as the ``source`` dict's id below).
    """
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()
    return store.upsert_source(
        platform="youtube",
        external_id="UCtestchannel000000001",
        display_name="Test Channel",
    )


def _upload_ndjson(video_ids: list[str]) -> str:
    """Build NDJSON stdout (one yt-dlp --dump-json line per video_id)."""
    lines = []
    for index, video_id in enumerate(video_ids):
        lines.append(
            json.dumps(
                {
                    "id": video_id,
                    "title": f"Title {video_id}",
                    "description": f"Description {video_id}",
                    "upload_date": "20260101",
                    "view_count": 100 + index,
                    "like_count": 10 + index,
                    "comment_count": index,
                    "duration": 300 + index,
                    "channel": "Test Channel",
                }
            )
        )
    return "\n".join(lines) + "\n"


def _source_dict(source_id: int) -> dict:
    """A source row dict matching store.list_sources shape for the channel above."""
    return {
        "source_id": source_id,
        "external_id": "UCtestchannel000000001",
        "display_name": "Test Channel",
        "platform": "youtube",
    }


def test_fetch_new_uploads_returns_only_unseen_video_ids() -> None:
    """fetch_new_uploads must return EXACTLY the uploads not already in ``seen``.

    WHY: this is the delta invariant. The listing returns the channel's recent uploads
    on every run; without the ``seen`` diff, Orbit would re-show every old video each
    run. We seed 2 of the 5 listed ids as seen and assert the SPECIFIC remaining 3 come
    back — asserting the exact ids (not just ``len == 3``) so a wrong-set bug (e.g.
    returning the seen ones, or off-by-one filtering) fails loudly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store(Path(tmp))
        for seeded_video_id in SEEDED_VIDEO_IDS:
            store.mark_seen(source_id, seeded_video_id)

        fake_result = subproc.SubprocResult(
            returncode=0, stdout=_upload_ndjson(FIXTURE_VIDEO_IDS), stderr=""
        )
        with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
            new_uploads = youtube_yt.fetch_new_uploads(_source_dict(source_id), depth="default")

    returned_video_ids = [upload.video_id for upload in new_uploads]
    assert returned_video_ids == EXPECTED_NEW_VIDEO_IDS
    # The seeded (already-seen) ids must NOT resurface — the core no-resurface intent.
    assert not any(video_id in SEEDED_VIDEO_IDS for video_id in returned_video_ids)


def test_fetch_new_uploads_parses_full_metadata_for_handoff() -> None:
    """An Upload must carry the metadata Sub-phases 2/4 depend on (esp. duration).

    WHY: Sub-phase 4 chapterizes using ``Upload.duration`` (long-form = > 1200s). If the
    parse drops duration or the counts, the downstream stages silently misbehave. We
    assert the field-level shape on a fresh (unseen) upload to lock the contract.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store(Path(tmp))
        fake_result = subproc.SubprocResult(
            returncode=0, stdout=_upload_ndjson(["vid000003"]), stderr=""
        )
        with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
            new_uploads = youtube_yt.fetch_new_uploads(_source_dict(source_id), depth="default")

    assert len(new_uploads) == 1
    upload = new_uploads[0]
    assert upload.video_id == "vid000003"
    assert upload.title == "Title vid000003"
    assert upload.description == "Description vid000003"
    assert upload.upload_date == "20260101"
    assert upload.duration == 300
    assert upload.view_count == 100
    assert upload.channel_name == "Test Channel"


def test_fetch_new_uploads_empty_channel_returns_empty_and_logs_count_zero() -> None:
    """An empty listing must return ``[]`` AND log delta_fetch_completed count=0.

    WHY: a channel with no new uploads is the common steady-state. The run must still
    announce completion with count=0 (observability / fail-loud), not return quietly in
    a way that's indistinguishable from a crash. We capture stdout (the JSON log stream)
    and assert the completion event with count=0 is present.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store(Path(tmp))
        fake_result = subproc.SubprocResult(returncode=0, stdout="", stderr="")
        captured = io.StringIO()
        with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
            with redirect_stdout(captured):
                new_uploads = youtube_yt.fetch_new_uploads(_source_dict(source_id), depth="default")

    assert new_uploads == []
    completion_events = [
        json.loads(line)
        for line in captured.getvalue().splitlines()
        if line.strip() and json.loads(line).get("event") == "delta_fetch_completed"
    ]
    assert len(completion_events) == 1
    assert completion_events[0]["count"] == 0


def test_fetch_new_uploads_skips_malformed_line_without_losing_feed() -> None:
    """A malformed NDJSON line must be skipped, not crash the whole listing.

    WHY: yt-dlp output is a stream; one corrupt/partial line must not lose every other
    upload (defensive parse, mirrors the subscriptions parser). We inject a junk line
    between two valid ones and assert the two valid uploads still come back.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store(Path(tmp))
        good_ndjson = _upload_ndjson(["vid000003"]).strip()
        other_good = _upload_ndjson(["vid000004"]).strip()
        stdout_with_junk = good_ndjson + "\n" + "{not valid json" + "\n" + other_good + "\n"
        fake_result = subproc.SubprocResult(returncode=0, stdout=stdout_with_junk, stderr="")
        with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
            new_uploads = youtube_yt.fetch_new_uploads(_source_dict(source_id), depth="default")

    returned_video_ids = [upload.video_id for upload in new_uploads]
    assert returned_video_ids == ["vid000003", "vid000004"]


def test_fetch_new_uploads_raises_loud_error_on_timeout() -> None:
    """A yt-dlp timeout must raise YouTubeFetchError, never a silent empty list.

    WHY: fail loud (Rule 12). A network/yt-dlp hang must not be swallowed into ``[]``
    that looks like "no new uploads" — that would silently stall the pipeline. We assert
    the typed error is raised and that its message is actionable (points at the README).
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_id = _fresh_store(Path(tmp))
        with patch.object(
            youtube_yt.subproc,
            "run_with_timeout",
            side_effect=subproc.SubprocTimeout("yt-dlp timed out"),
        ):
            raised = None
            try:
                youtube_yt.fetch_new_uploads(_source_dict(source_id), depth="default")
            except youtube_yt.YouTubeFetchError as exc:
                raised = exc

    assert raised is not None, "a timeout must raise YouTubeFetchError, not return []"
    assert "readme" in str(raised).lower() or "§8.6" in str(raised)


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
