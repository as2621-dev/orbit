"""Pure Stage-1 YouTube inclusion helpers (Phase 8 / Sub-phase 2).

Extracted from ``orbit.py`` to keep the driver under the 1000-line CLAUDE.md limit
while the Stage-1 YouTube half gained a long-form inclusion floor. These are pure,
deterministic functions (Rule 5 — no model in the decision): they window/cap a
channel's new uploads and drop short-form clips BEFORE the classify call, so the LLM
budget is only ever spent on long-form candidates.

``orbit.py`` re-imports ``_select_recent_uploads`` so its stage-1 call site and the
existing ``tests/test_orbit_youtube_producer.py`` references (``orbit._select_recent_uploads``)
resolve unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Mirror the sys.path dance in classify.py / youtube_yt.py so ``from lib import log``
# and ``from lib.youtube_yt import Upload`` resolve whether this module is imported as
# ``lib.stage1_youtube`` (via orbit.py's scripts-dir insert) or from the scripts dir.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402
from lib.youtube_yt import Upload  # noqa: E402

# The 10-minute long-form inclusion floor (2026-07-06 ruling): YouTube goes long-form
# only. Uploads shorter than this never reach the classifier. Distinct from
# ``chapterize.LONG_FORM_THRESHOLD_SECONDS`` (1200s), which gates chapterize eligibility,
# NOT digest inclusion.
MIN_UPLOAD_DURATION_SECONDS: int = 600


def drop_short_form_uploads(uploads: list[Upload]) -> tuple[list[Upload], int]:
    """Split uploads into long-form keepers and count the short-form ones dropped.

    An upload is dropped only when it has a KNOWN duration below
    :data:`MIN_UPLOAD_DURATION_SECONDS`. A ``duration is None`` upload is KEPT
    (fail-open): commit ``654f0fb`` taught us that missing metadata must never nuke an
    item — an absent duration is a metadata gap, not a signal that the clip is short.

    Args:
        uploads: A channel's recency-windowed uploads, in feed order.

    Returns:
        A ``(long_form_uploads, short_form_dropped_count)`` tuple: the uploads that pass
        the floor (order preserved) and how many known-short uploads were dropped.

    Example:
        >>> long_form, dropped = drop_short_form_uploads(uploads)
        >>> dropped
        2
    """
    long_form_uploads: list[Upload] = []
    short_form_dropped_count = 0
    for upload in uploads:
        duration = getattr(upload, "duration", None)
        # Reason: fail-open on a missing duration — keep it and let classify judge it.
        # Only a duration we KNOW is below the floor is a drop.
        if duration is not None and duration < MIN_UPLOAD_DURATION_SECONDS:
            short_form_dropped_count += 1
            continue
        long_form_uploads.append(upload)
    return long_form_uploads, short_form_dropped_count


def _select_recent_uploads(
    uploads: list[Upload],
    *,
    recency_cutoff: str,
    per_channel_cap: int,
) -> list[Upload]:
    """Keep a channel's recent uploads, newest first, capped to ``per_channel_cap``.

    Bounds the cold-DB first run (where the delta engine marks an entire back-catalogue
    "new"): only uploads with an ``upload_date`` (``YYYYMMDD``) on/after ``recency_cutoff``
    survive, sorted newest-first, then truncated to the cap. ``YYYYMMDD`` strings compare
    lexically == chronologically.

    Positional fallback (fail loud): if the channel returns NO dated uploads at all
    (yt-dlp's flat listing occasionally omits dates even with ``approximate_date``), we
    can't drop the whole channel — that is exactly the YouTube-dropout bug. Instead we take
    the newest ``per_channel_cap`` uploads by feed order (the channel ``/videos`` listing is
    newest-first, and ``fetch_new_uploads`` preserves that order) and log a warning. When at
    least one dated upload exists we trust the dates and never fall back.

    Args:
        uploads: The channel's new (unseen) uploads from the delta engine, in feed order.
        recency_cutoff: Inclusive lower bound as a ``YYYYMMDD`` string.
        per_channel_cap: Max uploads to return for this channel.

    Returns:
        The newest in-window uploads, at most ``per_channel_cap`` of them.

    Example:
        >>> _select_recent_uploads(uploads, recency_cutoff="20260620", per_channel_cap=5)
        [<newest>, ...]
    """
    dated = [upload for upload in uploads if upload.upload_date]
    if not dated and uploads:
        # Reason: zero dates for a non-empty channel means we cannot place any upload in
        # time. Rather than silently dropping the channel (the dropout bug), fall back to
        # newest-by-feed-order and surface it loudly (Rule 12).
        log.log_warning(
            "youtube_stage1_upload_dates_missing",
            channel_name=uploads[0].channel_name,
            upload_count=len(uploads),
            per_channel_cap=per_channel_cap,
            fix_suggestion=(
                "yt-dlp returned no upload_date for this channel even with "
                "youtubetab:approximate_date; using newest-by-feed-order fallback. If this "
                "recurs widely, check the yt-dlp version / extractor args."
            ),
        )
        return uploads[:per_channel_cap]
    recent = [upload for upload in dated if upload.upload_date >= recency_cutoff]
    recent.sort(key=lambda upload: upload.upload_date, reverse=True)
    return recent[:per_channel_cap]
