# Phase 5: Overlap clustering, internal/external trending, and scoop detection

**Milestone:** M3 — Overlap, trending & scoop
**Status:** Not started
**Estimated effort:** L

## Goal
Across the unified YouTube+X item stream, Orbit clusters overlaps (short-form reactions merge into one block; long-form sharing a topic cross-links by timestamp without merging), computes internal-network trending (baseline-relative velocity), tags external corroboration vs scoop via a light cross-search, and flags anomaly/scoop accounts loudly — populating the overlap block, right-rail trending, and scoops strip in the existing render.

## Resolved open questions folded into this phase
- **Embedding source for clustering (master-plan Q1) — RESOLVED: NO embedding model.** Inspection of `cluster.py`/`fusion.py`/`dedupe.py` found clustering is 100% lexical: `prepared_similarity = max(char-trigram Jaccard, stopword-filtered token Jaccard)`, greedy single-leader clustering (thresholds 0.42 breaking-news / 0.48 default), plus a second-pass entity-overlap merge (`overlap coefficient |A∩B|/min(|A|,|B|)`, threshold 0.45) for short-vs-long matching. There are NO embedding imports (no sentence-transformers/OpenAI/numpy-cosine). **Decision: Orbit reuses the lexical machinery as-is** — no embedding endpoint, no new dependency, no host-LLM embedding call. The entity-overlap second pass is exactly the tool for matching short tweet reactions against long video titles. If pure-lexical proves too weak in real use, an embedding pass is a future enhancement, not an M3 requirement (Rule 2 — don't add it speculatively).
- **Short-merge vs long-cross-link (design decision 7).** Long-form (the duration > 1200s items from Phase 2) is NEVER absorbed into a topic cluster; instead the overlap block CROSS-LINKS into each episode's chapter timestamp. Short items (tweets, short videos) on the same thing MERGE into one "Everyone's talking about" block. This is the Orbit-specific adaptation of the reference's uniform clustering.
- **Trending is built, not lifted.** `signals.py` has NO time-windowed velocity / baseline / anomaly logic — it's static per-item scoring. So internal velocity (Sub-phase 2) and anomaly/scoop (Sub-phase 4) are BUILT on top, reusing only the reference's `log1p_safe` compression, per-actor reference-normalization idea, and recency decay. The `seen.first_seen_at` history + engagement snapshots provide the time dimension.

## Sub-phases

### Sub-phase 1: Cluster overlaps — short-merge, long-cross-link (Stage 4)
- **Files touched:** `skills/orbit/scripts/lib/cluster.py`, `skills/orbit/scripts/lib/dedupe.py`, `skills/orbit/scripts/lib/fusion.py`
- **What ships:** Lifted lexical clustering (`prepared_similarity`, greedy single-leader, entity-overlap second pass) adapted into `cluster_overlaps(items) -> list[Cluster]` with the Orbit rule: items flagged `is_long_form` (duration > 1200s) are NEVER merged into a cluster body — they are attached as cross-links (the cluster references the episode + the relevant chapter timestamp). Short items on the same topic merge into one cluster representing the "Everyone's talking about" block. Cluster text uses title+highlights (video) / tweet text (X), all lexical — no embeddings.
- **Definition of done:** A test with three near-duplicate short tweets asserts they collapse into ONE cluster (short-merge intent). A test with two long-form videos on the same topic asserts they remain TWO separate items but the cluster cross-links BOTH with their chapter timestamps intact (long-cross-link, never-shred intent per design decision 7 — the deep-link must survive). A test asserts an off-topic lone item forms its own singleton (no false merge). No embedding model invoked (assert no network/LLM call in the cluster path).
- **Dependencies:** none (consumes Phase 4's unified item stream)

### Sub-phase 2: Internal-network trending — baseline-relative velocity (Stage 5a)
- **Files touched:** `skills/orbit/scripts/lib/signals.py`, `skills/orbit/scripts/lib/trending.py`
- **What ships:** `compute_internal_trending(clusters, store) -> list[TrendingItem]` measuring network velocity: multiple followed creators converging on the same cluster (cluster size as a velocity proxy) AND a single item spiking relative to the creator's OWN baseline (engagement vs the creator's recent median, using `log1p_safe` compression + per-creator reference normalization adapted from `signals.py`). Uses `seen.first_seen_at` and stored engagement to get the time dimension the reference lacks. Outputs a ranked trending list for the right-rail. Deterministic math (Rule 5).
- **Definition of done:** A test asserts a cluster touched by 3 different followed creators ranks above a single-creator cluster of equal raw engagement (convergence/velocity intent). A test asserts an item at 5× the creator's own baseline engagement ranks as trending while an item at the creator's normal level does not — even if the normal-level item has higher RAW engagement (baseline-relative intent, the core brief Stage 5 distinction, not raw popularity). No LLM/network call.
- **Dependencies:** Sub-phase 1 (needs clusters)

### Sub-phase 3: External trending — corroboration vs scoop tagging (Stage 5b)
- **Files touched:** `skills/orbit/scripts/lib/trending.py`, `skills/orbit/scripts/lib/web_search_keyless.py`
- **What ships:** A light external cross-search (lift the reference's keyless web-search module) `tag_external_corroboration(trending_items) -> list[TrendingItem]` that, for top internal-trending items, runs a bounded cross-search and tags each `corroborated` (also big outside the user's network) vs `scoop` (your people first, little external signal). Bounded by `depth` to control cost/calls. Deterministic classification of the search-result volume into the two tags (a count threshold, not an LLM call) — Rule 5.
- **Definition of done:** A test mocks the cross-search to return many external results for item A and near-zero for item B, then asserts A is tagged `corroborated` and B is tagged `scoop` (the brief's corroboration-vs-scoop distinction, encoded as a deterministic threshold). A test asserts `depth="quick"` caps the number of cross-searches issued (cost control). Cross-search boundary mocked — no live web call.
- **Dependencies:** Sub-phase 2 (operates on internal-trending output)

### Sub-phase 4: Anomaly/scoop detection + render the M3 sections (Stage 5c + Stage 7 sections)
- **Files touched:** `skills/orbit/scripts/lib/trending.py`, `skills/orbit/scripts/lib/render.py`, `skills/orbit/scripts/lib/rerank.py`
- **What ships:** `detect_scoops(items, store) -> list[Scoop]` flagging a normally-dormant account that suddenly posts something accelerating fast (low historical post frequency from `seen` history + a high baseline-relative velocity from Sub-phase 2) — the highest-value signal, flagged loudly. Wires the trending/scoop multiplier (the `1.0` no-op left in `rerank.py` in Phase 3) so scoops/trending boost an item's derank score. Extends `render.py` to populate the previously-empty M3 sections: the "Everyone's talking about" overlap block (from Sub-phase 1 clusters), the right-rail trending list (Sub-phase 2-3), and the scoops strip (this sub-phase) — each cross-linking to its item/chapter deep-link.
- **Definition of done:** A test asserts an item from a creator with a long dormant gap (from `seen` history) that suddenly spikes is flagged as a scoop, while a high-frequency creator's spike is NOT (dormancy + acceleration intent, the brief's "highest-value signal" — not merely "high engagement"). A test asserts a scoop's trending multiplier raises its derank score above an otherwise-identical non-scoop item (the multiplier is no longer a no-op). A render test asserts the output HTML now contains the overlap block, a right-rail trending section, and a scoops strip with deep-links (the M3 sections populate). Boundaries mocked.
- **Dependencies:** Sub-phase 3 (corroboration/scoop tags feed the strip), Sub-phase 1 (overlap block), Sub-phase 2 (velocity feeds scoop detection)

## Phase-level definition of done
`pytest tests/` passes. Over the unified YouTube+X stream: short reactions merge into one overlap block while long-form episodes stay separate but cross-linked by chapter timestamp; the right-rail shows baseline-relative internal trending tagged corroborated-vs-scoop by the external cross-search; dormant-account acceleration is flagged in a loud scoops strip; and the rendered HTML (from Phase 3, now with the trending/scoop multiplier live) shows all three previously-empty M3 sections populated with working deep-links. No embedding model was added.

## Out of scope
- No new embedding/vector dependency (resolved: lexical-only).
- No delivery (iMessage/WhatsApp), `--setup`, or README (M4).
- No re-architecting of M1/M2 score/tier/render beyond wiring the trending multiplier and adding the three sections.
- No live web/LLM/X/YouTube calls in tests.

## Open questions
- Lexical-only clustering may miss paraphrased overlaps with no shared tokens; accepted for M3 per Rule 2 (the entity-overlap second pass mitigates; embeddings are a future enhancement if real use shows misses). Noted, not blocking.
- The dormancy threshold (what gap counts as "normally dormant") and the scoop velocity multiplier magnitude are first-cut constants tuned against the maintainer's real network. Not blocking — tunable.
- External cross-search call budget per `depth` is first-cut; documented in the M4 README cost section. Not blocking.

## Self-critique

**Product lens:** PASS. Maps exactly to brief Stage 4-5: overlap (short-merge/long-cross-link), internal trending (velocity vs baseline), external (corroboration vs scoop), anomaly/scoop loudly flagged. Design decision 7 (long-form stays a unit) is enforced and TESTED via the cross-link/never-shred check. No out-of-brief features. M3 builds on the M1 ranking the maintainer has by now validated on real days (per master-plan, M3 should not be built on an unproven ranking — the multiplier extends a proven score rather than replacing it).
**Engineering lens:** PASS. Within stack — reuses lifted lexical `cluster/dedupe/fusion` and `web_search_keyless` (no new embedding/vector dep, resolving Q1 inside the chosen stack rather than expanding it). DoDs are fresh-context checkable (3 tweets → 1 cluster; 5×-baseline item trends, normal item doesn't; A=corroborated/B=scoop; dormant-spike=scoop; HTML contains the three sections). Rule 5 honored: clustering, velocity, corroboration-thresholding, and dormancy detection are deterministic; no judgment-LLM is even required here (cluster *labeling* text, if added, would be the only candidate — kept optional). Sub-phase 4 activates the multiplier left flexible in Phase 3, so it extends rather than rewrites `rerank.py`.
**Risk lens:** Findings + fixes. (1) **File-boundary conflict:** Sub-phases 2, 3, 4 ALL touch `trending.py`, and 1 & 4 both touch `cluster.py`/`render.py`/`rerank.py` regions. Resolved by a strict sequential dependency chain (1→2→3→4) — flagged so `/run-phase` runs these in order, NOT parallel worktrees (this phase is inherently sequential and should not be split across concurrent agents). (2) Test coverage per Rule 9: every DoD fails on wrong business logic (raw-vs-baseline, convergence, corroboration threshold, dormancy, never-shred), not on mere "returns a list". (3) Painting-into-a-corner: 1(clusters)→2(velocity needs clusters)→3(external needs internal)→4(scoop needs velocity + clusters + tags, then renders all). Each consumes prior output; the multiplier slot was reserved in Phase 3 so 4 doesn't need to refactor scoring. Order holds. (4) Cost risk: the external cross-search is bounded by `depth` and tested for it — guards against runaway calls on the user's plan.
**Irreversible sub-phases:** None. (All outputs are computed-and-rendered or data rows on the existing schema; no migrations, no destructive ops, fully re-runnable.)
