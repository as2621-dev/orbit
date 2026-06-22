"""VTT transcript fetch retaining cue timestamps (Stage 1b) for Orbit.

This module exists because of one product-defining design decision: **VTT cue
timestamps must SURVIVE the transcript fetch.** The last30days reference
flattens a transcript to plaintext in ``_clean_vtt()`` (it runs
``re.sub(r'\\d{2}:\\d{2}:\\d{2}\\.\\d{3}\\s*-->...')`` and DELETES every cue
offset). Orbit does the OPPOSITE: :func:`parse_vtt_cues` is a cue-PRESERVING
parser that keeps each cue's ``cue_start_seconds`` / ``cue_end_seconds`` as
floats.

Why the timestamps matter: they power the headline feature. Chapterization
(Sub-phase 4) segments a long-form video and labels each segment with the
``cue_start_seconds`` of its first cue; :func:`build_deep_link` turns that
offset into a ``watch?v=ID&t=Ns`` URL that drops the user into the exact moment
of the video. If the offsets were flattened away (as in the reference), every
chapter would point at second 0 and the "jump to the moment" feature would be
impossible. So the cue list is the load-bearing artifact here; the joined
plaintext (:meth:`Transcript.plain_text`, used for classify/embed) is a derived
convenience that NEVER destroys the underlying cues.

The yt-dlp VTT command flags are lifted verbatim from the reference
(``--write-auto-subs --sub-lang en,es,pt --sub-format vtt --skip-download``) and
built as an argv LIST (never a shell string) so the ``video_id`` cannot be
reinterpreted by a shell. yt-dlp writes the ``.vtt`` into a temp dir
(``-o <tmp>/%(id)s``); we read it back and parse it through
:func:`parse_vtt_cues`, NOT ``_clean_vtt``.
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.transcribe`` (via orbit.py's sys.path insert of the scripts dir) or run
# from the scripts dir directly. Mirrors youtube_yt.py's sys.path pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log, subproc  # noqa: E402  (import follows the sys.path inserts above)

# Lifted verbatim from the last30days reference (youtube_yt.py:31-35,57). The
# per-run transcription budget by depth: ``quick`` skips transcription entirely
# (0), ``default`` keeps the cost low (2), ``deep`` goes wide (8). The numeric
# cap across many videos is applied by the pipeline DRIVER's loop; this module
# enforces the ``quick == 0`` gate (see fetch_transcript_with_cues).
TRANSCRIPT_LIMITS: dict[str, int] = {
    "quick": 0,
    "default": 2,
    "deep": 8,
}

# Per-run SUMMARY budget (the cluster-winner summarization stage). Unlike the
# transcript budget above (which deliberately fetches 0 on quick), summarization
# must always produce a useful digest, so every depth's cap is >= SUMMARY_FLOOR.
# The pipeline picks the top-N winners by rank up to max(SUMMARY_FLOOR, cap[depth])
# and transcribes EACH (passing force=True to bypass the quick==0 transcript gate);
# the remaining winners render as cards/links without a summary.
SUMMARY_FLOOR: int = 8
SUMMARY_CAP_BY_DEPTH: dict[str, int] = {
    "quick": 8,
    "default": 12,
    "deep": 24,
}
# A separate, smaller cap for X-post (tweet) summaries — tweets are cheap to
# summarize (no transcript) but we still bound the per-run LLM call volume.
X_SUMMARY_CAP_BY_DEPTH: dict[str, int] = {
    "quick": 4,
    "default": 8,
    "deep": 16,
}

# Lifted verbatim from the reference (youtube_yt.py:57). Word cap applied across
# the joined cue text so classify/embed inputs stay bounded; trailing cues past
# the cap are dropped, but the cues we KEEP retain their timestamps intact.
TRANSCRIPT_MAX_WORDS: int = 5000

# Caption languages tried, lifted verbatim from the reference's default
# (en,es,pt). Passed as a single comma-joined argv token, matching the reference.
_SUB_LANGS: str = "en,es,pt"

# yt-dlp transcript-fetch budget. The reference uses 30s per video; we allow a
# little headroom (45s) for a slow caption fetch while still bounding a hung
# process (subproc kills the process group on timeout).
_YT_DLP_TRANSCRIPT_TIMEOUT_SECONDS: int = 45

# A VTT cue timing line: ``HH:MM:SS.mmm --> HH:MM:SS.mmm`` with the hours group
# optional so ``MM:SS.mmm --> MM:SS.mmm`` is also tolerated. Trailing cue
# settings (position/align, e.g. ``align:start position:0%``) after the end
# timestamp are matched-and-ignored by the open-ended ``.*`` tail.
_CUE_TIMING_RE = re.compile(
    r"^\s*"
    r"(?:(?P<start_hours>\d{1,2}):)?(?P<start_minutes>\d{2}):(?P<start_seconds>\d{2}\.\d{3})"
    r"\s*-->\s*"
    r"(?:(?P<end_hours>\d{1,2}):)?(?P<end_minutes>\d{2}):(?P<end_seconds>\d{2}\.\d{3})"
    r".*$"
)

# Inline cue tags to strip from cue text, e.g. ``<00:00:01.000>`` karaoke
# timing tags and ``<c>``/``</c>`` styling tags. Stripping these leaves clean
# human-readable text while the structural cue offsets stay on the cue object.
_INLINE_TAG_RE = re.compile(r"<[^>]+>")

# A line that is ONLY a cue number (the optional numeric id before a timing
# line). Skipped so it never lands in cue text.
_CUE_NUMBER_RE = re.compile(r"^\s*\d+\s*$")


@dataclass
class TranscriptCue:
    """A single timed VTT cue whose offsets SURVIVE parsing (the core invariant).

    Attributes:
        cue_start_seconds: Cue start as TOTAL seconds (e.g. ``00:01:30.000`` ->
            ``90.0``). This is the offset chapterize/deep-links trace back to.
        cue_end_seconds: Cue end as total seconds.
        text: The cue's human-readable text with inline ``<...>`` tags stripped.
    """

    cue_start_seconds: float
    cue_end_seconds: float
    text: str


@dataclass
class Transcript:
    """A fetched transcript: the timed cue list PLUS a derived plaintext view.

    The ``cues`` list is the load-bearing artifact (chapterize + deep-links read
    ``cue_start_seconds`` off it). :meth:`plain_text` joins cue texts for
    classify/embed WITHOUT mutating or destroying the cues — they stay intact on
    the object for the timed downstream stages.

    Attributes:
        video_id: The video this transcript belongs to (e.g. ``dQw4w9WgXcQ``).
        cues: The timed cues in feed order; timestamps preserved as floats.
    """

    video_id: str
    cues: list[TranscriptCue] = field(default_factory=list)

    def plain_text(self) -> str:
        """Join the cue texts into one plaintext string for classify/embed.

        Cues are joined with single spaces in order; the cue list is NOT mutated,
        so timestamps remain available for chapterize/deep-links afterwards.

        Returns:
            The concatenated cue text (empty string if there are no cues).

        Example:
            >>> transcript = Transcript("abc", [TranscriptCue(5.0, 7.0, "Hello world")])
            >>> transcript.plain_text()
            'Hello world'
        """
        return " ".join(cue.text for cue in self.cues if cue.text).strip()

    def word_count(self) -> int:
        """Return the number of whitespace-separated words across all cue text.

        Returns:
            The total word count of :meth:`plain_text`.
        """
        plain = self.plain_text()
        return len(plain.split()) if plain else 0


def _timestamp_to_seconds(hours: str | None, minutes: str, seconds_millis: str) -> float:
    """Convert a parsed VTT timestamp into TOTAL seconds as a float.

    This is the product invariant in arithmetic form: a cue at ``00:01:30.000``
    MUST yield ``90.0`` (hours*3600 + minutes*60 + seconds.millis).

    Args:
        hours: The hours component as a string, or None (``MM:SS.mmm`` form).
        minutes: The minutes component (``MM``).
        seconds_millis: The seconds-with-millis component (``SS.mmm``).

    Returns:
        The total seconds as a float.

    Example:
        >>> _timestamp_to_seconds("00", "01", "30.000")
        90.0
        >>> _timestamp_to_seconds(None, "00", "05.000")
        5.0
    """
    total_hours = int(hours) if hours else 0
    return total_hours * 3600 + int(minutes) * 60 + float(seconds_millis)


def parse_vtt_cues(vtt_text: str) -> list[TranscriptCue]:
    """Parse WEBVTT text into timed cues, PRESERVING each cue's offsets.

    The opposite of the reference's ``_clean_vtt`` flattener: instead of deleting
    the ``HH:MM:SS.mmm --> ...`` lines, we read each one into
    ``cue_start_seconds`` / ``cue_end_seconds`` floats and attach the following
    text line(s). The ``WEBVTT`` header block, cue-number-only lines, and inline
    ``<...>`` tags (karaoke timing / styling) are stripped; trailing position/
    align cue settings after the timestamp are ignored. Both ``HH:MM:SS.mmm`` and
    ``MM:SS.mmm`` timing forms are tolerated.

    Args:
        vtt_text: The raw WEBVTT file contents.

    Returns:
        The cues in document order. Empty if the text carries no parseable cues.

    Example:
        >>> cues = parse_vtt_cues("WEBVTT\\n\\n00:01:30.000 --> 00:01:33.000\\nHi")
        >>> cues[0].cue_start_seconds
        90.0
        >>> cues[0].text
        'Hi'
    """
    cues: list[TranscriptCue] = []
    lines = vtt_text.splitlines()
    line_index = 0
    total_lines = len(lines)

    while line_index < total_lines:
        raw_line = lines[line_index]
        timing_match = _CUE_TIMING_RE.match(raw_line)
        if not timing_match:
            line_index += 1
            continue

        cue_start_seconds = _timestamp_to_seconds(
            timing_match.group("start_hours"),
            timing_match.group("start_minutes"),
            timing_match.group("start_seconds"),
        )
        cue_end_seconds = _timestamp_to_seconds(
            timing_match.group("end_hours"),
            timing_match.group("end_minutes"),
            timing_match.group("end_seconds"),
        )

        # Collect the cue's text line(s): everything up to the next blank line or
        # the next timing line. Cue-number-only lines are skipped, inline tags
        # stripped. Reason: a cue's payload is the lines BELOW its timing line.
        text_parts: list[str] = []
        line_index += 1
        while line_index < total_lines:
            text_line = lines[line_index]
            if not text_line.strip():
                break
            if _CUE_TIMING_RE.match(text_line):
                break
            if _CUE_NUMBER_RE.match(text_line):
                line_index += 1
                continue
            cleaned = _INLINE_TAG_RE.sub("", text_line).strip()
            if cleaned:
                text_parts.append(cleaned)
            line_index += 1

        cue_text = " ".join(text_parts).strip()
        cues.append(
            TranscriptCue(
                cue_start_seconds=cue_start_seconds,
                cue_end_seconds=cue_end_seconds,
                text=cue_text,
            )
        )

    return cues


def _apply_word_cap(cues: list[TranscriptCue], max_words: int) -> list[TranscriptCue]:
    """Truncate trailing cues so the joined cue text stays under ``max_words``.

    Cues are kept in order until the cumulative word count would exceed
    ``max_words``; the cues that are kept retain their timestamps intact (we drop
    whole trailing cues rather than splitting one, so no cue ends up with a
    truncated/desynced offset).

    Args:
        cues: The parsed cues in order.
        max_words: The maximum cumulative word count to keep.

    Returns:
        The kept cues (a prefix of the input); the full list if already under cap.
    """
    kept_cues: list[TranscriptCue] = []
    running_word_count = 0
    for cue in cues:
        cue_word_count = len(cue.text.split()) if cue.text else 0
        if running_word_count + cue_word_count > max_words:
            break
        kept_cues.append(cue)
        running_word_count += cue_word_count
    return kept_cues


def _build_transcript_command(video_id: str, temp_dir: str) -> list[str]:
    """Build the yt-dlp argv for fetching a video's VTT captions into ``temp_dir``.

    Flags lifted verbatim from the reference: ``--write-auto-subs --sub-lang
    en,es,pt --sub-format vtt --skip-download``. Built as a list (never a shell
    string) so ``video_id`` cannot be reinterpreted by a shell — closes the door
    on argument injection. yt-dlp writes ``<temp_dir>/<id>.<lang>.vtt``.

    Args:
        video_id: The video's external id (e.g. ``dQw4w9WgXcQ``).
        temp_dir: Directory yt-dlp writes the ``.vtt`` into (``-o <tmp>/%(id)s``).

    Returns:
        The argv list to pass to :func:`lib.subproc.run_with_timeout`.
    """
    return [
        "yt-dlp",
        "--write-auto-subs",
        "--sub-lang",
        _SUB_LANGS,
        "--sub-format",
        "vtt",
        "--skip-download",
        "-o",
        f"{temp_dir}/%(id)s",
        f"https://www.youtube.com/watch?v={video_id}",
    ]


def _read_produced_vtt(video_id: str, temp_dir: str) -> str | None:
    """Read the ``.vtt`` yt-dlp wrote for ``video_id`` from ``temp_dir``, or None.

    yt-dlp names the file ``<id>.<lang>.vtt`` (e.g. ``dQw4w9WgXcQ.en.vtt``); a
    video may have several language tracks. We pick the first match preferring the
    language order in :data:`_SUB_LANGS`, mirroring the reference's ``_read_vtt``.

    Args:
        video_id: The video's external id (the file stem prefix).
        temp_dir: The directory yt-dlp wrote into.

    Returns:
        The raw VTT text, or None if no matching file exists / cannot be read.
    """
    matches = sorted(Path(temp_dir).glob(f"{video_id}*.vtt"))
    if not matches:
        return None

    language_priority = {code: index for index, code in enumerate(_SUB_LANGS.split(","))}

    def rank(vtt_path: Path) -> int:
        stem = vtt_path.stem
        suffix = stem[len(video_id) + 1 :] if stem.startswith(video_id + ".") else ""
        language_code = suffix.split("-")[0].split(".")[0]
        return language_priority.get(language_code, len(language_priority))

    chosen_path = sorted(matches, key=rank)[0]
    try:
        return chosen_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def fetch_transcript_with_cues(video_id: str, depth: str = "default", *, force: bool = False) -> Transcript | None:
    """Fetch a video's VTT captions and parse them into a cue-preserving Transcript.

    Runs the lifted yt-dlp VTT command into a temp dir, reads the produced
    ``.vtt``, and parses it via :func:`parse_vtt_cues` (NOT the reference's
    ``_clean_vtt``) so cue offsets survive. The :data:`TRANSCRIPT_MAX_WORDS` cap
    is applied across the joined cue text (trailing cues dropped, kept cues keep
    their timestamps).

    Depth gate: this enforces the ``quick == 0`` rule — if
    ``TRANSCRIPT_LIMITS[depth] == 0`` (i.e. ``quick``), it returns ``None``
    WITHOUT running yt-dlp and logs ``transcript_skipped_quick_depth``. The
    NUMERIC per-run cap across many videos (``default`` -> 2, ``deep`` -> 8) is
    the pipeline driver's loop concern, applied by the caller — this function
    only gates the quick=0 case and transcribes a single video.

    Failure modes are handled loudly-but-gracefully: a timeout, a missing yt-dlp
    binary, or a video with no captions logs a warning/error with a
    ``fix_suggestion`` and returns ``None`` rather than crashing. The temp dir is
    always cleaned up.

    Args:
        video_id: The video's external id (e.g. ``dQw4w9WgXcQ``).
        depth: The run depth (``quick`` / ``default`` / ``deep``). ``quick`` skips
            transcription entirely (returns None, no yt-dlp call) UNLESS ``force``.
        force: When True, bypass the ``quick == 0`` skip gate and fetch anyway. The
            summarize-winners stage sets this for the bounded set of cluster winners it
            has chosen to transcribe, so summaries are produced at every depth.

    Returns:
        A :class:`Transcript` with intact cues, or ``None`` if transcription was
        skipped (quick depth) or the fetch produced no usable captions.

    Example:
        >>> transcript = fetch_transcript_with_cues("dQw4w9WgXcQ")  # doctest: +SKIP
        >>> transcript.cues[0].cue_start_seconds  # doctest: +SKIP
        5.0
    """
    transcript_limit = TRANSCRIPT_LIMITS.get(depth, TRANSCRIPT_LIMITS["default"])
    if transcript_limit == 0 and not force:
        # Reason: quick depth deliberately fetches 0 transcripts — skip the yt-dlp
        # call entirely (the DoD: depth=quick fetches 0 transcripts). The
        # summarize-winners stage passes force=True to opt in: it has already chosen a
        # bounded set of cluster winners to transcribe, so the quick=0 feed-wide gate
        # does not apply to it.
        log.log_info("transcript_skipped_quick_depth", video_id=video_id, depth=depth)
        return None

    log.log_info("transcript_fetch_started", video_id=video_id, depth=depth)

    with tempfile.TemporaryDirectory() as temp_dir:
        command = _build_transcript_command(video_id, temp_dir)
        try:
            result = subproc.run_with_timeout(
                command, timeout=_YT_DLP_TRANSCRIPT_TIMEOUT_SECONDS
            )
        except subproc.SubprocTimeout:
            log.log_warning(
                "transcript_fetch_timed_out",
                video_id=video_id,
                timeout_seconds=_YT_DLP_TRANSCRIPT_TIMEOUT_SECONDS,
                fix_suggestion=(
                    "yt-dlp took longer than "
                    f"{_YT_DLP_TRANSCRIPT_TIMEOUT_SECONDS}s fetching captions for "
                    f"video '{video_id}'. Check your network and re-run; the item is "
                    "kept without a transcript."
                ),
            )
            return None
        except FileNotFoundError:
            log.log_error(
                "transcript_fetch_yt_dlp_missing",
                video_id=video_id,
                fix_suggestion="Install yt-dlp (e.g. `pip install yt-dlp`) and re-run.",
            )
            return None

        vtt_text = _read_produced_vtt(video_id, temp_dir)

    # Reason: yt-dlp exits 0 with no file for a video that genuinely lacks the
    # requested captions — that is a no-captions case, not a crash. Return None.
    if not vtt_text or not vtt_text.strip():
        log.log_warning(
            "transcript_no_captions",
            video_id=video_id,
            return_code=result.returncode,
            fix_suggestion=(
                f"No {_SUB_LANGS} captions were available for video '{video_id}'. "
                "The item is kept without a transcript (it cannot be chapterized)."
            ),
        )
        return None

    parsed_cues = parse_vtt_cues(vtt_text)
    capped_cues = _apply_word_cap(parsed_cues, TRANSCRIPT_MAX_WORDS)
    transcript = Transcript(video_id=video_id, cues=capped_cues)

    log.log_info(
        "transcript_fetch_completed",
        video_id=video_id,
        depth=depth,
        cue_count=len(transcript.cues),
        word_count=transcript.word_count(),
    )
    return transcript


def build_deep_link(video_id: str, start_seconds: float) -> str:
    """Build a ``watch?v=ID&t=Ns`` deep-link from a video id and a cue offset.

    The seconds are int-truncated (YouTube's ``t=`` parameter is whole seconds).
    This is the headline feature's URL builder: chapterize attaches a chapter's
    first-cue ``cue_start_seconds`` here so the link drops the user into the exact
    moment.

    Args:
        video_id: The video's external id (e.g. ``abc``).
        start_seconds: The cue offset in seconds (float; truncated to int).

    Returns:
        The deep-link URL string.

    Example:
        >>> build_deep_link("abc", 90.0)
        'https://www.youtube.com/watch?v=abc&t=90s'
        >>> build_deep_link("abc", 90.7)
        'https://www.youtube.com/watch?v=abc&t=90s'
    """
    return f"https://www.youtube.com/watch?v={video_id}&t={int(start_seconds)}s"
