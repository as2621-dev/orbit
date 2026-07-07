"""DoD tests for two-axis classification with a channel prior (Phase 2 / Sub-phase 3).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Classification is Stage 2 — it decides what is signal vs. noise and on- vs. off-topic,
which Phase 3 (rank) reads back from the store. So the tests assert the intents the
product depends on:

  1. Never-drop / also-posted routing: an item failing an axis is routed to the "they
     also posted" strip (``is_also_posted == True``) AND persisted on-record — NOT
     dropped (design decision 5: derank, never delete).
  2. Override-no-LLM: a stored ``is_user_override=1`` classification is returned WITHOUT
     calling the LLM (user corrections are sacred — re-classifying them would silently
     undo the user's correction every run).
  3. Channel-prior seeds the uncertain verdict: when the model returns junk the verdict
     falls back to the channel prior for Axis A (so a flaky model line does not lose the
     channel's signal/noise posture).
  4. Clean happy path + malformed-JSON-without-crash, and the default boundary fails loud.

All external boundaries are mocked: the LLM boundary is injected per call (no real
model — there is none in this build env), and the store is pointed at a temp DB via
``ORBIT_DB_PATH`` + ``store._db_override`` (no real ``~/.local/share`` write). Mirrors
the temp-DB setup in ``tests/test_delta_uploads.py``.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Make ``scripts`` importable so ``import store`` and
# ``from lib import classify`` resolve regardless of the working directory.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import classify, paths  # noqa: E402


def _fresh_store(tmp_dir: Path) -> None:
    """Point the store at a temp DB and init it (no real ~/.local/share write)."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()


def _upload(video_id: str = "vid000003") -> dict:
    """A minimal item dict (video_id/title/description), the dict-shaped item input."""
    return {
        "video_id": video_id,
        "title": f"Title {video_id}",
        "description": f"Description {video_id}",
    }


def _verdict(axis_a_signal: int, axis_b_on_topic: int) -> str:
    """A clean strict-JSON verdict string the LLM boundary would return."""
    return f'{{"axis_a_signal": {axis_a_signal}, "axis_b_on_topic": {axis_b_on_topic}}}'


def _verdict_with_category(axis_a_signal: int, axis_b_on_topic: int, category: str) -> str:
    """A clean strict-JSON verdict string including the third (category) axis."""
    return (
        f'{{"axis_a_signal": {axis_a_signal}, "axis_b_on_topic": {axis_b_on_topic}, '
        f'"category": "{category}"}}'
    )


def test_failing_axis_routes_to_also_posted_and_persists_never_dropped() -> None:
    """An item failing Axis B is routed to "also posted" (NOT dropped) and persisted.

    WHY: design decision 5 — items that fail either axis are deranked into the "they
    also posted" strip, never deleted. A regression that dropped (returned None, or
    didn't persist) such items would silently hide a creator's post the user might still
    want. We assert BOTH the routing flag (``is_also_posted``) AND that the row is
    on-record in the store (so Phase 3's rank can read it back). The model says
    signal + off-topic -> must still be persisted and routed, not dropped.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai", "robotics"],
            llm_classifier=lambda prompt: _verdict(axis_a_signal=1, axis_b_on_topic=0),
        )
        # On-record: Phase 3 (rank) reads the verdict back via the store. Read inside
        # the temp-dir context so the DB file still exists when we query it.
        persisted = store.get_classification("vid000003")

    # Routed to "they also posted" because it fails Axis B (off-topic) — never dropped.
    assert result.is_also_posted is True
    assert result.axis_a_signal == 1
    assert result.axis_b_on_topic == 0
    assert persisted is not None, "a failing-axis item must be persisted, not dropped"
    assert persisted["axis_a_signal"] == 1
    assert persisted["axis_b_on_topic"] == 0
    assert persisted["is_user_override"] == 0


def test_user_override_returned_without_calling_llm() -> None:
    """A stored is_user_override=1 classification is returned WITHOUT calling the LLM.

    WHY: user corrections are sacred (override-persistence intent). If classify_item
    re-ran the model over an overridden item, it would silently undo the user's
    correction on the next run. We pre-seed an override, inject an LLM boundary that
    RAISES if called, and assert the stored verdict comes back and the boundary was
    never invoked.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        # User corrected this item to noise + on-topic, and marked it an override.
        store.set_classification("vid000003", axis_a_signal=0, axis_b_on_topic=1, is_user_override=1)

        llm_must_not_be_called = MagicMock(side_effect=AssertionError("LLM must not be called on an override"))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",  # prior disagrees (would say signal) — must be ignored
            interests=["ai"],
            llm_classifier=llm_must_not_be_called,
        )

    llm_must_not_be_called.assert_not_called()
    assert result.is_user_override == 1
    assert result.axis_a_signal == 0
    assert result.axis_b_on_topic == 1
    assert result.is_also_posted is True  # noise -> also-posted


