"""LLM editorial prose for the Tiles digest (Phase 7 / Sub-phase 2).

Two summarization/judgment jobs (Rule 5 — summarizing the day's feed is a valid model
use; everything around the call is deterministic code):

  - :func:`summarize_items` — ONE <=140-char editorial blurb per top-tier item, keyed
    by ``item_external_id``. All items are packed into a SINGLE ``claude``-CLI call (Rule
    6 token discipline) and a JSON map is parsed back.
  - :func:`synthesize_verdict` — the ONE masthead "verdict" sentence, whose prompt
    carries scoop + cluster context so the sentence reflects the day's real shape.

Both go through the project's ONLY live-model boundary, :func:`lib.llm.call_claude_cli`
(the Claude Code subscription path — no API key, stdlib only). The boundary is the
same ``Callable[[str], str]`` shape classify/chapterize use, so tests inject a mock (or
patch :data:`lib.summarize.call_claude_cli`) and never spawn the real CLI.

**Fail-soft (Rule 12 / graceful degradation):** the digest must NEVER break because the
LLM is down. ANY error — a missing CLI, a timeout, a non-zero exit, or an unparseable
response — makes :func:`summarize_items` return ``{}`` and :func:`synthesize_verdict`
return ``""``. The renderer then degrades to a structural-only digest (no fabricated
prose). Every degradation logs ``summarize_failed`` / ``verdict_failed`` with an
actionable ``fix_suggestion``; no exception escapes either function.

Prompt TEMPLATES live in references/summarize.md (NOT inline), loaded at runtime and
``.format(...)``-substituted — mirrors references/classify.md / references/chapterize.md
so the maintainer tunes wording without touching code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.summarize`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors classify.py / chapterize.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.llm import call_claude_cli  # noqa: E402

# The prompt TEMPLATES live in references/summarize.md, NOT inline (tune wording without
# touching code). Resolved relative to this file: scripts/lib/summarize.py -> ../../references.
_SUMMARIZE_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "summarize.md").resolve()

# Hard ceiling on a single editorial blurb. The model is ASKED for <=140 chars, but we
# never trust it — any longer blurb is truncated defensively in code (Rule 12).
MAX_BLURB_CHARS: int = 140

# The injectable live-model boundary: a rendered prompt in, the model's raw text out —
# identical to ``lib.classify.LlmClassifier`` / ``lib.chapterize.ChapterSegmenter``.
LlmCaller = Callable[[str], str]


def _load_prompt_section(section_name: str) -> str:
    """Load ONE labeled prompt section from references/summarize.md.

    The file holds multiple templates, each fenced by ``<!-- PROMPT:<name> -->`` and
    ``<!-- /PROMPT:<name> -->`` markers. This returns the text BETWEEN those markers for
    ``section_name`` (stripped), ready for ``.format(...)``.

    Args:
        section_name: The section label (``summarize_items`` | ``synthesize_verdict``).

    Returns:
        The template body for that section.

    Raises:
        ValueError: If the section markers are absent (the prompt file is malformed —
            fail loud here; the callers turn any raised error into safe degradation).

    Example:
        >>> template = _load_prompt_section("summarize_items")  # doctest: +SKIP
        >>> "{items_block}" in template  # doctest: +SKIP
        True
    """
    document = _SUMMARIZE_PROMPT_PATH.read_text(encoding="utf-8")
    open_marker = f"<!-- PROMPT:{section_name} -->"
    close_marker = f"<!-- /PROMPT:{section_name} -->"
    start_index = document.find(open_marker)
    end_index = document.find(close_marker)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError(
            f"references/summarize.md is missing the '{section_name}' prompt section "
            f"(expected '{open_marker}' ... '{close_marker}')."
        )
    return document[start_index + len(open_marker) : end_index].strip()


def _read_field(obj: Any, field_name: str) -> str:
    """Read ``field_name`` off an object (attribute) or a dict (key) as a string.

    Args:
        obj: A :class:`lib.rerank.RankableItem`, a dict, or any object exposing the field.
        field_name: The field to read.

    Returns:
        The value coerced to a string, or "" if absent/None.
    """
    if isinstance(obj, dict):
        value = obj.get(field_name)
    else:
        value = getattr(obj, field_name, None)
    return "" if value is None else str(value)


def _resolve_caller(llm_call: Optional[LlmCaller]) -> LlmCaller:
    """Pick the live-model boundary: the injected one, else module-level ``call_claude_cli``.

    Resolving the default HERE (not as a bound default arg) lets a test either inject
    ``llm_call=mock`` OR patch :data:`lib.summarize.call_claude_cli` — both reach the same
    seam, neither spawns the real CLI.

    Args:
        llm_call: The injected boundary, or None to use the module default.

    Returns:
        The boundary callable to invoke.
    """
    return llm_call if llm_call is not None else call_claude_cli


def _truncate_blurb(blurb: str) -> str:
    """Clamp one blurb to :data:`MAX_BLURB_CHARS`, never trusting the model's length.

    Args:
        blurb: The raw blurb string from the model.

    Returns:
        The stripped blurb, hard-capped to ``MAX_BLURB_CHARS`` characters.

    Example:
        >>> _truncate_blurb("x" * 200)[:1], len(_truncate_blurb("x" * 200))
        ('x', 140)
    """
    return blurb.strip()[:MAX_BLURB_CHARS]


def summarize_items(items: list[Any], *, llm_call: Optional[LlmCaller] = None) -> dict[str, str]:
    """Write one <=140-char editorial blurb per item, keyed by ``item_external_id``.

    All ``items`` are packed into a SINGLE ``claude``-CLI call (Rule 6 token discipline)
    that returns a JSON map ``{item_external_id: blurb}``; each blurb is truncated to
    :data:`MAX_BLURB_CHARS` in code (the model's length is never trusted).

    CONTRACT: this summarizes WHATEVER ``items`` it is given. The CALLER is responsible
    for passing only the items that should get blurbs — in Orbit that is the Hero/Standard
    tier (cost control; Compact/Index/tweets get none). Only ids that appear in BOTH the
    input and the model's map are returned, so an absent/extra id never invents a blurb.

    **Fail-soft (Rule 12):** ANY LLM error or unparseable response returns ``{}`` and logs
    ``summarize_failed`` — the digest renders structurally with no blurbs rather than
    breaking. No exception escapes.

    Args:
        items: The items to summarize (:class:`lib.rerank.RankableItem` or dicts exposing
            ``item_external_id`` / ``title`` / ``channel_name``).
        llm_call: The injectable live-model boundary; defaults to
            :func:`lib.llm.call_claude_cli` (resolved at call time so a patch also works).

    Returns:
        A ``{item_external_id: blurb}`` map (each blurb <=140 chars). ``{}`` for an empty
        input (no LLM call) OR for any failure.

    Example:
        >>> summarize_items([])
        {}
        >>> summarize_items(  # doctest: +SKIP
        ...     [item], llm_call=lambda prompt: '{"abc": "A sharp take on the M5 leak."}',
        ... )
        {'abc': 'A sharp take on the M5 leak.'}
    """
    if not items:
        # Edge case: nothing to summarize — no model call, empty map.
        return {}

    # Build the id->item index AND the prompt's items block in one pass. Items with an
    # empty id are skipped (they could never be keyed back from the model's map).
    indexed_items: dict[str, Any] = {}
    block_lines: list[str] = []
    for item in items:
        item_external_id = _read_field(item, "item_external_id")
        if not item_external_id:
            continue
        indexed_items[item_external_id] = item
        channel_name = _read_field(item, "channel_name")
        title = _read_field(item, "title")
        # Tab-separated so the model parses id/channel/title cleanly even with commas.
        block_lines.append(f"{item_external_id}\t{channel_name}\t{title}")

    if not indexed_items:
        return {}

    try:
        template = _load_prompt_section("summarize_items")
        prompt = template.format(items_block="\n".join(block_lines))
        raw_response = _resolve_caller(llm_call)(prompt)
        parsed = json.loads(raw_response)
        if not isinstance(parsed, dict):
            raise ValueError("model did not return a JSON object of id->blurb")
    except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point (Rule 12).
        log.log_error(
            "summarize_failed",
            fix_suggestion=(
                "The blurb LLM call failed or returned unparseable JSON; the digest renders "
                "WITHOUT blurbs (graceful degradation). Check the 'claude' CLI/subscription "
                "and references/summarize.md's output contract."
            ),
            item_count=len(indexed_items),
            error_message=str(exc),
        )
        return {}

    # Keep only ids we actually asked about, with a non-empty string blurb, truncated.
    blurbs: dict[str, str] = {}
    for item_external_id in indexed_items:
        raw_blurb = parsed.get(item_external_id)
        if isinstance(raw_blurb, str) and raw_blurb.strip():
            blurbs[item_external_id] = _truncate_blurb(raw_blurb)

    log.log_info("summarize_completed", requested=len(indexed_items), blurbed=len(blurbs))
    return blurbs


def _build_verdict_context(tiered_items: list[Any], scoops: list[Any], clusters: list[Any]) -> str:
    """Render the scoop + cluster + top-item context block for the verdict prompt.

    The verdict must reflect the day's REAL shape, so the prompt is grounded in: the top
    tiered headlines, the scoops (a followed account broke a story early), and the
    overlap clusters (multiple followed sources converging). Each section is omitted when
    empty so the model is never handed blank scaffolding.

    Args:
        tiered_items: :class:`lib.density.TieredItem`-shaped objects (``.scored_item.item``
            is the :class:`lib.rerank.RankableItem`; ``.density_tier`` the tier).
        scoops: :class:`lib.trending.TrendingItem`-shaped objects exposing ``title``.
        clusters: :class:`lib.cluster.Cluster`-shaped objects exposing ``member_item_ids``
            / ``source_diversity`` / ``representative_item_id``.

    Returns:
        The assembled context block (may be empty if all three inputs are empty).
    """
    sections: list[str] = []

    # Top headlines — read the RankableItem title through the TieredItem wrapper, but
    # tolerate a bare item/dict too (defensive: the verdict must not crash on shape).
    headline_lines: list[str] = []
    for tiered_item in tiered_items[:8]:
        scored_item = getattr(tiered_item, "scored_item", None)
        rankable = getattr(scored_item, "item", None) if scored_item is not None else None
        source_obj = rankable if rankable is not None else tiered_item
        title = _read_field(source_obj, "title")
        channel_name = _read_field(source_obj, "channel_name")
        density_tier = _read_field(tiered_item, "density_tier")
        if title:
            tier_label = f"[{density_tier}] " if density_tier else ""
            headline_lines.append(f"- {tier_label}{channel_name} — {title}".rstrip(" —"))
    if headline_lines:
        sections.append("Top headlines today:\n" + "\n".join(headline_lines))

    scoop_lines = [f"- {title}" for title in (_read_field(s, "title") for s in scoops) if title]
    if scoop_lines:
        sections.append(
            "Scoops (a followed account surfaced this ahead of the wider network):\n"
            + "\n".join(scoop_lines)
        )

    cluster_lines: list[str] = []
    for cluster in clusters:
        member_count = len(getattr(cluster, "member_item_ids", []) or [])
        source_diversity = getattr(cluster, "source_diversity", 0) or 0
        representative = _read_field(cluster, "representative_item_id")
        cluster_lines.append(
            f"- {source_diversity} sources / {member_count} items converging"
            + (f" (representative: {representative})" if representative else "")
        )
    if cluster_lines:
        sections.append(
            "Overlapping stories (clusters — multiple followed sources on one topic):\n"
            + "\n".join(cluster_lines)
        )

    return "\n\n".join(sections)


def synthesize_verdict(
    tiered_items: list[Any],
    scoops: list[Any],
    clusters: list[Any],
    *,
    llm_call: Optional[LlmCaller] = None,
) -> str:
    """Write the ONE masthead "verdict" sentence summarizing the day's feed.

    The prompt is grounded in scoop + cluster + top-headline context (via
    :func:`_build_verdict_context`) so the sentence names the day's real story rather
    than fabricating one. Returns plain text (which MAY carry simple ``**bold**`` accent
    markers the renderer styles), kept to a single sentence by the prompt.

    **Fail-soft (Rule 12):** ANY LLM error or empty response returns ``""`` and logs
    ``verdict_failed`` — the masthead renders with no verdict rather than breaking. No
    exception escapes.

    Args:
        tiered_items: The day's tiered items (:class:`lib.density.TieredItem` shape).
        scoops: The day's scoops (:class:`lib.trending.TrendingItem` shape).
        clusters: The day's overlap clusters (:class:`lib.cluster.Cluster` shape).
        llm_call: The injectable live-model boundary; defaults to
            :func:`lib.llm.call_claude_cli` (resolved at call time so a patch also works).

    Returns:
        The single verdict sentence (stripped), or ``""`` when there is nothing to
        summarize (no LLM call) OR on any failure.

    Example:
        >>> synthesize_verdict([], [], [])
        ''
        >>> synthesize_verdict(  # doctest: +SKIP
        ...     tiered, scoops, clusters,
        ...     llm_call=lambda prompt: "Quiet day — the only real story is the M5 leak.",
        ... )
        'Quiet day — the only real story is the M5 leak.'
    """
    if not tiered_items and not scoops and not clusters:
        # Edge case: an empty day has no shape to summarize — no model call.
        return ""

    try:
        context_block = _build_verdict_context(tiered_items, scoops, clusters)
        if not context_block:
            # Nothing renderable survived (e.g. titleless items) — degrade, no model call.
            return ""
        template = _load_prompt_section("synthesize_verdict")
        prompt = template.format(context_block=context_block)
        verdict = _resolve_caller(llm_call)(prompt).strip()
    except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point (Rule 12).
        log.log_error(
            "verdict_failed",
            fix_suggestion=(
                "The verdict LLM call failed; the masthead renders WITHOUT a verdict "
                "(graceful degradation). Check the 'claude' CLI/subscription and "
                "references/summarize.md's output contract."
            ),
            error_message=str(exc),
        )
        return ""

    log.log_info("verdict_completed", has_verdict=bool(verdict))
    return verdict
