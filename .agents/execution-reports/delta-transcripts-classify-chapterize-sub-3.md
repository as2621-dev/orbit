# Execution report — Phase 2 / Sub-phase 3: Two-axis classification with channel prior

**Status:** COMPLETE (code written; NOT committed — orchestrator commits at phase end).

## Implemented
Stage-2 classification: each item judged on Axis A (signal/noise) and Axis B (on/off-topic)
via a single LLM judgment call (Rule 5), seeded by the channel-level prior, with user
overrides respected and never re-classified, and failing items routed to the "they also
posted" strip (deranked, never dropped — design decision 5). Persists via
`store.set_classification`.

## Files touched (only these three — Rule 3)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/scripts/lib/classify.py` (NEW, 322 lines)
- `/Users/asheshsrivastava/frommyfeed/skills/orbit/references/classify.md` (NEW, prompt template)
- `/Users/asheshsrivastava/frommyfeed/tests/test_classify.py` (NEW, 7 tests)

No edits to store.py, youtube_yt.py, transcribe.py, or anything else.

## Key design points
- **Injectable LLM boundary** `LlmClassifier = Callable[[str], str]`. Default
  `_default_llm_classifier` raises `NotImplementedError` with a `fix_suggestion` (no live
  model in this build env — fail loud, tests inject a mock). `classify_item` also takes an
  injectable `store_module` (defaults to `store`).
- **Prompt template lives in `references/classify.md`**, loaded + `.format(...)`-substituted
  at runtime (4 placeholders: item_title, item_description, channel_category, interests).
  Path resolved as `_LIB_DIR.parent.parent / "references" / "classify.md"` (references/ is a
  sibling of scripts/, both under orbit/).
- **Override short-circuit is deterministic and FIRST**: if a stored row has
  `is_user_override == 1`, return it built from the row WITHOUT calling the LLM.
- **Channel prior seeds the verdict on uncertainty**: `_parse_verdict` falls back to
  `channel_category` ('signal'->Axis A 1, 'noise'->0) per-axis when JSON is malformed or an
  axis key is missing/uncoercible. Axis-B fallback default = 1 (on-topic) — stays generous so
  a parse failure never silently buries an item; documented as `_AXIS_B_PRIOR_DEFAULT`. Logs
  `classify_verdict_unparseable` warning, never crashes.
- Item input handled as Upload (attribute) OR dict (key) via `_read_item_field`.

## Self-review (Step B) + fixes (Step C)
Three bugs caught during validation and fixed:
1. **HIGH — wrong prompt path.** First wrote `_LIB_DIR.parent / "references"` (= scripts/references,
   nonexistent). references/ is a sibling of scripts/. Fixed to `_LIB_DIR.parent.parent`.
2. **HIGH — `.format()` KeyError on the template comment.** The HTML comment literally
   contained `{placeholders}`, which `str.format` tried to substitute. Reworded the comment to
   avoid single braces and noted the doubling rule. (The JSON example was already doubled `{{...}}`.)
3. **MEDIUM (test) — store read after temp-dir teardown.** The never-drop test read
   `store.get_classification` outside the `with TemporaryDirectory()` block, hitting "unable to
   open database file". Moved the read inside the context.

No remaining critical/high findings.

## Validation (Step D)
- Import check: `from lib import classify` -> `ok`.
- `uv run --with pytest pytest tests/test_classify.py -q` -> **7 passed in 0.06s**.
- Full suite `uv run --with pytest pytest tests/ -q` -> **31 passed in 0.13s** (no regression).

Tests (Rule 9 — encode WHY):
1. failing-axis routes to also-posted AND persists on-record (never dropped) — verdict signal+off-topic.
2. user-override returned WITHOUT calling the LLM (injected boundary asserts not-called).
3. channel prior seeds Axis A on unparseable verdict — 'noise'->0 and 'signal'->1 (two tests).
4. clean signal+on-topic verdict -> is_also_posted False (boolean polarity guard).
5. malformed JSON falls back without crashing.
6. default boundary raises NotImplementedError (fail loud).

LLM mocked throughout; no real model call.

## DoD check (Step E): PASS
- "mocks LLM to `{axis_a_signal:1, axis_b_on_topic:0}` -> classified on-record AND routed to
  also-posted, not dropped" — PASS (test 1: `is_also_posted True` + `store.get_classification`
  returns the row).
- "existing `is_user_override=1` returned from store WITHOUT calling the LLM" — PASS (test 2).
- "channel prior seeds the verdict when the LLM is uncertain" — PASS (test 3, both categories).
- LLM mocked, no real call — PASS.

## Concerns
- None blocking. The exact prompt wording in `references/classify.md` is intentionally a draft;
  it is tuned during real-day usage (phase open question, noted not-blocking).
- Axis-B-on-uncertainty default (1) is a documented product choice (generous = never silently
  bury). If real-day usage shows off-topic items leaking through on parse failures, this is the
  knob to revisit.

## Handoff for Phase 3 (rank)
**`Classification` shape** (`lib/classify.py`):
- `item_external_id: str` (YouTube `video_id`)
- `axis_a_signal: int` (1 signal / 0 noise)
- `axis_b_on_topic: int` (1 on / 0 off)
- `is_user_override: int` (1 if user-corrected)
- `is_also_posted: bool` (property) = `axis_a_signal == 0 or axis_b_on_topic == 0`. True =>
  route to "they also posted" strip (deranked, NEVER dropped). False => top-line item.

**How Phase 3 reads a classified item's axes from the store:**
`store.get_classification(item_external_id) -> dict | None` with keys
`axis_a_signal`, `axis_b_on_topic`, `is_user_override` (plus `classification_id`,
`item_external_id`, `classified_at`). Returns None if never classified. Rank can reconstruct
the also-posted routing with the same rule: `row["axis_a_signal"] == 0 or row["axis_b_on_topic"] == 0`.
