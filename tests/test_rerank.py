"""DoD tests for weighted derank scoring (Phase 3 / Sub-phase 1).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Derank (Stage 6a) is the thumb-on-the-scale that decides what floats to Hero and
what sinks to "they also posted" — so the tests assert the three intents the product
depends on (the sub-phase Definition of Done), each constructed so it FAILS on wrong
logic, not just "returns a sorted list":

  1. Priority thumb-on-scale: two items with IDENTICAL raw engagement but DIFFERENT
     creator priority_weight -> the higher-weight creator sorts first. A regression
     that ignored priority_weight (or applied it the wrong way) would tie or invert.
  2. Engagement is RELATIVE to the creator's OWN baseline: an item far above its
     creator's LOW baseline outranks an item with HIGHER RAW engagement that is
     normal-for-its-creator. A regression that ranked on raw views would invert this.
  3. Uniqueness boost: a high-priority creator's UNIQUE item with LOW engagement is
     NOT bottom-ranked. A regression that dropped the priority-scaled floor would
     bury the lone sharp take.

Plus defensive coverage (Rule 12): empty/garbage upload_date, creator absent from
weights, missing engagement counts, and an empty item list must degrade gracefully
and never crash. All inputs are constructed fixtures — there is no network/LLM/store
in this module (scoring is pure math, Rule 5).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

# Make ``scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_chapterize.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import rerank  # noqa: E402
from lib.rerank import RankableItem, ScoredItem  # noqa: E402
from lib.youtube_yt import Upload  # noqa: E402

# A fixed "now" so recency decay is deterministic across runs.
REFERENCE_DATE = date(2026, 1, 10)


def _config(creator_weights: dict[str, float] | None = None) -> SimpleNamespace:
    """A minimal OrbitConfig stand-in exposing only ``creator_weights`` (what score reads)."""
    return SimpleNamespace(creator_weights=creator_weights or {})


def _item(
    item_external_id: str,
    creator_external_id: str,
    *,
    view_count: int | None = None,
    like_count: int | None = None,
    comment_count: int | None = None,
    upload_date: str = "20260110",
) -> RankableItem:
    """Construct a RankableItem fixture directly (no Upload/network needed)."""
    return RankableItem(
        item_external_id=item_external_id,
        title=f"title {item_external_id}",
        channel_name=f"channel {creator_external_id}",
        creator_external_id=creator_external_id,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        upload_date=upload_date,
    )


def _rank(items: list[RankableItem], config: SimpleNamespace) -> list[ScoredItem]:
    return rerank.derank_items(items, config, reference_date=REFERENCE_DATE)


# --- DoD #1 — priority_weight is the thumb on the scale ---------------------


def test_identical_engagement_higher_priority_sorts_first() -> None:
    """Two items, IDENTICAL raw engagement, DIFFERENT priority_weight -> higher first.

    WHY: priority_weight is the user's explicit thumb on the ranking scale
    (api-contracts derank contract). With every other signal held identical — same
    views/likes/comments, same upload_date, each creator alone so relative-engagement
    is equal — the ONLY differentiator is the creator's priority_weight. The
    high-weight creator MUST sort first. A regression that ignored the weight would
    tie (and break on the deterministic id tiebreak), and one that inverted it would
    fail outright. This is the "priority to the creator" intent, not "returns sorted".
    """
    high = _item("v_high", "UC_high", view_count=1000, like_count=50, comment_count=5)
    low = _item("v_low", "UC_low", view_count=1000, like_count=50, comment_count=5)
    config = _config({"UC_high": 2.0, "UC_low": 1.0})

    ranked = _rank([low, high], config)

    assert [scored.item.item_external_id for scored in ranked] == ["v_high", "v_low"]
    assert ranked[0].score > ranked[1].score, "higher priority_weight must yield a strictly higher score"


# --- DoD #2 — engagement RELATIVE to the creator's own baseline -------------


def test_breakout_vs_own_low_baseline_beats_higher_raw_but_normal_item() -> None:
    """An item far above its creator's LOW baseline beats a higher-RAW but normal item.

    WHY: brief Stage 6 ranks engagement RELATIVE to the creator's own baseline, not on
    raw views — so a small creator's breakout (a sharp departure from their norm) is
    surfaced over a big creator's perfectly ordinary upload, even though the big
    creator's RAW view count is higher. We build it so RAW order and RELATIVE order
    DISAGREE: the breakout item has FEWER raw views than the normal item, yet must
    rank ABOVE it. A regression that scored on raw engagement would invert this.

    Construction (priority held equal at 1.0, recency held equal):
      - Creator LOW posts mostly tiny items (10 views) and one breakout (200k views).
        Its batch-median baseline is ~the tiny level, so the breakout is far above it.
      - Creator HIGH always posts ~1M-view items; one such 1M item is normal-for-it,
        so its relative engagement is ~0.
    The breakout (200k raw) must outrank the HIGH creator's normal item (1M raw).
    """
    config = _config({"UC_low": 1.0, "UC_high": 1.0})
    # Creator LOW's own baseline: three tiny items establish a low median.
    low_tiny_a = _item("low_tiny_a", "UC_low", view_count=10)
    low_tiny_b = _item("low_tiny_b", "UC_low", view_count=10)
    low_tiny_c = _item("low_tiny_c", "UC_low", view_count=10)
    # Creator LOW's BREAKOUT — far above its own baseline, but only 200k raw views.
    low_breakout = _item("low_breakout", "UC_low", view_count=200_000)
    # Creator HIGH always posts ~1M; these establish a HIGH baseline, and the item we
    # compare against is normal-for-HIGH (1M) — higher RAW than the breakout's 200k.
    high_normal_a = _item("high_normal_a", "UC_high", view_count=1_000_000)
    high_normal_b = _item("high_normal_b", "UC_high", view_count=1_000_000)
    high_normal_target = _item("high_normal_target", "UC_high", view_count=1_000_000)

    items = [
        low_tiny_a,
        low_tiny_b,
        low_tiny_c,
        low_breakout,
        high_normal_a,
        high_normal_b,
        high_normal_target,
    ]
    ranked = _rank(items, config)
    ranks = {scored.item.item_external_id: position for position, scored in enumerate(ranked)}

    # Sanity: the breakout has FEWER raw views than the normal target — so a raw-views
    # ranker would put the target first. Relative-to-baseline ranking must NOT.
    assert low_breakout.view_count < high_normal_target.view_count
    assert ranks["low_breakout"] < ranks["high_normal_target"], (
        "an item far above its creator's own baseline must outrank a higher-RAW but normal-for-its-creator item"
    )


# --- DoD #3 — uniqueness boost keeps a lone high-priority take off the bottom ---


def test_high_priority_unique_low_engagement_item_not_bottom_ranked() -> None:
    """A high-priority creator's UNIQUE low-engagement item is NOT bottom-ranked.

    WHY: a lone sharp take from a creator the user trusts must not sink just because
    it has few views (uniqueness boost, tied to priority_weight). We give a HIGH
    priority creator a single item with NEAR-ZERO engagement, alongside several
    ordinary (priority 1.0) items that DO have engagement. The unique high-priority
    item must NOT land last. A regression that dropped the priority-scaled floor (so
    the item scored purely on its near-zero engagement) would bury it at the bottom.
    """
    config = _config({"UC_trusted": 3.0})
    # The lone sharp take: trusted creator, almost no engagement.
    unique_high = _item("unique_high", "UC_trusted", view_count=5, like_count=0, comment_count=0)
    # A crowd of ordinary, unweighted (priority 1.0) items WITH real engagement.
    ordinary = [
        _item(f"ordinary_{index}", f"UC_ord_{index}", view_count=5000, like_count=200, comment_count=20)
        for index in range(5)
    ]

    ranked = _rank([unique_high, *ordinary], config)
    ranks = {scored.item.item_external_id: position for position, scored in enumerate(ranked)}
    last_position = len(ranked) - 1

    assert ranks["unique_high"] != last_position, (
        "a high-priority creator's unique low-engagement item must not be bottom-ranked"
    )


# --- Nothing dropped: rank controls density, never inclusion ----------------


def test_derank_drops_nothing_and_sorts_descending() -> None:
    """Every item that goes in comes out, sorted strictly descending by score.

    WHY: rank controls DENSITY, never inclusion (api-contracts derank contract). A
    regression that filtered low scorers would silently lose items the "they also
    posted" strip depends on. Also pins the contract Sub-phases 2-4 build on: output
    length == input length, and order is non-increasing by score.
    """
    config = _config({"UC_a": 1.5})
    items = [
        _item("a", "UC_a", view_count=100),
        _item("b", "UC_b", view_count=100),
        _item("c", "UC_c", view_count=999999),
    ]

    ranked = _rank(items, config)

    assert len(ranked) == len(items), "nothing may be dropped — rank controls density, not inclusion"
    assert {scored.item.item_external_id for scored in ranked} == {"a", "b", "c"}
    scores = [scored.score for scored in ranked]
    assert scores == sorted(scores, reverse=True), "items must be sorted descending by score"


# --- Defensive / edge cases (Rule 12 — degrade, never crash) ----------------


def test_empty_item_list_returns_empty_without_crashing() -> None:
    """An empty batch returns an empty list (the first real run may have no new items)."""
    assert rerank.derank_items([], _config(), reference_date=REFERENCE_DATE) == []


def test_creator_absent_from_weights_defaults_to_neutral() -> None:
    """A creator not in creator_weights gets the neutral default weight (no crash).

    WHY: most creators won't have an explicit weight; absence must mean "no thumb on
    the scale" (1.0), not a KeyError. Two items with identical engagement, one weighted
    and one absent -> the weighted one ranks first, the absent one still scores fine.
    """
    config = _config({"UC_weighted": 2.0})
    weighted = _item("weighted", "UC_weighted", view_count=1000, like_count=10)
    unweighted = _item("unweighted", "UC_missing", view_count=1000, like_count=10)

    ranked = _rank([unweighted, weighted], config)

    assert ranked[0].item.item_external_id == "weighted"
    # The absent-weight item still produced a finite, positive score (neutral weight).
    unweighted_score = next(s.score for s in ranked if s.item.item_external_id == "unweighted")
    assert unweighted_score > 0.0


def test_missing_engagement_counts_do_not_crash() -> None:
    """Items with all-None engagement counts score without crashing (log1p_safe -> 0)."""
    config = _config()
    no_engagement = _item("none", "UC_x", view_count=None, like_count=None, comment_count=None)

    ranked = _rank([no_engagement], config)

    assert len(ranked) == 1
    assert isinstance(ranked[0].score, float)


def test_empty_and_garbage_upload_date_degrade_to_neutral_recency() -> None:
    """Empty / malformed upload_date falls back to neutral recency, never crashes.

    WHY: yt-dlp sometimes omits or mangles upload_date; an unparseable date must not
    crash the rank nor bury the item — it degrades to a neutral mid recency. We assert
    both the empty and the garbage cases equal the documented neutral decay, and a
    valid same-day date scores strictly fresher than the neutral fallback.
    """
    assert rerank.recency_decay("", reference_date=REFERENCE_DATE) == rerank.RECENCY_NEUTRAL_DECAY
    assert rerank.recency_decay("not-a-date", reference_date=REFERENCE_DATE) == rerank.RECENCY_NEUTRAL_DECAY
    assert rerank.recency_decay("20269999", reference_date=REFERENCE_DATE) == rerank.RECENCY_NEUTRAL_DECAY
    # A same-day upload is maximally fresh (1.0) and beats the neutral fallback.
    assert rerank.recency_decay("20260110", reference_date=REFERENCE_DATE) == 1.0
    assert 1.0 > rerank.RECENCY_NEUTRAL_DECAY

    # And an item carrying a garbage date still scores (no crash) inside the pipeline.
    garbage = _item("garbage_date", "UC_x", view_count=100, upload_date="20269999")
    ranked = _rank([garbage], _config())
    assert len(ranked) == 1


def test_recency_decay_halves_at_half_life() -> None:
    """An item one half-life old has recency ~0.5 (encodes the decay shape, not magic).

    WHY: recency_decay's half-life is the documented contract (older halves). Pinning
    it guards against a silent change to the decay base/half-life that would reshuffle
    rankings. RECENCY_HALF_LIFE_DAYS days before the reference -> decay 0.5.
    """
    half_life_days = int(rerank.RECENCY_HALF_LIFE_DAYS)
    older_date = date(REFERENCE_DATE.year, REFERENCE_DATE.month, REFERENCE_DATE.day - half_life_days)
    decay = rerank.recency_decay(older_date.strftime("%Y%m%d"), reference_date=REFERENCE_DATE)
    assert abs(decay - 0.5) < 1e-9


# --- from_parts adapter — the item-ingestion API later phases slot into ------


def test_from_parts_builds_item_from_real_upload_fields() -> None:
    """RankableItem.from_parts reads the REAL Upload field names (the ingestion API).

    WHY: Phase 4 (X source) and the renderer build items through this adapter, so it
    must map the actual lib.youtube_yt.Upload fields (video_id -> item_external_id,
    view/like/comment counts, upload_date) and carry classification + chapters
    through verbatim. A regression in the field mapping would silently mis-rank every
    item. We pass a real Upload and assert the mapping.
    """
    upload = Upload(
        video_id="vid_abc",
        title="A real talk",
        description="desc",
        upload_date="20260105",
        view_count=12345,
        like_count=678,
        comment_count=9,
        duration=1800,
        channel_name="Real Channel",
    )
    sentinel_classification = object()
    sentinel_chapters = [object(), object()]

    item = RankableItem.from_parts(
        upload,
        sentinel_classification,
        sentinel_chapters,
        creator_external_id="UC_real",
    )

    assert item.item_external_id == "vid_abc"
    assert item.title == "A real talk"
    assert item.channel_name == "Real Channel"
    assert item.creator_external_id == "UC_real"
    assert item.view_count == 12345
    assert item.like_count == 678
    assert item.comment_count == 9
    assert item.upload_date == "20260105"
    assert item.classification is sentinel_classification
    assert item.chapters == sentinel_chapters


def test_from_parts_defaults_chapters_and_creator_id() -> None:
    """from_parts tolerates None chapters and an unset creator id (first-cut callers)."""
    upload = Upload(
        video_id="vid_min",
        title="t",
        description="",
        upload_date="",
        view_count=None,
        like_count=None,
        comment_count=None,
        duration=None,
        channel_name="C",
    )

    item = RankableItem.from_parts(upload)

    assert item.item_external_id == "vid_min"
    assert item.chapters == []
    assert item.creator_external_id == ""


def test_raw_popularity_term_helps_high_traffic_item() -> None:
    """A high-view item outranks an identical low-view one even when BOTH are lone-creator.

    WHY: locked decision 6 added a raw-popularity term. With each item the only one from
    its creator, relative-engagement is 0 for both, so WITHOUT the raw term they would tie
    and order by id. We give the high-view item the id ("zzz_high") that LOSES an
    id-tiebreak; if it still ranks first, the raw-popularity term is doing the work.
    """
    config = _config({})
    high = _item("zzz_high", "UCa", view_count=100_000, like_count=5_000, comment_count=500)
    low = _item("aaa_low", "UCb", view_count=10, like_count=0, comment_count=0)
    scored = _rank([high, low], config)
    assert scored[0].item.item_external_id == "zzz_high", "raw popularity must lift the high-traffic item"


def test_raw_popularity_does_not_dominate_priority() -> None:
    """A trusted creator's low-view item still beats a low-priority creator's viral one.

    WHY: the raw-popularity term must HELP without letting a mega-channel dominate. With
    priority 3.0 vs 1.0, the small trusted item must still rank first despite the other's
    huge raw counts — proving RAW_POPULARITY_WEIGHT is subordinate to priority.
    """
    config = _config({"UC_trusted": 3.0, "UC_big": 1.0})
    trusted_small = _item("t", "UC_trusted", view_count=10, like_count=1, comment_count=0)
    big_popular = _item("b", "UC_big", view_count=1_000_000, like_count=80_000, comment_count=9_000)
    scored = _rank([trusted_small, big_popular], config)
    assert scored[0].item.item_external_id == "t", "priority must still dominate raw popularity"


def test_winner_score_biases_toward_longer_duration() -> None:
    """winner_score prefers the longer video among otherwise-equal cluster members.

    WHY: locked decision 3 — the cluster winner (the one that gets the full summary) is the
    one that "covers the most information", proxied by duration. Two same-creator, same-
    engagement items differ only in duration; the longer must score higher for selection.
    """
    config = _config({})
    short = _item("s", "UC1", view_count=1_000, like_count=50, comment_count=5)
    short.duration = 600
    long_video = _item("l", "UC1", view_count=1_000, like_count=50, comment_count=5)
    long_video.duration = 3_600
    baselines = rerank.compute_creator_engagement_baselines([short, long_video])
    score_short = rerank.winner_score(short, config, creator_baselines=baselines, reference_date=REFERENCE_DATE)
    score_long = rerank.winner_score(long_video, config, creator_baselines=baselines, reference_date=REFERENCE_DATE)
    assert score_long > score_short, "the longer video must win the crown among equals"


def _run_all_standalone() -> int:
    """Run every ``test_*`` function in this module without pytest. Returns exit code."""
    test_functions = [
        value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)
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
