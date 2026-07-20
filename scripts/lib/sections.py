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

TWO paths produce sections. They differ in WHO chooses the timestamps, and that drives
their cost by two orders of magnitude:

  - :func:`build_sections` (per video) — sends a whole transcript and lets the MODEL pick
    three topic-shift starts, then SNAPS each returned offset to the nearest real cue.
    ~3k tokens and one ``claude -p`` call per video.
  - :func:`build_sections_batch` (all videos at once) — CODE picks each video's three
    anchors from material already on hand (Stage-1 chapters, else a fetched transcript
    compacted to three short excerpts), and the model writes only the prose, for every
    video, in ONE call. ~6k tokens total for a ~35-video digest.

**The pipeline runs the batched path** (Rule 6). The per-video path is kept for the case
where a caller genuinely wants the model to find the topic shifts in one video.

Both share the same safety property — a section's timestamp always traces back to a real
source offset, so the ``watch?v=ID&t=Ns`` deep link always lands on content the summary
describes. The per-video path enforces it by snapping; the batched path enforces it more
strongly still, by never letting the model return a timestamp at all.

Where it runs matters (cost): sections are built AFTER ranking, on the final digest items
only. Stage 1 classifies up to 60 uploads but only ~35 reach the digest, so building
sections there would summarize ~25 videos that are never shown.

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


# --- Batched, anchor-grounded path (what the pipeline runs) -------------------------
# Cost is why this exists. The per-video :func:`build_sections` above sends a whole
# transcript and asks the model to CHOOSE the split points — ~3k tokens and one
# ``claude -p`` call per video, so a ~35-video digest costs ~35 calls / ~105k tokens.
# The batched path inverts the split: CODE picks each video's three anchors from material
# it already has (Stage-1 chapters, else a fetched transcript), and the model writes only
# the prose, for EVERY video, in ONE call (~6k tokens total). Rule 5 — the offsets are a
# deterministic choice, so only the summaries are a model judgment; Rule 6 — one call.
#
# The safety property is stronger here than in the per-video path: the model never returns
# a timestamp at all, so there is nothing to snap and no invented offset can exist.

# Per-anchor cap on the grounding text sent to the model. Three anchors at this cap is
# ~660 chars (~170 tokens) per video, so a ~35-video batch stays around 6k tokens.
MAX_ANCHOR_SOURCE_CHARS: int = 220

# How many consecutive cues are joined to describe a transcript anchor. Enough to carry a
# sentence or two of real speech; the char cap above is the hard backstop.
_CUES_PER_TRANSCRIPT_ANCHOR: int = 4

# Where the three transcript anchors sit in the cue list, as fractions of its length.
# Front-loaded rather than even thirds: the opening and the middle argument carry most of
# a video's substance, and the last fraction still leaves cues after it to describe.
_TRANSCRIPT_ANCHOR_FRACTIONS: tuple[float, ...] = (0.0, 0.40, 0.75)


@dataclass(frozen=True)
class SectionAnchor:
    """One fixed moment in a video, with the material found there.

    Attributes:
        start_seconds: The anchor's offset. ALWAYS a real source offset — a Stage-1
            chapter's ``start_seconds`` or a transcript cue's ``cue_start_seconds``.
        source_text: The grounding material at that moment (a chapter title, or the
            transcript speech there), capped at :data:`MAX_ANCHOR_SOURCE_CHARS`.
    """

    start_seconds: float
    source_text: str


@dataclass(frozen=True)
class SectionSource:
    """One video's input to :func:`build_sections_batch`.

    Attributes:
        video_id: The video's external id — the key the model's response is mapped back by.
        video_title: The title, given to the model as context.
        anchors: The code-chosen anchors, ascending, at most
            :data:`SECTIONS_PER_VIDEO` of them.
    """

    video_id: str
    video_title: str
    anchors: list[SectionAnchor]


def _spread_indices(item_count: int, pick_count: int) -> list[int]:
    """Pick ``pick_count`` evenly-spread indices across ``item_count`` items.

    Reason: an even spread makes the three sections cover the whole video. Taking the
    first three chapters instead would describe only its opening minutes.

    Args:
        item_count: How many items there are to pick from.
        pick_count: How many indices to pick.

    Returns:
        Ascending, de-duplicated indices (fewer than ``pick_count`` when the input is
        too short to yield distinct ones).

    Example:
        >>> _spread_indices(7, 3)
        [0, 3, 6]
        >>> _spread_indices(2, 3)
        [0, 1]
    """
    if item_count <= 0 or pick_count <= 0:
        return []
    if item_count <= pick_count:
        return list(range(item_count))
    step = (item_count - 1) / (pick_count - 1) if pick_count > 1 else 0
    return sorted({int(round(index * step)) for index in range(pick_count)})


