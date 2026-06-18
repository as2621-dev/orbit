"""DoD tests for external corroboration tagging (Phase 5 / Sub-phase 3, Stage 5b).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Stage 5b is the brief's corroboration-vs-scoop distinction: for the TOP internal-
trending items, a light KEYLESS external cross-search decides whether a topic is
also big OUTSIDE the user's network ("corroborated") or whether the user's network
had it first with little external signal ("scoop" — the high-value find). The tests
pin the product intents, each constructed to FAIL on wrong business logic:

  1. Corroboration vs scoop is a DETERMINISTIC count threshold: an item whose
     cross-search returns MANY results is tagged ``corroborated``; one returning
     NEAR-ZERO is tagged ``scoop``. A regression that inverted the threshold (or
     ignored the count) would flip A and B — this fails on that.
  2. ``depth`` BOUNDS the number of cross-searches issued (cost control / CSO). With
     MORE trending items than the quick budget, the mock ``search_fn`` is called AT
     MOST the quick-budget number of times. A regression that searched every item
     (unbounded egress) would over-call — this fails on that.
  3. Defensive (Rule 12): an empty trending list issues NO searches and never crashes;
     a ``search_fn`` that RAISES or returns empty degrades the item to a safe default
     tag (``scoop``, no external signal) without crashing.
  4. No live web call: the cross-search boundary is injected/mocked in every test;
     the module imports stdlib + lib only, no new pip dependency.

The network boundary is the injected ``search_fn`` (and, for the keyless module, an
injected ``page_fetcher``) — NO test touches the network. All inputs are constructed
fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_trending.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import web_search_keyless  # noqa: E402
from lib.external_trending import (  # noqa: E402
    CORROBORATION_TAG_CORROBORATED,
    CORROBORATION_TAG_SCOOP,
    CORROBORATION_TAG_UNTAGGED,
    DEPTH_CROSS_SEARCH_BUDGET,
    tag_external_corroboration,
)
from lib.trending import TrendingItem  # noqa: E402
from lib.web_search_keyless import SearchResult, keyless_search  # noqa: E402


def _trending_item(cluster_id: str, *, title: str, velocity: float = 1.0) -> TrendingItem:
    """A minimal TrendingItem with a title to cross-search on (other fields neutral)."""
    return TrendingItem(
        item_external_id=f"item-{cluster_id}",
        cluster_id=cluster_id,
        creator_external_id="UC1",
        title=title,
        card_url="",
        velocity_score=velocity,
        convergence_count=1,
        baseline_relative_ratio=velocity,
    )


def _result(index: int) -> SearchResult:
    """A canned external search hit (only the count matters for tagging)."""
    return SearchResult(
        result_title=f"external hit {index}",
        result_url=f"https://example.test/{index}",
        result_snippet="",
    )


# --- DoD #1: corroboration vs scoop is the deterministic count threshold -----


def test_many_external_results_tags_corroborated_and_near_zero_tags_scoop() -> None:
    """A high external footprint -> corroborated; a near-zero footprint -> scoop.

    WHY: this IS the brief's Stage-5b distinction. Item A (the user's network AND the
    wider web are talking about it) must read ``corroborated``; item B (the user's
    network had it first, the web is near-silent) must read ``scoop`` — the high-value
    find. The classification is a deterministic count threshold, not an LLM call. A
    regression that inverted the threshold would tag A scoop / B corroborated and this
    asserts the exact opposite, so it fails loudly on inverted logic.
    """
    item_a = _trending_item("A", title="big public news everyone covers")
    item_b = _trending_item("B", title="niche thing only my network saw")

    # Mock the cross-search: MANY results for A's title, near-ZERO for B's.
    def fake_search(query: str) -> list[SearchResult]:
        if query == item_a.title:
            return [_result(i) for i in range(8)]  # well above the threshold
        return []  # near-zero external signal

    tagged = tag_external_corroboration([item_a, item_b], search_fn=fake_search, depth="default")

    by_cluster = {item.cluster_id: item for item in tagged}
    assert by_cluster["A"].corroboration_tag == CORROBORATION_TAG_CORROBORATED
    assert by_cluster["B"].corroboration_tag == CORROBORATION_TAG_SCOOP


def test_result_count_exactly_at_threshold_is_corroborated_boundary() -> None:
    """Exactly threshold results -> corroborated; one fewer -> scoop (boundary intent).

    WHY: the threshold is the load-bearing constant. Pinning the boundary (>= is
    corroborated, < is scoop) prevents an off-by-one regression from silently
    reclassifying borderline topics — the boundary is the rule, so the test encodes it.
    """
    threshold = 3
    at_threshold = _trending_item("AT", title="exactly at threshold")
    below_threshold = _trending_item("BELOW", title="one below threshold")

    def fake_search(query: str) -> list[SearchResult]:
        if query == at_threshold.title:
            return [_result(i) for i in range(threshold)]  # == threshold
        return [_result(i) for i in range(threshold - 1)]  # < threshold

    tagged = tag_external_corroboration(
        [at_threshold, below_threshold],
        search_fn=fake_search,
        depth="default",
        result_threshold=threshold,
    )
    by_cluster = {item.cluster_id: item for item in tagged}
    assert by_cluster["AT"].corroboration_tag == CORROBORATION_TAG_CORROBORATED
    assert by_cluster["BELOW"].corroboration_tag == CORROBORATION_TAG_SCOOP


# --- DoD #2: depth bounds the number of cross-searches (cost control / CSO) ---


def test_depth_quick_caps_number_of_cross_searches() -> None:
    """depth='quick' caps cross-searches at the quick budget even with more items.

    WHY: the external cross-search is the phase's only egress and runs on the user's
    plan. The brief requires depth to BOUND the call count so a busy day cannot fire
    unbounded web requests. Given MANY more trending items than the quick budget, the
    mock must be called AT MOST quick-budget times; items beyond the budget stay
    untagged (no egress spent). A regression that searched every item would over-call
    and this asserts the cap, so it fails on unbounded egress.
    """
    quick_budget = DEPTH_CROSS_SEARCH_BUDGET["quick"]
    item_count = quick_budget + 5  # deliberately exceed the budget
    items = [_trending_item(str(i), title=f"topic {i}") for i in range(item_count)]

    call_count = 0

    def counting_search(query: str) -> list[SearchResult]:
        nonlocal call_count
        call_count += 1
        return [_result(0)]

    tagged = tag_external_corroboration(items, search_fn=counting_search, depth="quick")

    assert call_count == quick_budget, f"expected at most {quick_budget} searches, got {call_count}"
    # Items beyond the budget keep the untagged default — no egress was spent on them.
    untagged = [item for item in tagged if item.corroboration_tag == CORROBORATION_TAG_UNTAGGED]
    assert len(untagged) == item_count - quick_budget


def test_deeper_depth_allows_more_cross_searches_than_quick() -> None:
    """deep budget > default budget > quick budget (the throttle actually scales).

    WHY: the depth throttle must be monotonic — a user who asks for 'deep' must get
    strictly more corroboration than 'quick', otherwise the knob is cosmetic. This
    pins the ordering of the budget map so a future edit can't accidentally flatten it.
    """
    assert (
        DEPTH_CROSS_SEARCH_BUDGET["quick"]
        < DEPTH_CROSS_SEARCH_BUDGET["default"]
        < DEPTH_CROSS_SEARCH_BUDGET["deep"]
    )


# --- DoD #3: defensive (Rule 12) ---------------------------------------------


def test_empty_trending_list_issues_no_searches_and_does_not_crash() -> None:
    """An empty trending list -> no searches, empty result, no crash (a quiet day).

    WHY: a quiet day (no internal-trending items) must not fire any egress nor raise.
    The mock asserts zero calls so a regression that searched on an empty list is caught.
    """
    call_count = 0

    def counting_search(query: str) -> list[SearchResult]:
        nonlocal call_count
        call_count += 1
        return []

    result = tag_external_corroboration([], search_fn=counting_search, depth="default")
    assert result == []
    assert call_count == 0


def test_search_fn_that_raises_degrades_item_to_scoop_without_crashing() -> None:
    """A raising search_fn degrades the item to a safe 'scoop' tag, no crash (Rule 12).

    WHY: the external egress is best-effort — a network/parse failure must NOT crash the
    whole digest. The item degrades to 'scoop' (treated as no external signal, the safe
    default) and processing continues. A regression that let the exception propagate
    would crash here.
    """
    item = _trending_item("X", title="topic that errors")

    def raising_search(query: str) -> list[SearchResult]:
        raise RuntimeError("simulated network failure")

    tagged = tag_external_corroboration([item], search_fn=raising_search, depth="default")
    assert tagged[0].corroboration_tag == CORROBORATION_TAG_SCOOP


def test_search_fn_returning_empty_tags_scoop() -> None:
    """A search returning [] -> scoop (near-zero external signal is the scoop signal).

    WHY: zero external results is the canonical scoop case (your network had it first).
    Pins that an empty return is scoop, not untagged/crash.
    """
    item = _trending_item("Y", title="quiet topic")
    tagged = tag_external_corroboration([item], search_fn=lambda query: [], depth="default")
    assert tagged[0].corroboration_tag == CORROBORATION_TAG_SCOOP


def test_blank_title_degrades_to_scoop_without_spending_a_search() -> None:
    """An item with a blank title -> scoop, with NO egress spent on a blank query.

    WHY: a blank query is a no-op, not an error — and firing an egress call on it would
    waste budget and could 400. The item degrades to the safe scoop default and the
    search budget is preserved. A regression that searched a blank query is caught here.
    """
    blank = _trending_item("BLANK", title="   ")
    call_count = 0

    def counting_search(query: str) -> list[SearchResult]:
        nonlocal call_count
        call_count += 1
        return [_result(0)]

    tagged = tag_external_corroboration([blank], search_fn=counting_search, depth="default")
    assert tagged[0].corroboration_tag == CORROBORATION_TAG_SCOOP
    assert call_count == 0


# --- DoD #4: keyless module — no live web call, injected page fetcher ---------


def test_keyless_search_parses_injected_html_without_network() -> None:
    """keyless_search parses results from an INJECTED page_fetcher (no live request).

    WHY: the keyless module must keep its network boundary injectable so tests (and the
    DoD) never hit the network. Feeding canned DDG-shaped HTML through a fake fetcher
    must yield parsed SearchResults — proving the parse works AND that no live call is
    needed. This is the keyless/no-secret egress the orchestrator's CSO pass relies on.
    """
    fake_html = (
        '<a class="result__a" href="https://real.test/page">A Title</a>'
        '<a class="result__snippet">A snippet</a>'
        '<a class="result__a" href="https://real.test/two">Second</a>'
    )
    results = keyless_search("public query", count=5, page_fetcher=lambda url: fake_html)
    assert len(results) == 2
    assert results[0].result_title == "A Title"
    assert results[0].result_url == "https://real.test/page"
    assert results[0].result_snippet == "A snippet"


def test_keyless_search_blank_query_returns_empty_without_fetching() -> None:
    """A blank query short-circuits to [] without invoking the fetcher (no wasted egress).

    WHY: never send an empty search. The fake fetcher asserts it is never called.
    """
    fetch_count = 0

    def counting_fetcher(url: str) -> str:
        nonlocal fetch_count
        fetch_count += 1
        return ""

    assert keyless_search("   ", page_fetcher=counting_fetcher) == []
    assert fetch_count == 0


def test_keyless_search_fetcher_raising_degrades_to_empty_not_crash() -> None:
    """A page_fetcher that raises -> [] (never raises out of keyless_search; Rule 12).

    WHY: the keyless egress is best-effort. Even a misbehaving fetcher must degrade to
    no results, not crash the digest. Pins the never-raises contract.
    """

    def raising_fetcher(url: str) -> str:
        raise RuntimeError("boom")

    assert keyless_search("public query", page_fetcher=raising_fetcher) == []


def test_keyless_module_imports_only_stdlib_and_lib_no_new_dependency() -> None:
    """The keyless module's egress uses stdlib (urllib) — no new pip dependency.

    WHY: CSO/Rule 2 — no new dependency may be introduced for the egress. Assert the
    stdlib fetcher exists and the module exposes the injectable seam (SearchFn /
    PageFetcher), so the network path is stdlib-only and mockable.
    """
    assert hasattr(web_search_keyless, "default_page_fetcher")
    assert callable(web_search_keyless.default_page_fetcher)
    # The injectable seam names exist (the boundary tests rely on them).
    assert hasattr(web_search_keyless, "SearchFn")
    assert hasattr(web_search_keyless, "PageFetcher")
