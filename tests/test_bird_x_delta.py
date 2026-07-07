"""DoD tests for the X SearchTimeline delta (Phase 4, Sub-phase 3 / Stage 1).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:

  1. ``test_delta_returns_only_unseen_and_marks_them`` — the delta engine is the entire
     point of Stage 1. If a tweet already in ``seen`` re-appeared as new, the user would
     see the same tweet in the digest every single day (resurfacing spam); if a genuinely
     new tweet were dropped, the digest would silently omit fresh content. This test
     fails if delta-filtering OR mark_seen-on-success is wrong.
  2. ``test_rotation_fairness_grows_coverage_across_days`` — the resolved Q5 fairness
     policy. With a follow list LARGER than the per-run budget, a static (non-rotating)
     selection would permanently starve the handles outside the first window — a
     high-follow user would never see most of who they follow. This test asserts the
     UNION of deep-pulled handles STRICTLY GROWS from day N to day N+1, which FAILS if
     the rotation offset logic is wrong (e.g. always selecting the same window).
  3. ``test_inter_request_delay_is_invoked_between_handles`` — pacing is the ToS-gray
     guardrail (reference/integrations.md §2). If the inter-handle delay were not
     invoked, the loop would fan out all handles instantly and risk rate-limiting /
     flagging the account. This test asserts the injected sleeper is called once per
     handle past the first.

The subprocess boundary is mocked (``_pull_handle_tweets``'s underlying
``subproc.run_with_timeout`` patched to return canned ``SubprocResult``s keyed on which
handle was queried) — NO live X call, NO real cookies, and the sleeper is injected as a
recorder so NO real sleeping happens. The store is pointed at a temp DB via
``ORBIT_DB_PATH`` + ``store._db_override``, mirroring tests/test_bird_x_following.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

# Make ``scripts`` importable so ``import store`` and ``from lib import ...``
# resolve regardless of the working directory. Mirrors tests/test_bird_x_following.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import bird_x, paths, subproc  # noqa: E402


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def _seed_x_sources(handles: List[str]) -> Dict[str, int]:
    """Upsert each handle as a ``platform="x"`` source; return {handle: source_id}.

    Mirrors persist_following's upsert so the rows look exactly like what
    ``store.list_sources(platform="x")`` returns at runtime.
    """
    source_id_by_handle: Dict[str, int] = {}
    for handle in handles:
        source_id = store.upsert_source(
            platform="x",
            external_id=handle,
            display_name=handle.title(),
            category="signal",
        )
        source_id_by_handle[handle] = source_id
    return source_id_by_handle


def _tweet_payload(handle: str, tweet_ids: List[str]) -> str:
    """Build a canned ``from:<handle>`` SearchTimeline JSON array (CLI output shape)."""
    return json.dumps(
        [
            {
                "id": tweet_id,
                "text": f"tweet {tweet_id} from {handle}",
                "author": {"username": handle},
                "createdAt": "2026-06-18T00:00:00Z",
                "likeCount": 10,
                "retweetCount": 2,
                "replyCount": 1,
                "quoteCount": 0,
            }
            for tweet_id in tweet_ids
        ]
    )


def _make_subproc_stub(
    payload_by_handle: Dict[str, str],
    queried_handles: List[str],
    queried_queries: Optional[List[str]] = None,
):
    """Build a fake run_with_timeout that records the queried handle and returns its payload.

    The command's positional query is ``from:<handle> -filter:retweets -filter:replies``; we
    take the handle from the first whitespace-delimited token after ``from:``, record it (for
    the rotation test's coverage assertion) and, when provided, record the FULL query string
    (for the retweets/replies-filter assertion), then return that handle's canned payload.
    """

    def _fake_run(cmd, *, timeout, env=None, on_pid=None):  # noqa: ANN001, ARG001
        query = next((arg for arg in cmd if isinstance(arg, str) and arg.startswith("from:")), "")
        if queried_queries is not None:
            queried_queries.append(query)
        # Reason: the query now carries trailing search operators, so the handle is the token
        # between ``from:`` and the first space, not the whole remainder of the string.
        handle = query[len("from:") :].split()[0] if query else ""
        queried_handles.append(handle)
        return subproc.SubprocResult(returncode=0, stdout=payload_by_handle.get(handle, "[]"), stderr="")

    return _fake_run


def test_delta_returns_only_unseen_and_marks_them() -> None:
    """Only tweets NOT already in ``seen`` are returned, and the new ones get marked seen.

    WHY: a re-seen tweet must never reappear as new (would resurface the same content
    every run); a genuinely new tweet must not be dropped. This is the delta contract.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        source_id_by_handle = _seed_x_sources(["alice"])
        alice_id = source_id_by_handle["alice"]

        # Pre-seed two tweet ids as already seen for alice.
        store.mark_seen(alice_id, "t1")
        store.mark_seen(alice_id, "t2")

        # The handle's timeline returns t1, t2 (seen) AND t3, t4 (new).
        payloads = {"alice": _tweet_payload("alice", ["t1", "t2", "t3", "t4"])}
        queried: List[str] = []
        sources = store.list_sources(platform="x")

        with patch.object(bird_x.subproc, "run_with_timeout", _make_subproc_stub(payloads, queried)):
            new_tweets = bird_x.fetch_new_tweets(sources, depth="default", run_day_ordinal=0, sleeper=lambda _s: None)

        returned_ids = {tweet.tweet_id for tweet in new_tweets}
        # Delta intent: ONLY the unseen ids come back; the pre-seeded ones are filtered.
        assert returned_ids == {"t3", "t4"}
        # The returned tweets carry the fields Sub-phase 4 needs.
        sample = next(tweet for tweet in new_tweets if tweet.tweet_id == "t3")
        assert sample.handle == "alice"
        assert sample.like_count == 10 and sample.retweet_count == 2
        assert sample.text and sample.created_at

        # The newly-returned ids are now marked seen (so a re-run would not resurface them).
        seen_after = store.get_seen_ids(alice_id)
        assert {"t1", "t2", "t3", "t4"}.issubset(seen_after)


def test_rotation_fairness_grows_coverage_across_days() -> None:
    """Across two consecutive run_day_ordinal values the UNION of deep-pulled handles strictly grows.

    WHY (resolved Q5): when the follow count exceeds the per-run budget, a non-rotating
    selection permanently starves everyone outside the first window. The rotation must
    move the window each day so coverage widens toward full. This assertion FAILS if the
    offset logic is wrong (e.g. a plain head-slice that always pulls the same handles).
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        # 15 handles, but "quick" budget is 12 -> a window of 12 rotates across days.
        handles = [f"h{n:02d}" for n in range(15)]
        _seed_x_sources(handles)
        assert len(handles) > bird_x.DEPTH_CONFIG["quick"]  # precondition: list > budget

        payloads = {handle: _tweet_payload(handle, [f"{handle}_t1"]) for handle in handles}
        sources = store.list_sources(platform="x")

        queried_day0: List[str] = []
        with patch.object(bird_x.subproc, "run_with_timeout", _make_subproc_stub(payloads, queried_day0)):
            bird_x.fetch_new_tweets(sources, depth="quick", run_day_ordinal=0, sleeper=lambda _s: None)

        queried_day1: List[str] = []
        with patch.object(bird_x.subproc, "run_with_timeout", _make_subproc_stub(payloads, queried_day1)):
            bird_x.fetch_new_tweets(sources, depth="quick", run_day_ordinal=1, sleeper=lambda _s: None)

        day0_handles = set(queried_day0)
        day1_handles = set(queried_day1)
        coverage_after_day0 = day0_handles
        coverage_after_day1 = day0_handles | day1_handles

        # Each run pulls exactly the budget (12) of the 15 handles.
        assert len(day0_handles) == bird_x.DEPTH_CONFIG["quick"]
        assert len(day1_handles) == bird_x.DEPTH_CONFIG["quick"]
        # Rotation-fairness invariant: day 1 reaches handles day 0 did not, so the union
        # of deep-pulled handles STRICTLY GROWS toward full coverage (not a tautology —
        # fails if the window does not rotate).
        assert len(coverage_after_day1) > len(coverage_after_day0)
        assert day1_handles - day0_handles  # day 1 pulled at least one handle day 0 missed


def test_inter_request_delay_is_invoked_between_handles() -> None:
    """The injected sleeper is invoked once per handle past the first (pacing guardrail).

    WHY (ToS-gray, reference/integrations.md §2): without the inter-handle delay the loop
    fans out every handle instantly and risks X rate-limiting / flagging the account. The
    sleeper is injected so the test asserts pacing without real sleeping.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        handles = ["alice", "bob", "carol", "dave"]
        _seed_x_sources(handles)
        payloads = {handle: _tweet_payload(handle, [f"{handle}_t1"]) for handle in handles}
        sources = store.list_sources(platform="x")

        sleep_calls: List[float] = []

        def _recording_sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)

        queried: List[str] = []
        with patch.object(bird_x.subproc, "run_with_timeout", _make_subproc_stub(payloads, queried)):
            bird_x.fetch_new_tweets(sources, depth="default", run_day_ordinal=0, sleeper=_recording_sleeper)

        # 4 handles -> the delay is applied before handles 2..4 (once per handle past the first).
        assert len(sleep_calls) == len(handles) - 1
        # And it sleeps the configured conservative inter-request delay each time.
        assert all(seconds == bird_x.INTER_REQUEST_DELAY_SECONDS for seconds in sleep_calls)


