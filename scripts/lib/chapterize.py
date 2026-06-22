"""Chapterize long-form videos into timestamped chapters (Phase 2 / Stage 3).

A chapter is sub-navigation INTO one video, never a way to shred it. Design
decision 7: a long-form upload stays ONE episode unit — :func:`chapterize_episode`
returns a ``list[Chapter]`` for the single upload it was given; it does NOT split
the upload into multiple items. Every chapter resolves to a
``watch?v=ID&t=Ns`` deep-link built (via :func:`lib.transcribe.build_deep_link`)
from a REAL transcript cue offset — never an invented timestamp.

The decision tree (Rule 5 — only transcript SEGMENTATION is a model judgment;
the threshold, the creator-chapter mapping, and the deep-link building are
deterministic code):

  1. duration is None or <= 1200s  -> short item, return ``[]`` (short stays short).
  2. creator ``chapters`` present  -> map them verbatim to :class:`Chapter`s with
     deep-links from each ``start_time`` (DETERMINISTIC, no LLM).
  3. long-form, no creator chapters -> ask the injected LLM segmenter to detect
     topic shifts (prompt in references/chapterize.md), then SNAP each returned
     ``start_seconds`` to the nearest real cue offset so the timestamp always
     traces back to a real cue.

There is NO live LLM in this build environment. The segmentation call therefore
goes through an INJECTABLE boundary (:data:`ChapterSegmenter`); the module-level
default (:func:`_default_chapter_segmenter`) FAILS LOUD with
``NotImplementedError`` rather than faking segments. Tests inject a mock; the
real host-session wiring is out of scope for this sub-phase.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.chapterize`` (via orbit.py's sys.path insert of the scripts dir) or run
# from the scripts dir directly. Mirrors youtube_yt.py / classify.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.transcribe import Transcript, build_deep_link  # noqa: E402

# Long-form threshold (master-plan Q6, resolved): a video is long-form — and thus
# eligible for chapters — only when its duration exceeds 1200 seconds (20 minutes).
# At or under this, the item stays a single short unit with no chapters.
LONG_FORM_THRESHOLD_SECONDS: int = 1200

# The prompt TEMPLATE lives in references/chapterize.md, NOT inline (so the
# maintainer can tune wording during real-day usage without touching code).
# Resolved relative to this file: scripts/lib/chapterize.py -> ../../references/chapterize.md.
_CHAPTERIZE_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "chapterize.md").resolve()


@dataclass
class Chapter:
    """One timestamped chapter — sub-navigation INTO a single long-form video.

    Attributes:
        title: A short, human-readable label for the segment (e.g. ``"Intro"``).
        start_seconds: The chapter's start offset in seconds. ALWAYS traces back to
            a real source: a creator chapter's ``start_time`` or a real transcript
            cue's ``cue_start_seconds`` — never an invented value.
        deep_link: The ``watch?v=ID&t=Ns`` URL built from ``start_seconds`` via
            :func:`lib.transcribe.build_deep_link`; drops the user into the moment.
    """

    title: str
    start_seconds: float
    deep_link: str


# The injectable segmentation boundary: takes the rendered prompt, returns the
# model's raw JSON string (a list of ``{"title", "start_seconds"}`` segments).
# Tests inject a mock; the real host-session caller is wired at runtime.
ChapterSegmenter = Callable[[str], str]


def _default_chapter_segmenter(prompt: str) -> str:
    """Default segmentation boundary — no live model here, so fail loud.

    Args:
        prompt: The rendered chapterize prompt (unused; we never fake segments).

    Raises:
        NotImplementedError: Always. The real host Claude-session caller must be
            injected at runtime; tests inject a mock.
    """
    log.log_error(
        "chapterize_segmenter_not_wired",
        fix_suggestion=(
            "wire the host Claude session caller at runtime; tests must inject a mock "
            "via chapterize_episode(..., segmenter=...)"
        ),
    )
    raise NotImplementedError(
        "No live chapter segmenter is wired. Inject one via "
        "chapterize_episode(..., segmenter=...). In this build env there is no live "
        "model; tests must mock the boundary."
    )


def _chapters_from_creator(video_id: str, creator_chapters: list[dict]) -> list[Chapter]:
    """Map creator-supplied chapters verbatim to :class:`Chapter`s (DETERMINISTIC).

    Each creator chapter is a ``{title, start_time, end_time}`` dict from yt-dlp.
    We keep its title and ``start_time`` verbatim and build the deep-link from that
    ``start_time`` — no LLM, no inference. Chapters with an unparseable ``start_time``
    are skipped (a single bad entry must not lose the whole chapter list).

    Args:
        video_id: The upload's video id (for the deep-link).
        creator_chapters: The raw yt-dlp ``chapters`` array.

    Returns:
        The mapped chapters in feed order.
    """
    chapters: list[Chapter] = []
    for creator_chapter in creator_chapters:
        if not isinstance(creator_chapter, dict):
            continue
        raw_start_time = creator_chapter.get("start_time")
        try:
            start_seconds = float(raw_start_time)
        except (TypeError, ValueError):
            # Reason: a single malformed creator chapter must not crash the run.
            continue
        chapter_title = str(creator_chapter.get("title") or "")
        chapters.append(
            Chapter(
                title=chapter_title,
                start_seconds=start_seconds,
                deep_link=build_deep_link(video_id, start_seconds),
            )
        )
    return chapters


def _render_prompt(video_title: str, transcript: Transcript) -> str:
    """Load references/chapterize.md and render the title + the timed cue lines.

    Each cue becomes a ``<start_seconds>\\t<text>`` line so the model can only pick
    among REAL cue offsets (the left column) — the snap step then enforces this.

    Args:
        video_title: The upload title (context for the labels).
        transcript: The cue-preserving transcript to segment.

    Returns:
        The fully rendered prompt string ready for the segmentation boundary.
    """
    template = _CHAPTERIZE_PROMPT_PATH.read_text(encoding="utf-8")
    cue_lines = "\n".join(
        f"{cue.cue_start_seconds}\t{cue.text}" for cue in transcript.cues
    )
    return template.format(video_title=video_title, cue_lines=cue_lines)


def snap_to_nearest_cue_offset(target_seconds: float, cue_offsets: list[float]) -> float:
    """Snap a model-returned timestamp to the NEAREST real cue offset.

    This is the invariant enforcer reused by BOTH chapterize and summarize: a
    chapter's (or summary bullet's) ``start_seconds`` MUST be a real cue offset,
    never an invented number. If the model returns an exact cue offset it is returned
    unchanged; otherwise it maps to the closest one.

    Args:
        target_seconds: The model's proposed start offset.
        cue_offsets: The real cue ``cue_start_seconds`` values (non-empty).

    Returns:
        The element of ``cue_offsets`` closest to ``target_seconds``.
    """
    return min(cue_offsets, key=lambda cue_offset: abs(cue_offset - target_seconds))


# Private alias kept for any in-tree caller/test referencing the underscore name.
_snap_to_nearest_cue_offset = snap_to_nearest_cue_offset


def _parse_segments(raw: str) -> list[dict] | None:
    """Parse the strict-JSON segment list; return None on any failure (never crash).

    Args:
        raw: The segmenter's raw response string.

    Returns:
        A list of segment dicts, or None if the payload is not a JSON list.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [segment for segment in parsed if isinstance(segment, dict)]


