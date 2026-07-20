"""Split one video into exactly THREE timestamped, summarized sections (Final design).

The Final digest design renders every YouTube row as a title plus three
``<timestamp> <summary>`` lines, each deep-linking into the moment. This module owns
that contract end to end: given a video and its transcript, it returns exactly
:data:`SECTIONS_PER_VIDEO` :class:`Section` objects.

This is DELIBERATELY separate from :mod:`lib.chapterize`. Chapters are sub-navigation
into long-form episodes and there may be any number of them (today's runs produce 5-7);
sections are a fixed-arity RENDER contract — always three, for every rendered video,
long-form or not. Chapterize keeps feeding classify/blurb grounding; sections feed the
digest rows.

Where it runs matters (cost): sections are built AFTER ranking, on the final digest
items only. Stage 1 classifies up to 60 uploads but only ~35 reach the digest, so
building sections there would transcribe and summarize ~25 videos that are never shown.

The decision tree (Rule 5 — only the split/summary is a model judgment; everything
that produces a URL is deterministic code):

  1. no transcript, or fewer than :data:`_MIN_CUES_FOR_SECTIONS` cues -> return ``[]``
     (the row renders with no section lines rather than with invented ones).
  2. otherwise -> ask the injected LLM to pick three topic-shift starts and summarize
     each (prompt in references/sections.md), then SNAP every returned
     ``start_seconds`` to the nearest real cue offset.

The snap is the load-bearing safety property: a section's timestamp is always a real
cue offset, so the ``watch?v=ID&t=Ns`` deep link always lands on content the summary
actually describes. A model-invented offset never survives into a link.

**Fail-soft (Rule 12):** any LLM error, unparseable response, or short/empty result
returns ``[]`` and logs — the digest renders structurally without section lines rather
than breaking, and never with placeholder prose.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.sections`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors chapterize.py / summarize.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.chapterize import _snap_to_nearest_cue_offset  # noqa: E402
from lib.transcribe import Transcript, build_deep_link  # noqa: E402

# The render contract: the Final design's YouTube row has exactly three section lines.
SECTIONS_PER_VIDEO: int = 3

# A video needs at least this many cues before a three-way split is meaningful. Below
# it, the "sections" would be near-identical slices of a handful of lines.
_MIN_CUES_FOR_SECTIONS: int = 12

# Hard cap on a section summary, applied in code — the model's length is never trusted
# (mirrors summarize.MAX_BLURB_CHARS). Sized to the design's section line, which wraps
# to about three lines at 12px in the 1fr column before it starts crowding the row.
MAX_SECTION_CHARS: int = 200

# Cue lines sent to the model. A 90-minute video can carry 1500+ cues; sending them all
# would blow the prompt budget (Rule 6), so cues are evenly downsampled to at most this
# many lines. Downsampling drops lines from the PROMPT only — snapping still targets the
# full cue list, so a section can start at a cue that was never shown to the model.
_MAX_PROMPT_CUE_LINES: int = 240

# The prompt TEMPLATE lives in references/sections.md, NOT inline (so the maintainer can
# tune wording during real-day usage without touching code).
# Resolved relative to this file: scripts/lib/sections.py -> ../../references/sections.md.
_SECTIONS_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "sections.md").resolve()


@dataclass(frozen=True)
class Section:
    """One of a video's three timestamped sections.

    Attributes:
        start_seconds: The section's start offset in seconds. ALWAYS a real transcript
            cue's ``cue_start_seconds`` — snapped in code, never the model's raw number.
        timestamp_label: The display label built from ``start_seconds`` (``"4:20"``,
            or ``"1:04:20"`` past an hour) — what the design renders in red mono.
        deep_link: The ``watch?v=ID&t=Ns`` URL built from ``start_seconds`` via
            :func:`lib.transcribe.build_deep_link`.
        summary_text: What this stretch of the video covers (<= :data:`MAX_SECTION_CHARS`).
    """

    start_seconds: float
    timestamp_label: str
    deep_link: str
    summary_text: str


# The injectable LLM boundary: takes the rendered prompt, returns the model's raw JSON
# string (a list of ``{"start_seconds", "text"}`` objects). Tests inject a mock; the
# real caller is :func:`lib.llm.call_claude_cli`, resolved at call time.
SectionSummarizer = Callable[[str], str]


def format_timestamp_label(total_seconds: float) -> str:
    """Format a cue offset as the design's timestamp label.

    Uses ``M:SS`` under an hour and ``H:MM:SS`` at or past one, matching how YouTube
    itself labels positions (so the label reads the same as the player's scrubber).

    Args:
        total_seconds: The offset in seconds (floats are truncated toward zero).

    Returns:
        The display label.

    Example:
        >>> format_timestamp_label(45)
        '0:45'
        >>> format_timestamp_label(260.9)
        '4:20'
        >>> format_timestamp_label(3860)
        '1:04:20'
    """
    whole_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _resolve_summarizer(llm_call: Optional[SectionSummarizer]) -> SectionSummarizer:
    """Resolve the LLM boundary, defaulting to the live ``claude`` CLI caller.

    Imported at CALL time (not module import) so a test that patches
    ``lib.llm.call_claude_cli`` is honoured. Mirrors summarize._resolve_caller.

    Args:
        llm_call: An explicit caller, or None to use the live default.

    Returns:
        The caller to use.
    """
    if llm_call is not None:
        return llm_call
    from lib import llm

    return llm.call_claude_cli


def _load_prompt_section(section_name: str) -> str:
    """Load ONE labeled prompt section from references/sections.md.

    The file fences each template with ``<!-- PROMPT:<name> -->`` /
    ``<!-- /PROMPT:<name> -->`` markers. This returns the text BETWEEN them (stripped),
    ready for ``.format(...)``. Mirrors summarize._load_prompt_section.

    Args:
        section_name: The section label (``build_sections``).

    Returns:
        The template body for that section.

    Raises:
        ValueError: If the section markers are absent (the prompt file is malformed —
            fail loud here; the caller turns any raised error into safe degradation).
    """
    document = _SECTIONS_PROMPT_PATH.read_text(encoding="utf-8")
    open_marker = f"<!-- PROMPT:{section_name} -->"
    close_marker = f"<!-- /PROMPT:{section_name} -->"
    start_index = document.find(open_marker)
    end_index = document.find(close_marker)
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError(
            f"references/sections.md is missing the '{section_name}' prompt section "
            f"(expected '{open_marker}' ... '{close_marker}')."
        )
    return document[start_index + len(open_marker) : end_index].strip()


def _downsample_cues(cues: list[Any], max_lines: int) -> list[Any]:
    """Evenly thin a cue list down to at most ``max_lines`` entries.

    Reason: an even stride preserves the video's SHAPE (the model still sees the whole
    timeline, just at coarser resolution) where head-truncation would hide the ending
    entirely and make a third section impossible to place.

    Args:
        cues: The full cue list, in time order.
        max_lines: The maximum number of cues to keep.

    Returns:
        The thinned list (the input itself when already short enough).

    Example:
        >>> _downsample_cues([1, 2, 3, 4, 5, 6], 3)
        [1, 3, 5]
    """
    if max_lines <= 0 or len(cues) <= max_lines:
        return list(cues)
    stride = len(cues) / max_lines
    return [cues[min(len(cues) - 1, int(index * stride))] for index in range(max_lines)]


def _render_transcript_block(transcript: Transcript) -> str:
    """Render the prompt's transcript block: one ``<start_seconds> <text>`` line per cue.

    Args:
        transcript: The cue-preserving transcript.

    Returns:
        The newline-joined block, downsampled to :data:`_MAX_PROMPT_CUE_LINES`.
    """
    block_lines: list[str] = []
    for cue in _downsample_cues(transcript.cues, _MAX_PROMPT_CUE_LINES):
        cue_text = " ".join(str(cue.text).split())
        if cue_text:
            block_lines.append(f"{int(cue.cue_start_seconds)} {cue_text}")
    return "\n".join(block_lines)


def _truncate_summary(summary_text: str) -> str:
    """Collapse whitespace and cap a section summary at :data:`MAX_SECTION_CHARS`.

    Truncation cuts at the last word boundary inside the cap and appends an ellipsis, so
    a capped summary never ends mid-word. Mirrors summarize._truncate_blurb.

    Args:
        summary_text: The model's raw summary.

    Returns:
        The cleaned, capped summary.

    Example:
        >>> _truncate_summary("  a   b  ")
        'a b'
    """
    collapsed = " ".join(summary_text.split())
    if len(collapsed) <= MAX_SECTION_CHARS:
        return collapsed
    clipped = collapsed[: MAX_SECTION_CHARS - 1]
    last_space_index = clipped.rfind(" ")
    if last_space_index > 0:
        clipped = clipped[:last_space_index]
    return clipped.rstrip(" ,;:—-") + "…"


def _parse_sections(raw_response: str) -> list[dict] | None:
    """Parse the model's response into a list of ``{start_seconds, text}`` dicts.

    Args:
        raw_response: The model's raw string.

    Returns:
        The parsed list, or None when the response is not a JSON array of objects.
    """
    parsed = json.loads(raw_response)
    if not isinstance(parsed, list):
        return None
    return [entry for entry in parsed if isinstance(entry, dict)]


def build_sections(
    video_id: str,
    video_title: str,
    transcript: Transcript | None,
    *,
    llm_call: Optional[SectionSummarizer] = None,
) -> list[Section]:
    """Build exactly three timestamped, summarized sections for one video.

    Every returned section's ``start_seconds`` is snapped to a real cue offset, so its
    ``deep_link`` always lands on content the ``summary_text`` describes. Sections come
    back in ascending time order with duplicate starts removed.

    Fail-soft: returns ``[]`` (never partial placeholder prose) when there is no usable
    transcript, when the LLM call fails, or when the response cannot be parsed. Returning
    fewer than :data:`SECTIONS_PER_VIDEO` sections is possible if the model collapses two
    starts onto the same cue; that is logged, and the renderer shows what survived.

    Args:
        video_id: The video's external id, used to build the deep links.
        video_title: The video title, given to the model as context.
        transcript: The cue-preserving transcript, or None when none was fetched.
        llm_call: The injectable live-model boundary; defaults to
            :func:`lib.llm.call_claude_cli` (resolved at call time so a patch works).

    Returns:
        Up to :data:`SECTIONS_PER_VIDEO` sections in ascending time order, or ``[]``.

    Example:
        >>> build_sections("abc", "A talk", None)
        []
    """
    if transcript is None or len(transcript.cues) < _MIN_CUES_FOR_SECTIONS:
        # Edge case: nothing to split. No model call — the row renders without sections.
        log.log_info(
            "sections_skipped_no_transcript",
            video_id=video_id,
            cue_count=0 if transcript is None else len(transcript.cues),
            min_cues=_MIN_CUES_FOR_SECTIONS,
        )
        return []

    cue_offsets = [float(cue.cue_start_seconds) for cue in transcript.cues]

    try:
        template = _load_prompt_section("build_sections")
        prompt = template.format(
            video_title=video_title,
            transcript_block=_render_transcript_block(transcript),
        )
        parsed_entries = _parse_sections(_resolve_summarizer(llm_call)(prompt))
        if parsed_entries is None:
            raise ValueError("model did not return a JSON array of section objects")
    except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point (Rule 12).
        log.log_error(
            "sections_failed",
            video_id=video_id,
            error_message=str(exc),
            fix_suggestion=(
                "The section-split LLM call failed or returned unparseable JSON; this "
                "video renders WITHOUT section lines (graceful degradation). Check the "
                "'claude' CLI/subscription and references/sections.md's output contract."
            ),
        )
        return []

    # Snap every start to a REAL cue offset, then dedupe. Two model starts can snap onto
    # the same cue on a short video; keeping the first preserves its summary.
    sections_by_offset: dict[float, Section] = {}
    for entry in parsed_entries:
        raw_start = entry.get("start_seconds")
        raw_text = entry.get("text")
        if not isinstance(raw_start, (int, float)) or not isinstance(raw_text, str) or not raw_text.strip():
            # Reason: a single malformed entry must not lose the whole split.
            continue
        snapped_start = _snap_to_nearest_cue_offset(float(raw_start), cue_offsets)
        if snapped_start in sections_by_offset:
            continue
        sections_by_offset[snapped_start] = Section(
            start_seconds=snapped_start,
            timestamp_label=format_timestamp_label(snapped_start),
            deep_link=build_deep_link(video_id, snapped_start),
            summary_text=_truncate_summary(raw_text),
        )

    sections = [sections_by_offset[offset] for offset in sorted(sections_by_offset)][:SECTIONS_PER_VIDEO]

    if len(sections) != SECTIONS_PER_VIDEO:
        # Rule 12: a short split is never silent — the design expects three lines.
        log.log_warning(
            "sections_incomplete",
            video_id=video_id,
            section_count=len(sections),
            expected_count=SECTIONS_PER_VIDEO,
            returned_count=len(parsed_entries),
            fix_suggestion=(
                "The model returned fewer than three usable sections (or two snapped onto "
                "one cue). The row renders with the sections that survived. Check "
                "references/sections.md if this recurs on long videos."
            ),
        )
    else:
        log.log_info("sections_built", video_id=video_id, section_count=len(sections))

    return sections
