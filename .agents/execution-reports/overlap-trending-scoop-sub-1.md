# Execution report — Phase 5, Sub-phase 1: Cluster overlaps (short-merge, long-cross-link)

**Status:** SUCCESS
**Date:** 2026-06-18

## What was implemented

Lifted (adapted, not copied — cjk/schema deps removed) the last30days lexical
clustering machinery into Orbit and layered the Orbit-specific design-decision-7
fusion (short-merge vs long-cross-link) on top. Three new lib modules + one test
file, split by responsibility:

- **`dedupe.py`** — pure lexical similarity primitives: `normalize_text`,
  char-trigram n-grams (`get_ngrams`), `jaccard_similarity`, stopword-filtered
  `token_jaccard`, `hybrid_similarity`, the cached `PreparedText` class, and
  `prepared_similarity`. The reference's `cjk.segment` is replaced by a plain
  whitespace split (English-first; documented as a future enhancement). No
  schema/SourceItem coupling — operates on raw strings.
- **`fusion.py`** — the Orbit rule. Defines `CrossLink` (the never-shred link into a
  long-form episode's relevant chapter) and `TopicGroup`, plus
  `fuse_topic_group(group, is_long_form)` which splits a topic group into short
  members (merged body) and cross-links (one per long-form episode that carries
  chapters). The relevant chapter is picked deterministically by
  `hybrid_similarity(representative_text, chapter.title)`, falling back to the first
  chapter — so a long-form episode with chapters always yields a real deep-link.
- **`cluster.py`** — entity helpers (`_extract_entities`, `_entity_overlap`
  overlap-coefficient), the greedy single-leader pass (`_greedy_groups`,
  threshold 0.48), the entity-overlap second-pass merge (`_merge_entity_groups`,
  threshold 0.45, max group size 3), the `Cluster` dataclass, and the
  `cluster_overlaps(items, config=None, *, is_long_form=...)` driver. Groups ALL
  items (short + long) lexically first, then fuses each group. Deterministic: items
  processed in stable `item_external_id` order; clusters sorted by descending body
  size then representative id; ids assigned `cluster-N` after the final sort.
- **`tests/test_cluster.py`** — 7 DoD tests (see below).

All three modules carry the sys.path-insert import header matching rerank.py /
chapterize.py so `from lib import ...` resolves both as a package member and when run
from the scripts dir.

## Files created (absolute paths)

- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/dedupe.py`
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/fusion.py`
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/cluster.py`
- `/Users/asheshsrivastava/frommyfeed/tests/test_cluster.py`

## Long-form-detection design decision + WHY

`RankableItem` carries no `duration`/`is_long_form` field, and rerank.py is
out-of-bounds for this sub-phase. So `cluster_overlaps` accepts an **injectable
predicate** `is_long_form: Callable[[RankableItem], bool]` with a default of
`_is_long_form_by_chapters` (`bool(item.chapters)`).

**WHY chapters as the proxy:** Phase-2's `chapterize_episode` returns `[]` for any
item <= 1200s and a non-empty list only for long-form videos. Short videos and X
tweets never carry chapters. So "has chapters" is a reliable, zero-coupling long-form
signal that needs no new field on `RankableItem`. Tests inject an explicit predicate
so the long/short distinction is deterministic and does not silently depend on the
heuristic.

**Handoff note for sub-phases 2/4:** A future explicit `duration: int | None` (or
`is_long_form: bool`) field on `RankableItem` would be cleaner than the chapters
proxy — e.g. a long-form video whose chapterization failed (no cues) would currently
be treated as short. Sub-phase 4 has `Upload.duration` in hand when it builds items,
so if it adds such a field it should pass
`is_long_form=lambda i: i.duration and i.duration > 1200` into `cluster_overlaps`.
This is a NON-BLOCKING refinement; the chapters proxy is correct for every
chapterized item today. (I did NOT edit rerank.py.)

## Divergences

- A pre-existing `cluster.py` and `test_cluster.py` from an earlier attempt used a
  different `Cluster` shape (`.label`/`.members`/`.size`, a whole-item `cross_links`
  list, and a `getattr(item, "is_long_form")` attribute contract). Per the task spec
  (authoritative on the `Cluster`/`CrossLink` shape sub-phase 2 depends on) and
  Rule 7 (pick one, surface the conflict), I replaced `cluster.py` with the
  spec-compliant version (`member_item_ids`/`representative_item_id`/typed
  `cross_links: list[CrossLink]`/`source_diversity`, injectable predicate). The
  current `test_cluster.py` is aligned to the spec API and tests the new shape
  correctly, so it was kept (it adds two useful cases: unmatched-long-form-only and
  empty-title degradation). Nothing else in the codebase imported the old
  `cluster.py` (grep-verified), so no callers broke.
- The reference's source/Polymarket merge guards in `_merge_entity_clusters` were
  dropped — Orbit has no per-source merge restriction (it is source-agnostic).
- The reference's MMR representative selection was not lifted; Orbit picks a single
  representative by creator priority_weight (the brief's thumb-on-the-scale), which is
  simpler and matches how rerank.py already ranks (Rule 2 — no speculative MMR).

## Self-review findings + fixes

1. **Doctest in `_extract_entities`** asserted `m5` would be extracted, but the
   `len(word) <= 2` filter drops it (runs before the digit check). Fixed the docstring
   example to a correct case (`"Apple chip benchmark review 2026"`).
2. **Doctest in `cluster_overlaps`** failed because the function emits a JSON log line
   to stdout. Marked the call lines `# doctest: +SKIP` (matching the chapterize.py /
   rerank.py convention).