def _chapters_from_segments(
    video_id: str, segments: list[dict], cue_offsets: list[float]
) -> list[Chapter]:
    """Turn parsed model segments into :class:`Chapter`s, snapping every offset.

    Each segment's ``start_seconds`` is snapped to the nearest real cue offset and
    the deep-link is built from that snapped offset — so the timestamp always traces
    back to a real cue. Segments with a non-numeric ``start_seconds`` are skipped.

    Args:
        video_id: The upload's video id (for the deep-link).
        segments: The parsed segment dicts from the model.
        cue_offsets: The real cue ``cue_start_seconds`` values (non-empty).

    Returns:
        The snapped chapters in model order.
    """
    chapters: list[Chapter] = []
    for segment in segments:
        raw_start_seconds = segment.get("start_seconds")
        try:
            proposed_seconds = float(raw_start_seconds)
        except (TypeError, ValueError):
            continue
        snapped_seconds = _snap_to_nearest_cue_offset(proposed_seconds, cue_offsets)
        chapter_title = str(segment.get("title") or "")
        chapters.append(
            Chapter(
                title=chapter_title,
                start_seconds=snapped_seconds,
                deep_link=build_deep_link(video_id, snapped_seconds),
            )
        )
    return chapters


def _single_chapter_fallback(video_id: str, cue_offsets: list[float]) -> list[Chapter]:
    """Fall back to ONE chapter at the first cue, or ``[]`` if there are no cues.

    Used when the transcript is empty or the segmenter verdict is unparseable. We
    NEVER invent a timestamp: the lone chapter starts at the first real cue offset.

    Args:
        video_id: The upload's video id (for the deep-link).
        cue_offsets: The real cue ``cue_start_seconds`` values (may be empty).

    Returns:
        A single-chapter list anchored at the first cue, or ``[]`` if no cues.
    """
    if not cue_offsets:
        return []
    first_offset = cue_offsets[0]
    return [
        Chapter(
            title="Full episode",
            start_seconds=first_offset,
            deep_link=build_deep_link(video_id, first_offset),
        )
    ]