def test_channel_prior_seeds_axis_a_when_verdict_unparseable_noise() -> None:
    """When the model returns junk, Axis A falls back to a 'noise' channel prior (0).

    WHY: a flaky / malformed model line must not silently flip the channel's Axis-A
    posture. The prior is the safety net. With category='noise', an unparseable verdict
    must seed ``axis_a_signal == 0``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="noise",
            interests=["ai"],
            llm_classifier=lambda prompt: "this is not json at all",
        )

    assert result.axis_a_signal == 0, "noise prior must seed Axis A=0 on an unparseable verdict"


def test_channel_prior_seeds_axis_a_when_verdict_unparseable_signal() -> None:
    """When the model returns junk, Axis A falls back to a 'signal' channel prior (1).

    WHY: mirror of the noise case — with category='signal', an unparseable verdict must
    seed ``axis_a_signal == 1``. Two categories, two asserts, so a prior wired backwards
    fails loudly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=lambda prompt: "",  # empty -> unparseable
        )

    assert result.axis_a_signal == 1, "signal prior must seed Axis A=1 on an unparseable verdict"


def test_clean_signal_on_topic_verdict_is_top_line() -> None:
    """A clean signal + on-topic verdict passes both axes -> NOT in 'also posted'.

    WHY: the happy path. An item that is signal AND on-topic is a top-line item;
    ``is_also_posted`` must be False so it is not deranked. This is the inverse of the
    never-drop test and guards the boolean's polarity.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=lambda prompt: _verdict(axis_a_signal=1, axis_b_on_topic=1),
        )

    assert result.axis_a_signal == 1
    assert result.axis_b_on_topic == 1
    assert result.is_also_posted is False


def test_malformed_json_falls_back_without_crashing() -> None:
    """Malformed JSON must fall back to the prior, never raise (defensive parse).

    WHY: the classify run processes many items; one bad model line must not crash the
    whole run. We feed half-valid JSON and assert a Classification is still returned
    (seeded from the prior) rather than an exception propagating.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic":',  # truncated
        )

    assert isinstance(result, classify.Classification)
    assert result.axis_a_signal == 1  # signal prior seeds it; no crash


def test_default_llm_classifier_fails_loud() -> None:
    """The default boundary must raise NotImplementedError — no faked verdict.

    WHY: there is no live LLM in this build env. A default that fabricated a verdict
    would silently classify on garbage. Fail loud (Rule 12): the default must raise so
    a missing runtime wiring is impossible to ignore.
    """
    raised = None
    try:
        classify._default_llm_classifier("any prompt")
    except NotImplementedError as exc:
        raised = exc

    assert raised is not None, "the default LLM boundary must raise NotImplementedError"


def test_each_taxonomy_category_is_parsed_onto_the_classification() -> None:
    """Each of ai/business/tech/sports is parsed verbatim onto ``Classification.category``.

    WHY (Rule 9): the category axis is the Stage-1 taxonomy gate's input. orbit.py drops
    only ``"other"`` and keeps every other taxonomy value, so if a clean ``ai`` verdict did
    not survive onto ``classification.category`` the gate would drop good items (or keep the
    wrong ones). We assert all four keep-categories round-trip exactly — a parser that
    dropped or defaulted them would fail here, not silently empty the digest.
    """
    for category in ("ai", "business", "tech", "sports"):
        with tempfile.TemporaryDirectory() as tmp:
            _fresh_store(Path(tmp))
            result = classify.classify_item(
                _upload(),
                channel_category="signal",
                interests=["ai"],
                llm_classifier=lambda prompt, _cat=category: _verdict_with_category(1, 1, _cat),
            )
        assert result.category == category, f"{category} verdict must round-trip onto the classification"
        # A keep-category never routes to also-posted on the category axis (that is the
        # gate's job in orbit.py); the two binary axes still decide also-posted.
        assert result.is_also_posted is False


