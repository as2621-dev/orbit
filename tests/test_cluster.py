"""DoD tests for overlap clustering (Phase 5 / Sub-phase 1, Stage 4).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does.
Clustering (Stage 4) is what turns a flat stream into the "Everyone's talking
about" overlap block while keeping a long-form episode a single viewing decision
(design decision 7). So the tests assert the product intents the brief depends on,
each constructed to FAIL on wrong business logic, not just "returns a list":

  1. Short-merge: three near-duplicate SHORT items on the same topic collapse into
     ONE cluster body. A regression that left them as 3 singletons (threshold too
     high, or the entity second pass dropped) would fail.
  2. Long-cross-link / never-shred: two LONG-FORM videos on the same topic stay TWO
     separate items, attached to the topic cluster as cross-links carrying their
     chapter ``deep_link`` INTACT. A regression that absorbed a long item into the
     merged body (``member_item_ids``), dropped one, or lost/rewrote a deep-link
     would fail.
  3. No false merge: an off-topic lone item forms its own singleton. A regression
     that over-merged unrelated items would fail.
  4. Defensive (Rule 12): empty input -> empty list; an empty-title item degrades
     gracefully (no crash).
  5. Determinism / no network (Rule 5): the cluster + fusion paths import ONLY
     stdlib + lib.dedupe/lib.fusion/lib.log — no embedding/network/LLM import exists.

The long/short distinction is injected as an explicit ``is_long_form`` predicate so
the tests pin the business rule deterministically (the production default treats an
item carrying chapters as long-form — Phase-2 chapterizes only > 1200s videos).
All inputs are constructed fixtures — no network/LLM/store in this module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Make ``scripts`` importable so ``from lib import ...`` resolves
# regardless of the working directory. Mirrors tests/test_rerank.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import cluster, fusion  # noqa: E402
from lib.cluster import cluster_overlaps  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402

# The explicit long-form predicate the tests inject: an item is a long-form episode
# when it carries chapters (mirrors the production default _is_long_form_by_chapters).
_IS_LONG_FORM = lambda item: bool(getattr(item, "chapters", None))  # noqa: E731


def _short_item(item_external_id: str, title: str, *, creator: str = "c1") -> RankableItem:
    """A SHORT YouTube-style item (no chapters -> not long-form)."""
    return RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name=f"channel {creator}",
        creator_external_id=creator,
        view_count=100,
        like_count=10,
        comment_count=1,
        upload_date="20260110",
    )


def _tweet_item(item_external_id: str, title: str, *, handle: str = "alice") -> RankableItem:
    """A SHORT X item (card_url set, no chapters -> short)."""
    return RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name=handle,
        creator_external_id=handle,
        view_count=5,
        like_count=10,
        comment_count=2,
        upload_date="",
        card_url=f"https://x.com/{handle}/status/{item_external_id}",
    )


def _chapter(title: str, start_seconds: float, deep_link: str) -> SimpleNamespace:
    """A Chapter stand-in (duck-typed: .title / .start_seconds / .deep_link)."""
    return SimpleNamespace(title=title, start_seconds=start_seconds, deep_link=deep_link)


def _long_item(
    item_external_id: str,
    title: str,
    chapters: list[SimpleNamespace],
    *,
    creator: str = "pod",
) -> RankableItem:
    """A LONG-FORM YouTube item: carries chapters (the long-form signal), deep-links intact."""
    return RankableItem(
        item_external_id=item_external_id,
        title=title,
        channel_name=f"channel {creator}",
        creator_external_id=creator,
        view_count=5000,
        like_count=400,
        comment_count=50,
        upload_date="20260110",
        chapters=chapters,
    )


# --- DoD #1 — short-merge: near-duplicate short items collapse into one cluster ---


def test_three_near_duplicate_short_items_collapse_into_one_cluster() -> None:
    """Three near-duplicate SHORT items on the same topic -> ONE cluster body of 3.

    WHY: the brief's Stage 4 "Everyone's talking about" block exists precisely to
    collapse the redundant chatter when multiple followed people react to the same
    thing — showing it once, not three times. If the threshold were too high or the
    merge logic broken, these three would stay 3 singletons and the overlap block
    would never form. This must fail on that regression, not merely assert a list.
    """
    items = [
        _short_item("a1", "Apple announces the new M5 chip for MacBook Pro"),
        _tweet_item("a2", "Apple just announced the new M5 chip in the MacBook Pro"),
        _short_item("a3", "The new Apple M5 chip MacBook Pro announcement is here", creator="c2"),
    ]
    clusters = cluster_overlaps(items, is_long_form=_IS_LONG_FORM)

    merged = [c for c in clusters if len(c.member_item_ids) >= 2]
    assert len(merged) == 1, f"expected one merged body, got sizes {[len(c.member_item_ids) for c in clusters]}"
    assert len(merged[0].member_item_ids) == 3, "all three near-duplicate short reactions must merge"
    assert set(merged[0].member_item_ids) == {"a1", "a2", "a3"}
    assert merged[0].cross_links == [], "short reactions carry no cross-links"


# --- DoD #2 — long-cross-link / never-shred (design decision 7) ---


def test_two_long_form_videos_cross_link_with_chapter_deep_links_intact() -> None:
    """Two LONG-FORM videos on one topic stay TWO items, cross-linked, deep-links INTACT.

    WHY: design decision 7 — a podcast/episode is ONE viewing decision and must
    never be shredded into the merged body. Two long episodes about the same topic
    must remain two SEPARATE items, attached to the short-chatter cluster as
    cross-links, each carrying the chosen chapter's ``deep_link`` byte-for-byte intact
    (the renderer drops the user into the exact moment). This fails if a long item is
    absorbed into the merged body (its id appears in member_item_ids), if only one of
    the two survives as a cross-link, or if any chapter deep-link is lost or rewritten.
    """
    deep_link_one = "https://www.youtube.com/watch?v=L1&t=600s"
    deep_link_two = "https://www.youtube.com/watch?v=L2&t=900s"
    short_chatter = [
        _tweet_item("s1", "Everyone talking about the OpenAI GPT-6 launch today"),
        _short_item("s2", "OpenAI GPT-6 launch is the big news today", creator="c2"),
    ]
    long_one = _long_item(
        "L1",
        "Deep dive on the OpenAI GPT-6 launch and what it means",
        [_chapter("GPT-6 launch recap", 600.0, deep_link_one)],
    )
    long_two = _long_item(
        "L2",
        "OpenAI GPT-6 launch breakdown full analysis",
        [_chapter("The GPT-6 launch", 900.0, deep_link_two)],
        creator="pod2",
    )
    clusters = cluster_overlaps([*short_chatter, long_one, long_two], is_long_form=_IS_LONG_FORM)

    # The two long videos must NOT be absorbed into any merged body.
    all_member_ids = {mid for c in clusters for mid in c.member_item_ids}
    assert "L1" not in all_member_ids and "L2" not in all_member_ids, "long-form must never be a merged member"

    # Both long videos must survive as separate cross-links.
    cross_by_episode = {x.episode_item_id: x for c in clusters for x in c.cross_links}
    assert "L1" in cross_by_episode and "L2" in cross_by_episode, "both long episodes must be cross-linked"

    # The chapter deep-links must survive byte-for-byte (never-shred).
    assert cross_by_episode["L1"].chapter_deep_link == deep_link_one
    assert cross_by_episode["L2"].chapter_deep_link == deep_link_two

    # They are cross-linked onto the SHORT chatter cluster, whose body excludes them.
    host = next(c for c in clusters if c.cross_links)
    assert len(host.member_item_ids) >= 1 and set(host.member_item_ids) <= {"s1", "s2"}


def test_unmatched_long_form_becomes_its_own_cross_link_cluster() -> None:
    """A long episode matching no short chatter is its own cross-link cluster (not merged).

    WHY: even with no surrounding short reactions, a long episode must remain a
    whole, surfaced unit — never dropped and never folded into an unrelated body.
    It forms a cluster with empty ``member_item_ids`` and itself as the lone
    cross-link (the representative is the episode), so the renderer still shows it
    with its deep-link. Fails if it vanishes or gets absorbed elsewhere.
    """
    long_solo = _long_item(
        "Lx",
        "A long retrospective on medieval cartography",
        [_chapter("Early maps", 120.0, "https://www.youtube.com/watch?v=Lx&t=120s")],
    )
    unrelated_short = _short_item("u1", "Stock market closes higher on rate-cut hopes")
    clusters = cluster_overlaps([unrelated_short, long_solo], is_long_form=_IS_LONG_FORM)

    solo = next((c for c in clusters if any(x.episode_item_id == "Lx" for x in c.cross_links)), None)
    assert solo is not None, "unmatched long episode must still surface as a cross-link cluster"
    assert solo.member_item_ids == [], "an unmatched long episode forms a cluster with no merged body"
    assert solo.representative_item_id == "Lx", "the episode itself represents its long-form-only cluster"


# --- DoD #3 — no false merge: off-topic lone item is its own singleton ---


def test_off_topic_lone_item_forms_its_own_singleton() -> None:
    """An off-topic item does NOT merge into an unrelated cluster.

    WHY: over-merging is as harmful as under-merging — collapsing unrelated items
    would put a basketball recap under an "Apple M5 chip" overlap block, destroying
    the signal. The off-topic item must stand alone as a size-1 body. Fails if a
    too-loose threshold (or buggy entity pass) swept it into the topic cluster.
    """
    items = [
        _short_item("a1", "Apple announces the new M5 chip for MacBook Pro"),
        _short_item("a2", "Apple M5 chip MacBook Pro announcement details", creator="c2"),
        _short_item("z1", "Lakers beat the Celtics in overtime thriller", creator="c3"),
    ]
    clusters = cluster_overlaps(items, is_long_form=_IS_LONG_FORM)

    off_topic = next(c for c in clusters if "z1" in c.member_item_ids)
    assert off_topic.member_item_ids == ["z1"], "the off-topic item must stay a singleton, not merge in"


# --- DoD #4 — defensive (Rule 12) ---


def test_empty_input_returns_empty_list_no_crash() -> None:
    """Empty input -> empty list (no crash). WHY: a quiet day must not raise (Rule 12)."""
    assert cluster_overlaps([], is_long_form=_IS_LONG_FORM) == []


def test_empty_title_item_degrades_gracefully() -> None:
    """An item with an empty title still clusters (as its own singleton), no crash.

    WHY: real feeds carry the occasional title-less item; the pipeline must degrade
    gracefully rather than crash mid-digest (Rule 12). An empty-title item simply
    matches nothing and stands alone.
    """
    items = [
        _short_item("e1", ""),
        _short_item("a1", "Apple announces the new M5 chip for MacBook Pro"),
    ]
    clusters = cluster_overlaps(items, is_long_form=_IS_LONG_FORM)

    assert len(clusters) == 2, "the empty-title item must form its own singleton, not merge or crash"
    empty = next(c for c in clusters if "e1" in c.member_item_ids)
    assert empty.member_item_ids == ["e1"]


# --- DoD #5 — determinism / no network / no embeddings (Rule 5) ---


def test_cluster_and_fusion_paths_import_only_stdlib_and_lib_helpers() -> None:
    """The cluster + fusion paths import NO embedding/network/LLM module — lexical-only (Rule 5).

    WHY: the resolved Q1 decision is that clustering is 100% lexical with no
    embedding model, network, or LLM call. The cheapest structural guarantee is that
    no such import exists across the cluster path (cluster.py + fusion.py). This fails
    the moment someone wires an embedding/HTTP/LLM dependency in, catching the
    regression at the boundary rather than via a live call in a test.
    """
    forbidden = (
        "import requests",
        "import httpx",
        "import urllib",
        "import numpy",
        "sentence_transformers",
        "import openai",
        "anthropic",
        "import torch",
        "import socket",
    )
    for module_name in ("cluster.py", "fusion.py"):
        source = (SCRIPTS_DIR / "lib" / module_name).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{module_name} must not import {needle!r} (clustering is lexical-only)"

    assert hasattr(cluster, "cluster_overlaps")
    assert hasattr(fusion, "fuse_topic_group")
    assert cluster.SIMILARITY_THRESHOLD == 0.48
    assert cluster.ENTITY_OVERLAP_THRESHOLD == 0.45


def test_long_form_same_topic_merge_into_one_cluster_post_reorder() -> None:
    """Post-reorder, same-topic long-form videos merge into ONE cluster (locked decision 2).

    WHY: the user does NOT want 5 separate summaries of one release. Because chapterization
    now happens AFTER clustering, items reach the clusterer with NO chapters, so the default
    long-form predicate treats them as mergeable — five near-identical long-form videos
    collapse into a single cluster whose ``all_member_item_ids`` carries the full membership
    the crown stage ranks (so it can pick ONE winner and footnote the other four). A
    regression that kept long-form videos as 5 separate clusters fails here.
    """
    items = [
        RankableItem(
            item_external_id=f"v{i}",
            title="New Anthropic model released today",
            channel_name=f"channel {i}",
            creator_external_id=f"c{i}",
            view_count=100,
            like_count=1,
            comment_count=0,
            upload_date="20260101",
            duration=1800,  # long-form by duration, but NO chapters at cluster time
            chapters=[],
        )
        for i in range(5)
    ]

    clusters = cluster_overlaps(items)  # production DEFAULT predicate (no injected is_long_form)

    assert len(clusters) == 1, "five same-topic long-form videos must collapse into one cluster"
    assert sorted(clusters[0].all_member_item_ids) == [f"v{i}" for i in range(5)]