def chapterize_episode(
    upload: Any,
    transcript: Transcript | None,
    *,
    segmenter: ChapterSegmenter = _default_chapter_segmenter,
) -> list[Chapter]:
    """Chapterize a single upload into timestamped chapters (design decision 7).

    Returns a ``list[Chapter]`` for the ONE ``upload`` — long-form stays a single
    episode unit; chapters are sub-navigation, not a split. Decision tree:

      1. ``upload.duration`` is None or <= :data:`LONG_FORM_THRESHOLD_SECONDS`
         -> short item, return ``[]`` (short stays short). Logs
         ``chapterize_skipped_short``.
      2. creator ``upload.chapters`` present (non-empty) -> map them verbatim to
         :class:`Chapter`s with deep-links from each ``start_time`` (DETERMINISTIC —
         no LLM). Logs ``chapterize_used_creator_chapters``.
      3. long-form, no creator chapters -> render the cues, call the injected
         segmenter, parse the strict-JSON segment list, SNAP each ``start_seconds``
         to the nearest real cue offset, and build deep-links from those offsets so
         every timestamp traces back to a real cue. Logs ``chapterize_segmented``.
         On an empty transcript or an unparseable verdict, falls back to a single
         chapter at the first cue (or ``[]`` if no cues) — never crashes, never
         invents a timestamp.

    Args:
        upload: An :class:`lib.youtube_yt.Upload` (read for ``video_id``,
            ``duration``, ``chapters``).
        transcript: The cue-preserving :class:`lib.transcribe.Transcript`, or None
            (None is treated as no-cues for the segmentation path).
        segmenter: The injectable segmentation boundary. Defaults to the loud-failing
            stub; tests inject a mock, runtime injects the host session caller.

    Returns:
        The chapters for this single upload (possibly empty).

    Example:
        >>> chapters = chapterize_episode(  # doctest: +SKIP
        ...     upload, transcript,
        ...     segmenter=lambda prompt: '[{"title": "Intro", "start_seconds": 0}]',
        ... )
        >>> chapters[0].deep_link  # doctest: +SKIP
        'https://www.youtube.com/watch?v=abc&t=0s'
    """
    video_id = upload.video_id
    duration = upload.duration

    # 1. Short item -> no chapters (short stays short). None duration is treated as
    #    short: we will not chapterize a video whose length we cannot confirm.
    if duration is None or duration <= LONG_FORM_THRESHOLD_SECONDS:
        log.log_info(
            "chapterize_skipped_short",
            video_id=video_id,
            duration=duration,
            threshold_seconds=LONG_FORM_THRESHOLD_SECONDS,
        )
        return []

    # 2. Creator chapters present -> use them verbatim (DETERMINISTIC, no LLM).
    creator_chapters = upload.chapters
    if creator_chapters:
        chapters = _chapters_from_creator(video_id, creator_chapters)
        log.log_info(
            "chapterize_used_creator_chapters",
            video_id=video_id,
            duration=duration,
            chapter_count=len(chapters),
        )
        return chapters

    # 3. Long-form, no creator chapters -> segment the transcript via the LLM.
    cue_offsets = [cue.cue_start_seconds for cue in transcript.cues] if transcript else []
    if not cue_offsets:
        # Reason: no cues to anchor a chapter to. We never invent a timestamp, so
        # there is nothing to return — and nothing to ask the model about.
        log.log_warning(
            "chapterize_no_cues",
            video_id=video_id,
            duration=duration,
            fix_suggestion=(
                "long-form video has no transcript cues to anchor chapters to; the "
                "item is kept without chapters. Check the transcript fetch for this id."
            ),
        )
        return []

    prompt = _render_prompt(upload.title, transcript)
    raw_verdict = segmenter(prompt)
    parsed_segments = _parse_segments(raw_verdict)

    if not parsed_segments:
        # Reason: unparseable / empty verdict must not crash the run — fall back to a
        # single chapter at the first real cue (never an invented timestamp).
        log.log_warning(
            "chapterize_verdict_unparseable",
            video_id=video_id,
            duration=duration,
            fix_suggestion=(
                "segmenter did not return a strict JSON array of segments; fell back to "
                "a single chapter at the first cue. Tune references/chapterize.md's "
                "output contract if frequent."
            ),
        )
        return _single_chapter_fallback(video_id, cue_offsets)

    chapters = _chapters_from_segments(video_id, parsed_segments, cue_offsets)
    if not chapters:
        # Reason: the model returned a list but no segment carried a usable offset —
        # fall back rather than return an empty chapter list for a long-form video.
        log.log_warning(
            "chapterize_no_usable_segments",
            video_id=video_id,
            duration=duration,
            fix_suggestion=(
                "no segment carried a numeric start_seconds; fell back to a single "
                "chapter at the first cue. Tune references/chapterize.md if frequent."
            ),
        )
        return _single_chapter_fallback(video_id, cue_offsets)

    log.log_info(
        "chapterize_segmented",
        video_id=video_id,
        duration=duration,
        chapter_count=len(chapters),
    )
    return chapters