def test_query_excludes_retweets_and_replies() -> None:
    """WHY: a bare ``from:<handle>`` search returns the handle's retweets and replies, which
    are digest noise (the user asked for original posts only). The query must carry the
    ``-filter:retweets -filter:replies`` operators so they are excluded AT THE SOURCE, before
    we ever fetch or classify them. This assertion fails if the operators are dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        _seed_x_sources(["alice"])
        payloads = {"alice": _tweet_payload("alice", ["t1"])}
        queried: List[str] = []
        queries: List[str] = []
        sources = store.list_sources(platform="x")

        with patch.object(bird_x.subproc, "run_with_timeout", _make_subproc_stub(payloads, queried, queries)):
            bird_x.fetch_new_tweets(sources, depth="default", run_day_ordinal=0, sleeper=lambda _s: None)

        assert queries, "the handle must have been queried"
        assert queries[0] == "from:alice -filter:retweets -filter:replies"


def test_parse_tweets_drops_retweet_prefixed_text() -> None:
    """WHY: defense-in-depth for the ``-filter:retweets`` query operator. A retweet that slips
    past the server-side filter still surfaces its ``RT @author: ...`` prefix, and a retweet
    is not the followed account's own content — it must never enter the digest. A genuine
    original tweet in the SAME payload must still survive (the guard must not over-drop)."""
    parsed = [
        {"id": "rt1", "text": "RT @someone: hot take", "author": {"username": "alice"}},
        {"id": "orig1", "text": "my own original thought", "author": {"username": "alice"}},
    ]

    tweets = bird_x._parse_tweets(parsed, handle="alice")

    assert [tweet.tweet_id for tweet in tweets] == ["orig1"], "RT-prefixed dropped, original kept"