def test_other_category_is_parsed_as_other_for_the_gate() -> None:
    """A clean ``other`` verdict yields ``category == "other"`` so the Stage-1 gate can drop it.

    WHY (Rule 9): the whole point of the taxonomy axis is that ``other`` is the drop signal.
    If ``other`` were coerced away (to the keep-sentinel) the gate would never fire and
    off-taxonomy noise would leak into the digest. classify.py must surface ``other``
    verbatim; the DROP decision lives in orbit.py (tested there).
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=lambda prompt: _verdict_with_category(1, 1, "other"),
        )

    assert result.category == "other", "an explicit 'other' verdict must be surfaced for the gate to drop"


def test_missing_category_defaults_to_keep_sentinel_and_logs() -> None:
    """A verdict with NO category defaults to the keep-sentinel (NOT "other") and logs.

    WHY (Rule 12): a prompt regression that stopped emitting ``category`` must NOT silently
    empty the digest. If a missing category defaulted to ``"other"`` the Stage-1 gate would
    drop EVERY item on such a regression. So a missing category fails OPEN — it defaults to
    the keep-sentinel (outside the taxonomy, so the gate keeps it) and logs
    ``classify_category_unparseable`` so the regression is visible, never swallowed. We feed
    the legacy two-axis verdict (no category) and assert both the sentinel and the warning.
    """
    captured = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        with contextlib.redirect_stdout(captured):
            result = classify.classify_item(
                _upload(),
                channel_category="signal",
                interests=["ai"],
                llm_classifier=lambda prompt: _verdict(axis_a_signal=1, axis_b_on_topic=1),
            )

    assert result.category == classify._CATEGORY_KEEP_ON_PARSE_FAILURE
    assert result.category != "other", "a missing category must NEVER default to the drop value"
    assert "classify_category_unparseable" in captured.getvalue(), "the fallback must be surfaced, not swallowed"


def test_garbled_off_taxonomy_category_defaults_to_keep_sentinel() -> None:
    """An off-taxonomy label (e.g. "politics") defaults to the keep-sentinel, not "other".

    WHY (Rule 12): the model must never invent its way into a DROP. If it returns a label
    outside the fixed taxonomy, we do not trust it and we do not map it to ``"other"``
    (which would drop the item) — we fail open to the keep-sentinel so the item survives and
    the anomaly is logged. Guards against a fabricated category silently pruning the digest.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        result = classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=lambda prompt: _verdict_with_category(1, 1, "politics"),
        )

    assert result.category == classify._CATEGORY_KEEP_ON_PARSE_FAILURE
    assert result.category != "other", "an off-taxonomy label must fail open to keep, not drop"


def test_classify_prompt_renders_the_fixed_taxonomy() -> None:
    """The rendered classify prompt carries the fixed taxonomy from references/classify.md.

    WHY (Rule 9): the taxonomy lives in the prompt file (tuned without touching code). If a
    prompt edit dropped the taxonomy the model would have no category vocabulary and every
    verdict would garble — silently emptying the digest via the gate is avoided only because
    classify.py fails open, but the digest would still be miscategorized. We capture the
    exact prompt the LLM boundary receives and assert every taxonomy member is present.
    """
    captured_prompts: list[str] = []

    def _capturing_llm(prompt: str) -> str:
        captured_prompts.append(prompt)
        return _verdict_with_category(1, 1, "ai")

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        classify.classify_item(
            _upload(),
            channel_category="signal",
            interests=["ai"],
            llm_classifier=_capturing_llm,
        )

    assert len(captured_prompts) == 1
    rendered = captured_prompts[0]
    for taxonomy_member in ("ai", "business", "tech", "sports", "other"):
        assert taxonomy_member in rendered, f"the prompt must render the '{taxonomy_member}' category"


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
