"""DoD test for the SHARED classify path on an X Tweet (Phase 4 / Sub-phase 4).

Per Rule 9, this encodes WHY the shared path matters, not merely what it does. The
M2 promise is ONE pipeline for both sources: an X tweet (text-only, no transcript)
must classify on the SAME two axes, via the SAME ``classify.classify_item`` function,
persisted to the SAME ``store.classifications`` table — there is NO X-specific
classifier or table. A regression that forked classify for X (a separate function /
table, or that failed to read ``tweet_id``/``text`` off a Tweet) would break the
unified-pipeline intent: the rank/render half reads classifications back uniformly,
so a parallel X store would silently drop X items out of the digest.

All boundaries mocked: the LLM is injected per call (no live model in this build env),
the store points at a temp DB via ``ORBIT_DB_PATH`` + ``store._db_override`` (no real
``~/.local/share`` write). Mirrors the temp-DB setup in tests/test_classify.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Make ``scripts`` importable so ``import store`` and ``from lib import ...``
# resolve regardless of the working directory. Mirrors tests/test_classify.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import classify, paths  # noqa: E402
from lib.bird_x import Tweet  # noqa: E402


def _fresh_store(tmp_dir: Path) -> None:
    """Point the store at a temp DB and init it (no real ~/.local/share write)."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()


def _tweet(tweet_id: str = "1900000000000000001") -> Tweet:
    """A real X Tweet (text-only, no video_id/title/description) for the shared path."""
    return Tweet(
        text="A genuinely sharp take on transformer attention scaling.",
        tweet_id=tweet_id,
        handle="alice",
        created_at="2026-06-18T00:00:00Z",
        like_count=120,
        retweet_count=45,
        reply_count=8,
        quote_count=3,
    )


def test_x_tweet_classifies_on_shared_path_and_persists() -> None:
    """An X Tweet classifies on the SAME two-axis path and persists to store.classifications.

    WHY: the M2 unified-pipeline intent. A Tweet has ``tweet_id``/``text`` — NOT
    ``video_id``/``title``/``description``. The SAME ``classify_item`` must (a) read the
    tweet's id off ``tweet_id``, (b) render the prompt body from ``text``, (c) produce a
    valid two-axis ``Classification``, and (d) persist it to the SAME
    ``store.classifications`` table keyed by ``tweet_id`` — proving there is no
    X-specific classify function or table (a fork would mean X items never reach the
    shared rank/render read-back). We inject a clean signal+on-topic verdict and assert
    BOTH the returned Classification AND the persisted row.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tweet = _tweet()
        result = classify.classify_item(
            tweet,
            channel_category="signal",
            interests=["ai", "transformers"],
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
        )
        # Persisted to the SAME table the YouTube path uses — keyed on the tweet_id.
        persisted = store.get_classification(tweet.tweet_id)

    # The shared path resolved the id from tweet_id (not video_id) and produced a valid
    # two-axis verdict.
    assert result.item_external_id == tweet.tweet_id, "item id must resolve from tweet_id on the shared path"
    assert result.axis_a_signal == 1
    assert result.axis_b_on_topic == 1
    assert result.is_also_posted is False  # signal + on-topic -> top-line

    # On-record in store.classifications (the SAME table) — no X-specific store.
    assert persisted is not None, "an X tweet must persist to store.classifications (shared table)"
    assert persisted["item_external_id"] == tweet.tweet_id
    assert persisted["axis_a_signal"] == 1
    assert persisted["axis_b_on_topic"] == 1
    assert persisted["is_user_override"] == 0


def test_x_tweet_prompt_body_reads_tweet_text() -> None:
    """The shared prompt renderer reads the tweet's ``text`` into the prompt body.

    WHY: a Tweet has no ``title``/``description``; if the renderer only read those YT
    fields it would hand the model an EMPTY item, so the verdict would be meaningless
    junk. We capture the rendered prompt the LLM boundary receives and assert the tweet
    text is in it — proving the text-only mapping (text -> prompt body) actually works
    on the shared path, not just that an id resolved.
    """
    captured_prompts: list[str] = []

    def _capturing_llm(prompt: str) -> str:
        captured_prompts.append(prompt)
        return '{"axis_a_signal": 1, "axis_b_on_topic": 1}'

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tweet = _tweet()
        classify.classify_item(
            tweet,
            channel_category="signal",
            interests=["ai"],
            llm_classifier=_capturing_llm,
        )

    assert len(captured_prompts) == 1
    assert tweet.text in captured_prompts[0], "the tweet text must be substituted into the shared prompt body"


def test_x_tweet_user_override_respected_on_shared_path() -> None:
    """A stored override for a tweet_id is returned WITHOUT calling the LLM (shared path).

    WHY: user corrections are sacred for X items too — the override short-circuit is the
    SAME deterministic code for both sources, keyed on the item id (here a tweet_id). If
    the X path bypassed the override check, a user's correction on a tweet would be
    silently re-classified every run. We pre-seed an override on the tweet_id, inject an
    LLM that raises if called, and assert the stored verdict comes back untouched.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tweet = _tweet()
        store.set_classification(tweet.tweet_id, axis_a_signal=0, axis_b_on_topic=1, is_user_override=1)

        llm_must_not_be_called = MagicMock(side_effect=AssertionError("LLM must not be called on an override"))
        result = classify.classify_item(
            tweet,
            channel_category="signal",  # prior disagrees — must be ignored on an override
            interests=["ai"],
            llm_classifier=llm_must_not_be_called,
        )

    llm_must_not_be_called.assert_not_called()
    assert result.item_external_id == tweet.tweet_id
    assert result.is_user_override == 1
    assert result.axis_a_signal == 0
    assert result.axis_b_on_topic == 1