3. **Ruff: unused `field` import in fusion.py** — `CrossLink`/`TopicGroup` use no
   `default_factory`. Removed `field` from the dataclasses import.
4. **Ruff: unused `Cluster` import in test_cluster.py** — removed.
5. **Ruff format** reformatted cluster.py (vertical stopword set). Applied.

## Validation (exact commands + outputs)

- `python3 -c "import ast; ast.parse(...)"` → `ast ok`
- `cd skills/orbit/scripts && python3 -c "from lib import cluster, dedupe, fusion"` → `import ok`
- `uv run --with pytest pytest tests/test_cluster.py -q` → **7 passed in 0.01s**
- `uv run --with pytest pytest tests/ -q` → **90 passed in 1.59s** (83 prior + 7 new, 0 failures)
- `python3 -m doctest lib/dedupe.py lib/fusion.py lib/cluster.py` → **DOCTESTS PASS**
- `ruff check --line-length 120` (all 4 files) → **All checks passed!**
- `ruff format --check --line-length 120` → all formatted
- Line counts: cluster.py 485, dedupe.py 239, fusion.py 196, test_cluster.py 283 — all < 500.

## Definition of done (per the 4 criteria)

1. Three near-duplicate SHORT tweets collapse into ONE cluster — **PASS**
   (`test_three_near_duplicate_short_items_collapse_into_one_cluster`).
2. Two long-form videos stay TWO separate items, cluster cross-links BOTH with chapter
   deep-links/timestamps intact — **PASS**
   (`test_two_long_form_videos_cross_link_with_chapter_deep_links_intact`; also
   `test_unmatched_long_form_becomes_its_own_cross_link_cluster`).
3. Off-topic lone item forms its own singleton (no false merge) — **PASS**
   (`test_off_topic_lone_item_forms_its_own_singleton`).
4. No embedding model / network / LLM in the cluster path — **PASS**, asserted
   structurally (`test_cluster_and_fusion_paths_import_only_stdlib_and_lib_helpers`
   bans requests/httpx/urllib/numpy/sentence_transformers/openai/anthropic/torch/socket
   imports) and behaviorally (no I/O; pure given inputs). Stdlib only: `re`,
   `dataclasses`, `typing`, `pathlib`, `sys` + `lib.dedupe`/`lib.fusion`/`lib.log`.

## Concerns / handoff for Sub-phase 2

Sub-phase 2 (`compute_internal_trending(clusters, store) -> list[TrendingItem]`)
consumes the `list[Cluster]` this sub-phase returns. The EXACT shapes:

```python
@dataclass
class Cluster:
    cluster_id: str                       # "cluster-N", deterministic order
    member_item_ids: list[str]            # SHORT members' item_external_ids (the body;
                                          #   may be [] for a long-form-only topic)
    representative_item_id: str           # highest-priority short member id, else the
                                          #   first cross-linked episode id
    cross_links: list[CrossLink]          # one per long-form episode on this topic
    source_diversity: int                 # count of DISTINCT creator_external_id across
                                          #   short members AND cross-linked episodes

@dataclass
class CrossLink:
    episode_item_id: str                  # the long-form episode's item_external_id
                                          #   (the episode REMAINS its own separate item)
    chapter_title: str
    chapter_start_seconds: float          # the chapter's start offset (real cue/creator offset)
    chapter_deep_link: str                # watch?v=ID&t=Ns — the never-shred deep-link

# Signature:
def cluster_overlaps(
    items: list[RankableItem],
    config: Any = None,                   # read only for creator_weights (representative pick)
    *,
    is_long_form: Callable[[RankableItem], bool] = _is_long_form_by_chapters,
) -> list[Cluster]: ...
```

For Sub-phase 2's velocity:
- **Cluster size** = `len(cluster.member_item_ids)` — the count of short reactions
  merged. Use as the convergence proxy.
- **`source_diversity`** = number of distinct creators (short + cross-linked) on the
  topic. This is the "multiple followed creators converging" signal: a cluster with
  `source_diversity == 3` should outrank a single-creator cluster of equal raw
  engagement (Sub-phase 2's DoD #1). NOTE: `source_diversity` counts cross-linked
  long-form creators too, whereas `len(member_item_ids)` counts ONLY short members —
  so a long-form-only cluster has `member_item_ids == []` but `source_diversity >= 1`.
  Sub-phase 2 should decide which proxy (size vs diversity) it weights and document it.
- **Cross-links carry the deep-link** for the right-rail render in Sub-phase 4 — do not
  drop them when mapping a cluster to a `TrendingItem`.
- The `representative_item_id` is always set (never empty), even for a long-form-only
  cluster, so Sub-phase 2/4 always has an item to headline/link.

Non-blocking: the chapters-based long-form proxy could misclassify a long video whose
chapterization yielded no chapters as "short" — see the long-form-detection handoff
note above for the clean `duration`-field fix when Sub-phase 4 builds items.