def _cap_source_text(source_text: str) -> str:
    """Collapse whitespace and cap anchor grounding at :data:`MAX_ANCHOR_SOURCE_CHARS`.

    Args:
        source_text: The raw grounding text.

    Returns:
        The cleaned, capped text.
    """
    return " ".join(source_text.split())[:MAX_ANCHOR_SOURCE_CHARS]


def anchors_from_chapters(chapters: list[Any]) -> list[SectionAnchor]:
    """Pick three anchors from a video's Stage-1 chapters (FREE — no fetch, no tokens).

    The preferred source: chapters were already built and paid for in Stage 1 (creator
    chapters verbatim, or an LLM segmentation that already ran), so reusing them costs
    nothing. A chapter's ``title`` is often a bare label ("Intro") — that is fine as
    GROUNDING, because the batch prompt is told to describe the segment rather than echo
    the label.

    Args:
        chapters: The item's :class:`lib.chapterize.Chapter` list (may be empty).

    Returns:
        Up to :data:`SECTIONS_PER_VIDEO` anchors in ascending time order, or ``[]``.

    Example:
        >>> anchors_from_chapters([])
        []
    """
    usable = [
        chapter
        for chapter in chapters
        if isinstance(getattr(chapter, "start_seconds", None), (int, float))
    ]
    if not usable:
        return []
    usable.sort(key=lambda chapter: float(chapter.start_seconds))
    return [
        SectionAnchor(
            start_seconds=float(usable[index].start_seconds),
            source_text=_cap_source_text(str(getattr(usable[index], "title", "") or "")),
        )
        for index in _spread_indices(len(usable), SECTIONS_PER_VIDEO)
    ]


def anchors_from_transcript(transcript: Transcript | None) -> list[SectionAnchor]:
    """Pick three anchors from a transcript (the chapterless fallback).

    Used only when a video has no chapters to reuse. The transcript fetch itself costs no
    tokens (it is a ``yt-dlp`` subprocess); compacting it to three short excerpts HERE, in
    code, is what keeps the batched prompt small — the full cue list never reaches a model.

    Args:
        transcript: The cue-preserving transcript, or None.

    Returns:
        Up to :data:`SECTIONS_PER_VIDEO` anchors in ascending time order, or ``[]`` when
        there is no transcript or it has fewer than :data:`_MIN_CUES_FOR_SECTIONS` cues.

    Example:
        >>> anchors_from_transcript(None)
        []
    """
    if transcript is None or len(transcript.cues) < _MIN_CUES_FOR_SECTIONS:
        return []

    cues = transcript.cues
    anchors: list[SectionAnchor] = []
    seen_offsets: set[float] = set()
    for fraction in _TRANSCRIPT_ANCHOR_FRACTIONS:
        start_index = min(len(cues) - 1, int(len(cues) * fraction))
        start_seconds = float(cues[start_index].cue_start_seconds)
        if start_seconds in seen_offsets:
            continue
        seen_offsets.add(start_seconds)
        excerpt = " ".join(
            str(cue.text) for cue in cues[start_index : start_index + _CUES_PER_TRANSCRIPT_ANCHOR]
        )
        anchors.append(
            SectionAnchor(start_seconds=start_seconds, source_text=_cap_source_text(excerpt))
        )
    return anchors


def build_section_source(
    video_id: str,
    video_title: str,
    *,
    chapters: Optional[list[Any]] = None,
    transcript: Transcript | None = None,
) -> SectionSource | None:
    """Build one video's batch input, preferring FREE chapters over a transcript.

    The cost decision lives here: chapters are reused when present (zero fetch, zero
    tokens) and the transcript is consulted only as the chapterless fallback.

    Args:
        video_id: The video's external id.
        video_title: The video title.
        chapters: The item's Stage-1 chapters, if any.
        transcript: A fetched transcript, used only when ``chapters`` yields no anchors.

    Returns:
        The :class:`SectionSource`, or None when neither source yields any anchor (the
        row then renders with no section lines rather than invented ones).

    Example:
        >>> build_section_source("abc", "A talk") is None
        True
    """
    anchors = anchors_from_chapters(chapters or [])
    if not anchors:
        anchors = anchors_from_transcript(transcript)
    if not anchors:
        return None
    return SectionSource(video_id=video_id, video_title=video_title, anchors=anchors)


