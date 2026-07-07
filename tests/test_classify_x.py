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

import orbit  # noqa: E402
import store  # noqa: E402
from lib import classify, paths  # noqa: E402
from lib.bird_x import Tweet  # noqa: E402
from lib.config import OrbitConfig  # noqa: E402


def _fresh_store(tmp_dir: Path) -> None:
    """Point the store at a temp DB and init it (no real ~/.local/share write)."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()


def _fresh_store_with_x_handles(tmp_dir: Path, handles: list[str]) -> None:
    """Point the store at a temp DB, init it, and persist one X source per handle.

    The X producer keys each tweet's Axis-A prior off the source row whose ``external_id``
    matches the tweet's ``handle``, so the handles here must match the tweets under test.
    """
    _fresh_store(tmp_dir)
    for handle in handles:
        store.upsert_source(
            platform="x",
            external_id=handle,
            display_name=f"@{handle}",
            category="signal",
        )


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


def test_x_producer_skips_tweet_when_classify_times_out(capsys) -> None:  # noqa: ANN001
    """A transient classify LLM timeout skips ONE tweet, never aborts the digest.

    WHY (Rule 9): the X half shares the same robustness contract as YouTube — a single
    ``claude -p`` timeout raised by ``classify.classify_item`` for one tweet must degrade
    to skipping that tweet, while every other tweet still reaches the unified digest.
    Before the per-item try/except a lone timeout aborted the ENTIRE pipeline. We persist
    two X sources, inject a delta that returns two tweets and a classifier that raises the
    real ``LlmCliError`` for one (routed by its text), then assert: the producer returns
    WITHOUT raising; the timed-out tweet is ABSENT from the returned items; the healthy
    tweet survives; and the ``x_stage1_item_classify_skipped`` warning was logged. Reverting
    the try/except re-raises here and fails the test.
    """
    healthy_tweet = Tweet(
        text="A sharp, healthy take on attention scaling.",
        tweet_id="1900000000000000010",
        handle="alice",
        created_at="2026-06-18T00:00:00Z",
        like_count=120,
        retweet_count=45,
        reply_count=8,
        quote_count=3,
    )
    doomed_tweet = Tweet(
        text="TIMEOUT this tweet please.",
        tweet_id="1900000000000000011",
        handle="bob",
        created_at="2026-06-18T00:00:00Z",
        like_count=90,
        retweet_count=10,
        reply_count=2,
        quote_count=1,
    )

    def _flaky_classifier(prompt: str) -> str:
        # Route the failure by the tweet text, which reaches the shared prompt body.
        if "TIMEOUT this tweet please." in prompt:
            raise orbit.LlmCliError("claude -p timed out")
        return '{"axis_a_signal": 1, "axis_b_on_topic": 1}'

    def _mock_x_delta(x_sources, depth, ordinal):  # noqa: ANN001 — test stub
        return [healthy_tweet, doomed_tweet]

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_x_handles(Path(tmp), ["alice", "bob"])
        config = OrbitConfig(interests=["ai", "transformers"])
        items = orbit.run_stage1_build_x_items(
            config,
            depth="default",
            x_delta=_mock_x_delta,
            llm_classifier=_flaky_classifier,
        )

    # The run survived the timeout and dropped ONLY the doomed tweet.
    built_ids = [item.item_external_id for item in items]
    assert built_ids == [healthy_tweet.tweet_id], "the timed-out tweet must be skipped, the healthy one kept"
    assert doomed_tweet.tweet_id not in built_ids

    # The skip was surfaced (Rule 12), not swallowed.
    assert "x_stage1_item_classify_skipped" in capsys.readouterr().out


def test_x_producer_drops_axis_a_noise_tweet(capsys) -> None:  # noqa: ANN001
    """A tweet the classifier judges noise (axis_a_signal == 0) is EXCLUDED from the digest.

    WHY (Rule 9): the user asked to "extract alpha, drop generic". Ranking already pushes
    low-signal tweets down, but density controls only WHERE a tweet renders, never WHETHER
    it renders — so generic posts ("gm", platitudes, engagement-bait) still leaked into the
    index tier. This test pins the X-only alpha gate: a tweet classified axis_a_signal=0 must
    NOT reach the returned items, a substantive tweet in the same batch MUST survive (the gate
    must not over-drop), and the drop count must be surfaced. Reverting the gate re-admits the
    noise tweet and fails the first assertion. YouTube inclusion is deliberately not gated.
    """
    signal_tweet = Tweet(
        text="Concrete benchmark: MoE routing cut inference cost 38% at our scale.",
        tweet_id="1900000000000000020",
        handle="alice",
        created_at="2026-06-18T00:00:00Z",
        like_count=200,
        retweet_count=60,
        reply_count=12,
        quote_count=5,
    )
    noise_tweet = Tweet(
        text="gm everyone, happy monday, blessed to be here",
        tweet_id="1900000000000000021",
        handle="bob",
        created_at="2026-06-18T00:00:00Z",
        like_count=3,
        retweet_count=0,
        reply_count=0,
        quote_count=0,
    )

    def _axis_a_classifier(prompt: str) -> str:
        # Route the verdict by the tweet text, which reaches the shared prompt body.
        if "happy monday" in prompt:
            return '{"axis_a_signal": 0, "axis_b_on_topic": 1}'
        return '{"axis_a_signal": 1, "axis_b_on_topic": 1}'

    def _mock_x_delta(x_sources, depth, ordinal):  # noqa: ANN001 — test stub
        return [signal_tweet, noise_tweet]

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_x_handles(Path(tmp), ["alice", "bob"])
        config = OrbitConfig(interests=["ai", "transformers"])
        items = orbit.run_stage1_build_x_items(
            config,
            depth="default",
            x_delta=_mock_x_delta,
            llm_classifier=_axis_a_classifier,
        )

    built_ids = [item.item_external_id for item in items]
    assert built_ids == [signal_tweet.tweet_id], "the axis-A noise tweet must be dropped, the signal tweet kept"
    assert noise_tweet.tweet_id not in built_ids
    # The drop count was surfaced (Rule 12), not swallowed.
    out = capsys.readouterr().out
    assert "x_stage1_build_completed" in out
    assert '"dropped_noise_count": 1' in out


def test_x_producer_drops_category_other_tweet(capsys) -> None:  # noqa: ANN001
    """A tweet the classifier tags ``category == "other"`` is EXCLUDED from the digest.

    WHY (Rule 9): the 2026-07-06 taxonomy ruling gates BOTH sources on the shared classify
    path — an item outside the fixed taxonomy (``other``) is dropped, not merely deranked.
    The X half must gate identically to YouTube: a substantive ``ai`` tweet survives, an
    otherwise-substantive ``other`` tweet (signal on Axis A, so the noise gate does NOT catch
    it) is dropped by the category gate, and the drop count is surfaced. Reverting the
    category gate re-admits the ``other`` tweet and fails the first assertion.
    """
    kept_tweet = Tweet(
        text="Concrete benchmark: MoE routing cut inference cost 38% at our scale.",
        tweet_id="1900000000000000030",
        handle="alice",
        created_at="2026-06-18T00:00:00Z",
        like_count=200,
        retweet_count=60,
        reply_count=12,
        quote_count=5,
    )
    other_tweet = Tweet(
        text="My cat did the funniest thing at brunch today, thread below.",
        tweet_id="1900000000000000031",
        handle="bob",
        created_at="2026-06-18T00:00:00Z",
        like_count=150,
        retweet_count=40,
        reply_count=9,
        quote_count=2,
    )

    def _category_classifier(prompt: str) -> str:
        # Both are Axis-A signal (so the noise gate does not catch the 'other' one); only
        # the category axis differs. Route by tweet text, which reaches the prompt body.
        if "My cat did the funniest thing" in prompt:
            return '{"axis_a_signal": 1, "axis_b_on_topic": 1, "category": "other"}'
        return '{"axis_a_signal": 1, "axis_b_on_topic": 1, "category": "ai"}'

    def _mock_x_delta(x_sources, depth, ordinal):  # noqa: ANN001 — test stub
        return [kept_tweet, other_tweet]

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store_with_x_handles(Path(tmp), ["alice", "bob"])
        config = OrbitConfig(interests=["ai", "transformers"])
        items = orbit.run_stage1_build_x_items(
            config,
            depth="default",
            x_delta=_mock_x_delta,
            llm_classifier=_category_classifier,
        )

    built_ids = [item.item_external_id for item in items]
    assert built_ids == [kept_tweet.tweet_id], "the 'other'-category tweet must be dropped, the 'ai' tweet kept"
    assert other_tweet.tweet_id not in built_ids
    # The category-drop count was surfaced (Rule 12), not swallowed.
    out = capsys.readouterr().out
    assert '"category_dropped_count": 1' in out
