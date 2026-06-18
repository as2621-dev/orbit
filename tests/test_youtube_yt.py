"""DoD tests for the YouTube subscriptions loader (Sub-phase 3).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:
Stage 0 is the foundation the whole pipeline stands on, so the tests assert the
intents that future phases depend on — (1) the subscription list is parsed
faithfully into channel ids, (2) those channels actually land in the ``sources``
table so the Phase-2 delta engine has something to diff, and (3) an auth failure is
LOUD and ACTIONABLE rather than a silent death or a raw stack trace.

All external boundaries are mocked: ``lib.subproc.run_with_timeout`` is patched (no
real yt-dlp, no network), and the store is pointed at a temp DB via ``ORBIT_DB_PATH``
+ ``store._db_override`` (no real ``~/.local/share`` write). Mirrors the temp-DB
setup in ``tests/test_store.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Make ``skills/orbit/scripts`` importable so ``import store`` and
# ``from lib import youtube_yt`` resolve regardless of the working directory.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths, subproc, youtube_yt  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "youtube_subs.jsonl"

# The exact channel ids the fixture encodes, in feed order. The test asserts these
# precisely so a parser regression (wrong key precedence, dropped lines) fails loudly.
EXPECTED_CHANNEL_IDS = [
    "UCaaaa0000000000000001",
    "UCbbbb0000000000000002",
    "UCcccc0000000000000003",
    "UCdddd0000000000000004",
]


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def test_load_youtube_subscriptions_parses_every_channel_id_from_feed() -> None:
    """load_youtube_subscriptions must parse each channel id from yt-dlp NDJSON.

    WHY: Stage 0's only job is to turn the authenticated subscriptions feed into a
    faithful channel list. If parsing drops or mangles ids, the delta engine would
    silently watch the wrong (or fewer) channels — the user would miss uploads with
    no error. So we assert the exact ids and count, including the entries that lack a
    ``channel_id`` key (must fall back to ``id``) to lock the defensive key precedence.
    """
    fixture_stdout = FIXTURE_PATH.read_text(encoding="utf-8")
    fake_result = subproc.SubprocResult(returncode=0, stdout=fixture_stdout, stderr="")

    with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
        subscriptions = youtube_yt.load_youtube_subscriptions("chrome")

    assert [sub.channel_id for sub in subscriptions] == EXPECTED_CHANNEL_IDS
    assert len(subscriptions) == len(EXPECTED_CHANNEL_IDS)
    # Display name must resolve via channel/uploader/title fallback, never blank.
    assert all(sub.display_name for sub in subscriptions)


def test_persist_subscriptions_writes_channels_into_sources_table() -> None:
    """persist_subscriptions must land each channel in ``sources`` as platform=youtube.

    WHY: Stage 0 must persist subs into the ``sources`` table so the Phase-2 delta
    engine has a concrete set of channels to diff for new uploads. A loader that parsed
    correctly but never persisted would leave the pipeline with nothing to fetch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        subscriptions = [
            youtube_yt.Subscription(channel_id=channel_id, display_name=f"Name {channel_id}")
            for channel_id in EXPECTED_CHANNEL_IDS
        ]

        persisted_count = youtube_yt.persist_subscriptions(subscriptions)
        assert persisted_count == len(EXPECTED_CHANNEL_IDS)

        youtube_sources = store.list_sources(platform="youtube")
        stored_channel_ids = {source["external_id"] for source in youtube_sources}
        assert stored_channel_ids == set(EXPECTED_CHANNEL_IDS)
        # Every persisted row carries a refresh timestamp so the weekly-cache check
        # (Sub-phase 4) can reason about freshness; missing it would break the skip.
        assert all(source["last_refreshed_at"] for source in youtube_sources)


def test_load_youtube_subscriptions_raises_loud_actionable_error_on_auth_failure() -> None:
    """An auth failure must raise a clear YouTubeAuthError, never crash or die silently.

    WHY: auth failure (no cookies / expired session) is the single most common Stage-0
    failure, and conventions.md §Error handling mandates it be loud and actionable —
    the user must be told to log into their browser and pointed at README §8.6, not
    handed a raw stack trace or a silent empty list. We assert both the exception TYPE
    (a clean, typed error — not a generic crash leaking the raw stderr as its message)
    and that its message carries actionable guidance.
    """
    auth_stderr = (
        "ERROR: could not find chrome cookies database / no cookies found, please sign in"
    )
    fake_result = subproc.SubprocResult(returncode=1, stdout="", stderr=auth_stderr)

    with patch.object(youtube_yt.subproc, "run_with_timeout", return_value=fake_result):
        try:
            youtube_yt.load_youtube_subscriptions("chrome")
            raised = None
        except youtube_yt.YouTubeAuthError as exc:
            raised = exc

    assert raised is not None, "auth failure must raise YouTubeAuthError, not pass silently"
    message_lower = str(raised).lower()
    # Actionable: tells the user to log/sign in AND points at the README troubleshooting.
    assert ("log into" in message_lower) or ("sign in" in message_lower) or ("log in" in message_lower)
    assert ("readme" in message_lower) or ("§8.6" in str(raised))
    # The raw stderr must NOT be surfaced verbatim as the exception message — a clean
    # typed error, not a regurgitated yt-dlp crash dump.
    assert "ERROR: could not find chrome cookies database" not in str(raised)


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