def _render_videos_block(sources: list[SectionSource]) -> str:
    """Render the batch prompt's VIDEOS block.

    One header line per video, then one ``- <timestamp> <material>`` line per anchor, so
    the model sees each video's shape without ever seeing a full transcript.

    Args:
        sources: The batch inputs.

    Returns:
        The newline-joined block.
    """
    block_lines: list[str] = []
    for source in sources:
        block_lines.append(f"{source.video_id}\t{source.video_title}")
        for anchor in source.anchors:
            label = format_timestamp_label(anchor.start_seconds)
            block_lines.append(f"  - {label} {anchor.source_text}")
    return "\n".join(block_lines)


def build_sections_batch(
    sources: list[SectionSource],
    *,
    llm_call: Optional[SectionSummarizer] = None,
) -> dict[str, list[Section]]:
    """Summarize every video's anchors in ONE model call, keyed by ``video_id``.

    Each returned :class:`Section` pairs a code-chosen anchor offset with the model's
    prose for it, so the deep link always lands on a real source offset. Only ids present
    in BOTH the input and the model's map are returned — an extra id never invents a row,
    and a missing one simply renders without section lines.

    **Fail-soft (Rule 12):** ANY LLM error or unparseable response returns ``{}`` and logs
    ``sections_batch_failed`` — the digest renders structurally, never with placeholders.

    Args:
        sources: One :class:`SectionSource` per video (build via
            :func:`build_section_source`). An empty list makes NO model call.
        llm_call: The injectable live-model boundary; defaults to
            :func:`lib.llm.call_claude_cli` (resolved at call time so a patch works).

    Returns:
        A ``{video_id: [Section, ...]}`` map. ``{}`` for empty input or ANY failure.

    Example:
        >>> build_sections_batch([])
        {}
    """
    if not sources:
        # Edge case: nothing to summarize — no model call at all.
        return {}

    indexed_sources = {source.video_id: source for source in sources if source.video_id}
    if not indexed_sources:
        return {}

    try:
        template = _load_prompt_section("build_sections_batch")
        prompt = template.format(videos_block=_render_videos_block(list(indexed_sources.values())))
        parsed = json.loads(_resolve_summarizer(llm_call)(prompt))
        if not isinstance(parsed, dict):
            raise ValueError("model did not return a JSON object of video_id->summaries")
    except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point (Rule 12).
        log.log_error(
            "sections_batch_failed",
            video_count=len(indexed_sources),
            error_message=str(exc),
            fix_suggestion=(
                "The batched section call failed or returned unparseable JSON; EVERY video "
                "renders WITHOUT section lines (graceful degradation). Check the 'claude' "
                "CLI/subscription and references/sections.md's build_sections_batch contract."
            ),
        )
        return {}

    sections_by_video: dict[str, list[Section]] = {}
    for video_id, source in indexed_sources.items():
        summaries = parsed.get(video_id)
        if not isinstance(summaries, list):
            continue
        built: list[Section] = []
        for anchor, summary_text in zip(source.anchors, summaries):
            if not isinstance(summary_text, str) or not summary_text.strip():
                # Reason: one blank summary must not lose the video's other sections.
                continue
            built.append(
                Section(
                    start_seconds=anchor.start_seconds,
                    timestamp_label=format_timestamp_label(anchor.start_seconds),
                    deep_link=build_deep_link(video_id, anchor.start_seconds),
                    summary_text=_truncate_summary(summary_text),
                )
            )
        if built:
            sections_by_video[video_id] = built

    missing_count = len(indexed_sources) - len(sections_by_video)
    if missing_count:
        # Rule 12: partial model coverage is never silent.
        log.log_warning(
            "sections_batch_incomplete",
            video_count=len(indexed_sources),
            covered_count=len(sections_by_video),
            missing_count=missing_count,
            fix_suggestion=(
                "The model omitted some video ids (or returned unusable summaries for them); "
                "those rows render without section lines. Check references/sections.md's "
                "build_sections_batch output contract if this recurs."
            ),
        )
    log.log_info(
        "sections_batch_built",
        video_count=len(indexed_sources),
        covered_count=len(sections_by_video),
    )
    return sections_by_video
