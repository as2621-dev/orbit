"""DoD tests for the Tiles digest's LLM prose boundary (Phase 7 / Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does. The
load-bearing intent of ``lib.summarize`` is graceful degradation (Rule 12 / brief's
"Graceful degradation"): the daily digest must STILL render when the LLM is flaky. So
the central assertions are:

  1. ``summarize_items`` returns a per-id blurb map AND truncates a >140-char blurb in
     code — the model's length is never trusted (a runaway blurb must never blow the
     tile layout).
  2. ``synthesize_verdict`` returns the model's sentence, and its prompt CARRIES scoop +
     cluster context — the masthead verdict must reflect the day's real shape, not be
     summarized from headlines alone.
  3. An LLM EXCEPTION yields ``{}`` / ``""`` — a down or flaky LLM must NEVER break the
     digest (the whole reason the boundary is fail-soft). A regression that let the
     exception escape would take the entire run down with it.
  4. An empty input short-circuits to ``{}`` / ``""`` with NO model call (cost control +
     no spurious CLI spawn on a quiet day).

The live-model boundary (``lib.llm.call_claude_cli``) is mocked at the seam — injected
via ``llm_call=`` and, in one test, monkeypatched on the module — so NO test spawns the
real ``claude`` CLI or touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make ``scripts`` importable so ``from lib import summarize`` resolves regardless of cwd.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import summarize  # noqa: E402


class _FakeRankable:
    """A minimal RankableItem stand-in exposing the fields summarize reads."""

    def __init__(self, item_external_id: str, title: str, channel_name: str = "Some Channel") -> None:
        self.item_external_id = item_external_id
        self.title = title
        self.channel_name = channel_name


class _FakeTieredItem:
    """A TieredItem stand-in: ``.scored_item.item`` is the rankable; ``.density_tier`` the tier."""

    def __init__(self, rankable: _FakeRankable, density_tier: str) -> None:
        self.scored_item = MagicMock(item=rankable)
        self.density_tier = density_tier


class _FakeScoop:
    """A TrendingItem stand-in exposing ``title`` (what the verdict context reads)."""

    def __init__(self, title: str) -> None:
        self.title = title


class _FakeCluster:
    """A Cluster stand-in exposing the fields the verdict context reads."""

    def __init__(self, member_item_ids: list[str], source_diversity: int, representative_item_id: str) -> None:
        self.member_item_ids = member_item_ids
        self.source_diversity = source_diversity
        self.representative_item_id = representative_item_id


# --- summarize_items ---------------------------------------------------------


def test_summarize_items_returns_blurb_map_keyed_by_id() -> None:
    """Happy path: a per-id blurb map comes back, keyed by ``item_external_id``.

    WHY: Sub-phase 4 wires ``RankableItem.summary`` from this map by id; if the key were
    not the external id the renderer could attach the wrong blurb to a tile (or none).
    """
    items = [_FakeRankable("abc", "The M5 chip deep dive"), _FakeRankable("xyz", "A quiet talk")]
    fake_llm = MagicMock(return_value='{"abc": "Apple bares the M5 internals.", "xyz": "A slow, careful walkthrough."}')

    blurbs = summarize.summarize_items(items, llm_call=fake_llm)

    assert blurbs == {"abc": "Apple bares the M5 internals.", "xyz": "A slow, careful walkthrough."}
    fake_llm.assert_called_once()  # one batched call, not one per item (Rule 6 token discipline)


def test_summarize_items_truncates_blurb_over_140_chars() -> None:
    """A >140-char blurb is hard-truncated in code — the model's length is never trusted.

    WHY: the tile layout is sized for a one-liner; a runaway blurb would overflow the
    card. Defensive truncation (Rule 12) keeps the layout intact even if the model
    ignores the <=140 instruction.
    """
    long_blurb = "x" * 200
    fake_llm = MagicMock(return_value='{"abc": "' + long_blurb + '"}')

    blurbs = summarize.summarize_items([_FakeRankable("abc", "Title")], llm_call=fake_llm)

    assert len(blurbs["abc"]) == summarize.MAX_BLURB_CHARS == 140


def test_summarize_items_empty_input_returns_empty_map_without_calling_llm() -> None:
    """Edge case: an empty item list short-circuits to ``{}`` with NO model call.

    WHY: a quiet day must not spawn a spurious ``claude`` CLI process (cost control) and
    must still produce a valid (empty) blurb map the renderer can consume.
    """
    fake_llm = MagicMock()

    blurbs = summarize.summarize_items([], llm_call=fake_llm)

    assert blurbs == {}
    fake_llm.assert_not_called()


def test_summarize_items_llm_exception_degrades_to_empty_map() -> None:
    """An LLM exception yields ``{}`` — a flaky LLM must NEVER break the digest (Rule 12).

    WHY: this is the load-bearing degradation guarantee. If the exception escaped, one
    failed ``claude -p`` call would take down the entire daily render. We assert the
    failure is swallowed into an empty map (the renderer then omits blurbs).
    """
    def _boom(_prompt: str) -> str:
        raise RuntimeError("claude CLI exited 1")

    blurbs = summarize.summarize_items([_FakeRankable("abc", "Title")], llm_call=_boom)

    assert blurbs == {}


def test_summarize_items_unparseable_json_degrades_to_empty_map() -> None:
    """A non-JSON response degrades to ``{}`` rather than crashing the parse.

    WHY: ``claude -p`` can return prose instead of the strict JSON contract; the digest
    must absorb that as "no blurbs", not raise.
    """
    fake_llm = MagicMock(return_value="sorry, I cannot do that")

    blurbs = summarize.summarize_items([_FakeRankable("abc", "Title")], llm_call=fake_llm)

    assert blurbs == {}


def test_summarize_items_supports_monkeypatched_module_boundary(monkeypatch) -> None:
    """The default boundary is resolved at call time, so a module patch reaches the seam.

    WHY: the spec allows mocking by patching ``lib.summarize.call_claude_cli`` (not only
    by injecting ``llm_call=``). This guards that the default isn't frozen at import.
    """
    monkeypatch.setattr(summarize, "call_claude_cli", lambda _prompt: '{"abc": "patched blurb"}')

    blurbs = summarize.summarize_items([_FakeRankable("abc", "Title")])  # no llm_call -> default seam

    assert blurbs == {"abc": "patched blurb"}


# --- synthesize_verdict ------------------------------------------------------


def test_synthesize_verdict_returns_mocked_sentence() -> None:
    """Happy path: the masthead verdict is the model's single sentence (stripped).

    WHY: Sub-phase 4 renders this verbatim under the masthead; leading/trailing
    whitespace from ``claude -p`` would render as an awkward gap.
    """
    tiered = [_FakeTieredItem(_FakeRankable("abc", "The M5 chip deep dive"), "hero")]
    fake_llm = MagicMock(return_value="  Quiet day — the only real story is the M5 leak.  ")

    verdict = summarize.synthesize_verdict(tiered, [], [], llm_call=fake_llm)

    assert verdict == "Quiet day — the only real story is the M5 leak."


def test_synthesize_verdict_prompt_includes_scoop_and_cluster_context() -> None:
    """The verdict prompt CARRIES scoop + cluster context, not just headlines.

    WHY: the brief requires the verdict to reflect the day's real shape (scoops +
    convergence), so the prompt MUST ground the model in that material. A regression that
    summarized from titles alone would lose the "only real story is a scoop" framing.
    """
    tiered = [_FakeTieredItem(_FakeRankable("abc", "Headline title"), "hero")]
    scoops = [_FakeScoop("Dormant account breaks the M5 benchmark")]
    clusters = [_FakeCluster(member_item_ids=["t1", "t2"], source_diversity=3, representative_item_id="abc")]
    captured_prompt: dict[str, str] = {}

    def _capture(prompt: str) -> str:
        captured_prompt["value"] = prompt
        return "A verdict sentence."

    summarize.synthesize_verdict(tiered, scoops, clusters, llm_call=_capture)

    prompt = captured_prompt["value"]
    assert "Dormant account breaks the M5 benchmark" in prompt  # scoop context present
    assert "3 sources" in prompt  # cluster convergence context present
    assert "Headline title" in prompt  # headline context present


def test_synthesize_verdict_empty_inputs_returns_empty_without_calling_llm() -> None:
    """Edge case: an empty day returns ``""`` with NO model call.

    WHY: there is no day-shape to summarize on an empty batch; spawning ``claude`` would
    waste a call and risk fabricating a verdict from nothing.
    """
    fake_llm = MagicMock()

    verdict = summarize.synthesize_verdict([], [], [], llm_call=fake_llm)

    assert verdict == ""
    fake_llm.assert_not_called()


def test_synthesize_verdict_llm_exception_degrades_to_empty_string() -> None:
    """An LLM exception yields ``""`` — a flaky LLM must NEVER break the masthead (Rule 12).

    WHY: same degradation guarantee as ``summarize_items``. A raised error here would
    abort the whole render right at the masthead.
    """
    tiered = [_FakeTieredItem(_FakeRankable("abc", "Title"), "hero")]

    def _boom(_prompt: str) -> str:
        raise TimeoutError("claude -p timed out")

    verdict = summarize.synthesize_verdict(tiered, [], [], llm_call=_boom)

    assert verdict == ""
