"""YouTube subscriptions loader (Stage 0) for Orbit.

Stage 0 of the Orbit pipeline: read the user's YouTube subscription list (the
channels they follow) via ``yt-dlp`` against the authenticated ``/feed/channels``
endpoint, and persist each channel into the ``sources`` table so the Phase-2
delta engine has a set of channels to diff for new uploads.

Authentication is via browser cookies (``--cookies-from-browser <browser>``); the
``cookie_source`` is a browser NAME (e.g. ``chrome``), never a raw cookie value.
Per Orbit's hard security rule (conventions.md §Logging, brief §4/§8.5), cookie
values are NEVER logged: this module redacts the cookie surface (the constructed
argv and any yt-dlp stderr) at the logging boundary before emitting anything.

On an auth failure (no cookies / expired session / sign-in required), this module
fails LOUD and ACTIONABLE: it raises :class:`YouTubeAuthError` with a message that
tells the user to log into YouTube in their browser and points at the README §8.6
troubleshooting section — never a silent death and never a raw stack trace.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Make ``lib`` and ``store`` importable whether this module is imported as the
# package member ``lib.youtube_yt`` (via orbit.py's sys.path insert of the scripts
# dir) or run from the scripts dir directly. Mirrors store.py's sys.path pattern so
# the imports below resolve in both cases. ``lib/`` is this file's parent; the
# scripts dir (which holds ``store.py``) is its grandparent.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (import must follow the sys.path inserts above)
from lib import log, subproc  # noqa: E402

# yt-dlp invocation budget. The subscriptions feed is a single flat-playlist dump
# (no per-video work), so 120s is generous headroom for a slow network / large
# subscription list while still bounding a hung process (subproc kills the group).
_YT_DLP_TIMEOUT_SECONDS: int = 120

# The authenticated YouTube subscriptions feed. ``--flat-playlist`` keeps it to one
# entry per channel (no per-channel expansion); ``--dump-json`` emits NDJSON.
_YOUTUBE_SUBSCRIPTIONS_FEED_URL: str = "https://www.youtube.com/feed/channels"

# yt-dlp invocation budget for the per-channel uploads listing. ``--flat-playlist``
# means yt-dlp lists the channel's uploads without per-video network work, so 120s is
# generous headroom for a slow network / large channel while still bounding a hung
# process (subproc kills the group on timeout).
_YT_DLP_UPLOADS_TIMEOUT_SECONDS: int = 120

# Template for a channel's public uploads URL. The delta listing reads PUBLIC uploads
# and therefore needs NO cookies (see fetch_new_uploads docstring) — the channel
# external_id (e.g. ``UCxxxx``) is substituted in.
_YOUTUBE_CHANNEL_UPLOADS_URL_TEMPLATE: str = "https://www.youtube.com/channel/{channel_id}/videos"

# Where the user is sent to fix an auth failure. Kept as a constant so the wording
# is consistent across the raised error and the structured error log.
_README_TROUBLESHOOTING_POINTER: str = "README §8.6 (troubleshooting)"

# Case-insensitive substrings in yt-dlp stderr that signal an auth/cookie problem
# rather than a generic failure. Robust subset per the spec — the important ones
# are "no cookies" / "could not find" / "sign in" / "login". We treat any non-zero
# return code as a failure too (see _stderr_indicates_auth_failure's caller), so
# this list only needs to catch auth signals that might co-occur with a zero exit.
_AUTH_FAILURE_STDERR_SIGNALS: tuple[str, ...] = (
    "no cookies",
    "could not find",
    "unable to obtain",
    "sign in",
    "login required",
    "login",
    "consent",
    "not logged in",
    "cookies database",
)


@dataclass
class Subscription:
    """A single YouTube channel the user is subscribed to.

    Attributes:
        channel_id: The channel's stable external id (e.g. ``UCxxxxxxxx``). Used as
            ``external_id`` in the ``sources`` table.
        display_name: The human-readable channel name.
    """

    channel_id: str
    display_name: str


@dataclass
class Upload:
    """A single video upload listed from a channel's public uploads feed (Stage 1a).

    Field shape is the contract the rest of Phase 2 consumes: Sub-phase 2 transcribes
    by ``video_id``; Sub-phase 4 chapterizes using ``duration`` (the long-form
    threshold is ``duration > 1200``) and the creator ``chapters`` (parsed here from
    the same yt-dlp ``--dump-json`` entry as of Sub-phase 4).

    Attributes:
        video_id: The video's stable external id (e.g. ``dQw4w9WgXcQ``). Used as
            ``item_external_id`` in the ``seen`` / ``classifications`` tables.
        title: The video title.
        description: The video description (full text as yt-dlp returns it).
        upload_date: yt-dlp's ``upload_date`` field, ``YYYYMMDD`` (or empty if absent).
        view_count: View count, or None if yt-dlp did not provide it.
        like_count: Like count, or None if yt-dlp did not provide it.
        comment_count: Comment count, or None if yt-dlp did not provide it.
        duration: Video duration in seconds, or None. Sub-phase 4's long-form check.
        channel_name: Human-readable channel name (entry ``channel`` or the source
            display_name fallback).
        chapters: Creator-supplied chapters as the raw yt-dlp array of
            ``{title, start_time, end_time}`` dicts, or None when the video has none.
            Sub-phase 4's chapterize uses these verbatim (deterministic, no LLM) when
            present for a long-form video.
    """

    video_id: str
    title: str
    description: str
    upload_date: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    duration: int | None
    channel_name: str
    chapters: list[dict] | None = None


class YouTubeAuthError(Exception):
    """Raised when yt-dlp cannot authenticate to the YouTube subscriptions feed.

    Carries an actionable, user-facing message (log into the browser, see the README
    troubleshooting section) — never a raw stack trace or any cookie material.
    """


class YouTubeFetchError(Exception):
    """Raised when the per-channel uploads listing fails (timeout / yt-dlp missing).

    Mirrors :class:`YouTubeAuthError`'s loud-and-actionable contract: carries a clear,
    user-facing message pointing at the README troubleshooting section — never a raw
    stack trace. Distinct from :class:`YouTubeAuthError` because the cookie-free uploads
    listing has no auth surface; its failure modes are an unreachable network (timeout)
    or a missing yt-dlp binary, not an auth problem.
    """


def _build_subscriptions_command(cookie_source: str) -> list[str]:
    """Build the yt-dlp argv for dumping the authenticated subscriptions feed.

    Built as a list (never a shell string) so ``cookie_source`` and the URL cannot
    be reinterpreted by a shell — closes the door on argument injection.

    Args:
        cookie_source: Browser name to read cookies from (e.g. ``chrome``).

    Returns:
        The argv list to pass to :func:`lib.subproc.run_with_timeout`.
    """
    return [
        "yt-dlp",
        "--cookies-from-browser",
        cookie_source,
        "--flat-playlist",
        "--dump-json",
        _YOUTUBE_SUBSCRIPTIONS_FEED_URL,
    ]


def _stderr_indicates_auth_failure(stderr: str) -> bool:
    """Return True if yt-dlp stderr contains a known auth/cookie failure signal.

    Args:
        stderr: Raw stderr text from the yt-dlp run.

    Returns:
        True if any case-insensitive auth signal substring is present.
    """
    stderr_lower = stderr.lower()
    return any(signal in stderr_lower for signal in _AUTH_FAILURE_STDERR_SIGNALS)


def _scrub_cookie_surface(text: str, cookie_source: str) -> str:
    """Remove the cookie-source token and any cookie-like material from log text.

    The browser name is not itself a secret, but per the hard rule we treat the whole
    cookie surface as sensitive: we drop the ``cookie_source`` token wherever it
    appears and redact lines that look like they carry cookie material, so neither the
    constructed argv nor stderr can leak a credential into the log stream.

    Args:
        text: The stderr (or joined argv) about to be logged.
        cookie_source: The browser name passed by the caller, scrubbed out by name.

    Returns:
        A scrubbed copy safe to attach to a structured log field.
    """
    scrubbed_lines: list[str] = []
    for raw_line in text.splitlines():
        line_lower = raw_line.lower()
        if "cookie" in line_lower or "auth_token" in line_lower or "ct0" in line_lower:
            # Reason: a stderr line mentioning cookies could echo a cookie value
            # (or a cookie-DB path); drop its content entirely rather than risk it.
            scrubbed_lines.append("[REDACTED cookie-related line]")
            continue
        if cookie_source and cookie_source in raw_line:
            raw_line = raw_line.replace(cookie_source, "[REDACTED]")
        scrubbed_lines.append(raw_line)
    return "\n".join(scrubbed_lines)


def _extract_channel_id(entry: dict) -> str:
    """Extract a channel id from one yt-dlp NDJSON entry, defensively.

    Prefers ``channel_id``, falls back to ``id``, then derives from a channel ``url``
    (``.../channel/<id>``) if present. yt-dlp's channel-feed entry shapes vary, so we
    try several keys rather than assume one.

    Args:
        entry: One parsed JSON object from the ``--dump-json`` stream.

    Returns:
        The channel id, or an empty string if none could be found.
    """
    channel_id = entry.get("channel_id") or entry.get("id") or ""
    if channel_id:
        return str(channel_id)
    url = entry.get("url") or entry.get("channel_url") or ""
    if isinstance(url, str) and "/channel/" in url:
        # Reason: a flat-playlist entry may carry only a URL; the last non-empty path
        # segment after ``/channel/`` is the channel id.
        tail = url.split("/channel/", 1)[1]
        return tail.split("/", 1)[0].split("?", 1)[0]
    return ""


def _extract_display_name(entry: dict, channel_id: str) -> str:
    """Extract a human-readable channel name from one NDJSON entry, defensively.

    Prefers ``channel``, then ``uploader``, then ``title``; falls back to the
    ``channel_id`` so a row is never nameless.

    Args:
        entry: One parsed JSON object from the ``--dump-json`` stream.
        channel_id: The already-extracted channel id, used as a last-resort name.

    Returns:
        The display name string.
    """
    display_name = entry.get("channel") or entry.get("uploader") or entry.get("title") or ""
    return str(display_name) if display_name else channel_id


def load_youtube_subscriptions(cookie_source: str) -> list[Subscription]:
    """Load the user's YouTube subscriptions via yt-dlp and browser cookies.

    Runs ``yt-dlp --cookies-from-browser <cookie_source> --flat-playlist --dump-json
    https://www.youtube.com/feed/channels`` (argv list, never a shell string) and
    parses the NDJSON output into :class:`Subscription` records.

    Auth failure (non-zero exit, or stderr carrying a known cookie/sign-in signal) is
    surfaced LOUDLY as :class:`YouTubeAuthError` with an actionable message pointing at
    the README troubleshooting steps — never a silent return and never a stack trace.
    Cookie material is scrubbed from anything logged.

    Args:
        cookie_source: Browser name to read YouTube cookies from (e.g. ``chrome``,
            ``firefox``, ``safari``, ``edge``, ``brave``).

    Returns:
        A list of :class:`Subscription` records, one per subscribed channel.

    Raises:
        YouTubeAuthError: If yt-dlp fails to authenticate (no/expired cookies) or
            exits non-zero, or if it times out.

    Example:
        >>> subscriptions = load_youtube_subscriptions("chrome")  # doctest: +SKIP
        >>> subscriptions[0].channel_id  # doctest: +SKIP
        'UCxxxxxxxxxxxxxxxxxxxxxx'
    """
    command = _build_subscriptions_command(cookie_source)
    log.log_info("youtube_subscriptions_load_started", cookie_source=cookie_source)

    try:
        result = subproc.run_with_timeout(command, timeout=_YT_DLP_TIMEOUT_SECONDS)
    except subproc.SubprocTimeout as exc:
        # Reason: a timeout is not an auth problem, but it must still be loud and
        # actionable — surface it as a clear error, not a swallowed exception.
        log.log_error(
            "youtube_subscriptions_timed_out",
            fix_suggestion=(
                "yt-dlp took longer than "
                f"{_YT_DLP_TIMEOUT_SECONDS}s. Check your network and re-run; "
                f"see {_README_TROUBLESHOOTING_POINTER}."
            ),
            cookie_source=cookie_source,
            timeout_seconds=_YT_DLP_TIMEOUT_SECONDS,
        )
        raise YouTubeAuthError(
            f"yt-dlp timed out after {_YT_DLP_TIMEOUT_SECONDS}s while loading your "
            "YouTube subscriptions. Check your network connection and re-run. "
            f"If it persists, see {_README_TROUBLESHOOTING_POINTER}."
        ) from exc
    except FileNotFoundError as exc:
        log.log_error(
            "youtube_subscriptions_yt_dlp_missing",
            fix_suggestion="Install yt-dlp (e.g. `pip install yt-dlp`) and re-run.",
            cookie_source=cookie_source,
        )
        raise YouTubeAuthError(
            "yt-dlp is not installed. Install it (e.g. `pip install yt-dlp`) and "
            f"re-run. See {_README_TROUBLESHOOTING_POINTER}."
        ) from exc

    auth_failed = result.returncode != 0 or _stderr_indicates_auth_failure(result.stderr)
    if auth_failed:
        # Scrub the cookie surface (browser name + any cookie-like stderr lines)
        # BEFORE it reaches the log stream. log.redact() catches credential-named
        # fields; we additionally scrub free-text stderr that the redactor can't see.
        scrubbed_stderr = _scrub_cookie_surface(result.stderr, cookie_source)
        log.log_error(
            "youtube_subscriptions_auth_failed",
            fix_suggestion=(
                "Log into YouTube in your browser (the one named by cookie_source), "
                f"then re-run. See {_README_TROUBLESHOOTING_POINTER}."
            ),
            cookie_source=cookie_source,
            return_code=result.returncode,
            yt_dlp_stderr=scrubbed_stderr,
        )
        raise YouTubeAuthError(
            "Could not read your YouTube subscriptions — authentication failed "
            "(no cookies found, or your session has expired). "
            f"Log into YouTube in your browser ('{cookie_source}') and re-run. "
            f"For step-by-step help, see {_README_TROUBLESHOOTING_POINTER}."
        )

    subscriptions = _parse_subscriptions_ndjson(result.stdout)
    log.log_info(
        "youtube_subscriptions_load_completed",
        cookie_source=cookie_source,
        count=len(subscriptions),
    )
    return subscriptions


def _parse_subscriptions_ndjson(stdout: str) -> list[Subscription]:
    """Parse yt-dlp ``--dump-json`` NDJSON stdout into Subscription records.

    One JSON object per line. Blank lines and lines that fail to parse (or that carry
    no usable channel id) are skipped rather than crashing the whole load — a single
    malformed line must not lose the entire subscription list. A skipped-line count is
    logged once as a warning.

    Args:
        stdout: The full stdout from the yt-dlp run.

    Returns:
        A list of :class:`Subscription` records in feed order.
    """
    subscriptions: list[Subscription] = []
    skipped_line_count = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped_line_count += 1
            continue
        if not isinstance(entry, dict):
            skipped_line_count += 1
            continue
        channel_id = _extract_channel_id(entry)
        if not channel_id:
            skipped_line_count += 1
            continue
        display_name = _extract_display_name(entry, channel_id)
        subscriptions.append(Subscription(channel_id=channel_id, display_name=display_name))

    if skipped_line_count:
        log.log_warning(
            "youtube_subscriptions_lines_skipped",
            skipped_line_count=skipped_line_count,
            parsed_count=len(subscriptions),
        )
    return subscriptions


def persist_subscriptions(subscriptions: list[Subscription]) -> int:
    """Persist subscriptions into the ``sources`` table (one upsert per channel).

    Each channel is upserted with ``platform="youtube"``, ``category="signal"``, and
    ``last_refreshed_at`` set to the current UTC time (ISO-8601), so the weekly Stage-0
    cache check (Phase-2/Sub-phase-4) can tell how fresh the list is. Upsert dedups on
    ``(platform, external_id)`` so a repeat load updates rather than duplicates.

    Args:
        subscriptions: The records to persist (from :func:`load_youtube_subscriptions`).

    Returns:
        The number of channels persisted.

    Example:
        >>> count = persist_subscriptions(
        ...     [Subscription(channel_id="UC123", display_name="Some Channel")]
        ... )  # doctest: +SKIP
        >>> count  # doctest: +SKIP
        1
    """
    refreshed_at = datetime.now(timezone.utc).isoformat()
    for subscription in subscriptions:
        store.upsert_source(
            platform="youtube",
            external_id=subscription.channel_id,
            display_name=subscription.display_name,
            category="signal",
            last_refreshed_at=refreshed_at,
        )
    persisted_count = len(subscriptions)
    log.log_info("youtube_subscriptions_persisted", count=persisted_count)
    return persisted_count


# --- Delta detection of new uploads (Stage 1a) -----------------------------


def _build_uploads_command(channel_id: str) -> list[str]:
    """Build the yt-dlp argv for listing a channel's public uploads.

    Built as a list (never a shell string) so ``channel_id`` cannot be reinterpreted
    by a shell — closes the door on argument injection. Deliberately cookie-free: the
    public uploads listing needs no authentication (see :func:`fetch_new_uploads`).

    Args:
        channel_id: The channel's external id (e.g. ``UCxxxx``).

    Returns:
        The argv list to pass to :func:`lib.subproc.run_with_timeout`.
    """
    uploads_url = _YOUTUBE_CHANNEL_UPLOADS_URL_TEMPLATE.format(channel_id=channel_id)
    return [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        uploads_url,
    ]


def _coerce_optional_int(value: object) -> int | None:
    """Coerce a yt-dlp numeric field to ``int`` or ``None``, never crashing.

    yt-dlp may emit ints, ``None``, or (rarely) a numeric string for count/duration
    fields. We accept int/float/numeric-string and fall back to ``None`` on anything
    unparseable rather than raising — a single odd field must not lose the whole upload.

    Args:
        value: The raw field value from the parsed JSON entry.

    Returns:
        The value as an int, or None if absent/unparseable.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # Reason: bool is an int subclass; a stray True/False is not a real count.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_uploads_ndjson(stdout: str, source_display_name: str) -> list[Upload]:
    """Parse yt-dlp ``--dump-json`` NDJSON stdout into Upload records.

    One JSON object per line. Blank lines, lines that fail to parse, and lines that
    carry no usable ``video_id`` are skipped rather than crashing the whole listing —
    a single malformed line must not lose the entire upload feed. A skipped-line count
    is logged once as a warning (mirrors :func:`_parse_subscriptions_ndjson`).

    Args:
        stdout: The full stdout from the yt-dlp run.
        source_display_name: Fallback channel name when an entry omits ``channel``.

    Returns:
        A list of :class:`Upload` records in feed order.
    """
    uploads: list[Upload] = []
    skipped_line_count = 0
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped_line_count += 1
            continue
        if not isinstance(entry, dict):
            skipped_line_count += 1
            continue
        video_id = entry.get("id") or entry.get("video_id") or ""
        if not video_id:
            skipped_line_count += 1
            continue
        uploads.append(
            Upload(
                video_id=str(video_id),
                title=str(entry.get("title") or ""),
                description=str(entry.get("description") or ""),
                upload_date=str(entry.get("upload_date") or ""),
                view_count=_coerce_optional_int(entry.get("view_count")),
                like_count=_coerce_optional_int(entry.get("like_count")),
                comment_count=_coerce_optional_int(entry.get("comment_count")),
                duration=_coerce_optional_int(entry.get("duration")),
                channel_name=str(entry.get("channel") or source_display_name or ""),
                chapters=entry.get("chapters"),
            )
        )

    if skipped_line_count:
        log.log_warning(
            "youtube_uploads_lines_skipped",
            skipped_line_count=skipped_line_count,
            parsed_count=len(uploads),
        )
    return uploads


def fetch_new_uploads(source: dict, depth: str) -> list[Upload]:
    """List a channel's uploads and return only those NOT already in ``seen`` (delta).

    Runs ``yt-dlp --flat-playlist --dump-json
    https://www.youtube.com/channel/<external_id>/videos`` (argv list, never a shell
    string) and parses the NDJSON output into :class:`Upload` records, then filters out
    any whose ``video_id`` is already in ``store.get_seen_ids(source["source_id"])`` —
    so previously-seen uploads never resurface as new (the delta intent).

    This function is intentionally COOKIE-FREE: the public uploads listing needs no
    authentication, and the phase scope restricts the command to ``--flat-playlist
    --dump-json``. It therefore does NOT mark items as seen — marking is deferred to the
    pipeline driver AFTER a successful run, so a crash mid-pipeline never silently drops
    items by pre-marking them.

    Failure modes are surfaced LOUDLY as :class:`YouTubeFetchError` with an actionable
    message (mirrors the :class:`YouTubeAuthError` handling): a timeout or a missing
    yt-dlp binary, never a silent return and never a raw stack trace.

    Args:
        source: A source row dict from :func:`store.list_sources` — must carry
            ``source_id``, ``external_id`` (the channel id), and ``display_name``.
        depth: The run depth (``quick`` / ``default`` / ``deep``). Logged and passed
            through for traceability; it does NOT gate the upload listing here. The
            per-channel transcription cap (``TRANSCRIPT_LIMITS``) belongs to Sub-phase 2.

    Returns:
        A list of :class:`Upload` records whose ``video_id`` is not yet in ``seen``.

    Raises:
        YouTubeFetchError: If yt-dlp times out or is not installed.

    Example:
        >>> source = {"source_id": 1, "external_id": "UC123", "display_name": "Chan"}
        >>> new_uploads = fetch_new_uploads(source, depth="default")  # doctest: +SKIP
        >>> new_uploads[0].video_id  # doctest: +SKIP
        'dQw4w9WgXcQ'
    """
    source_id = source["source_id"]
    channel_id = source["external_id"]
    source_display_name = source.get("display_name") or ""
    command = _build_uploads_command(channel_id)
    log.log_info(
        "delta_fetch_started",
        source_id=source_id,
        channel_id=channel_id,
        depth=depth,
    )

    try:
        result = subproc.run_with_timeout(command, timeout=_YT_DLP_UPLOADS_TIMEOUT_SECONDS)
    except subproc.SubprocTimeout as exc:
        log.log_error(
            "delta_fetch_timed_out",
            fix_suggestion=(
                "yt-dlp took longer than "
                f"{_YT_DLP_UPLOADS_TIMEOUT_SECONDS}s listing channel uploads. "
                f"Check your network and re-run; see {_README_TROUBLESHOOTING_POINTER}."
            ),
            source_id=source_id,
            channel_id=channel_id,
            timeout_seconds=_YT_DLP_UPLOADS_TIMEOUT_SECONDS,
        )
        raise YouTubeFetchError(
            f"yt-dlp timed out after {_YT_DLP_UPLOADS_TIMEOUT_SECONDS}s while listing "
            f"uploads for channel '{channel_id}'. Check your network connection and "
            f"re-run. If it persists, see {_README_TROUBLESHOOTING_POINTER}."
        ) from exc
    except FileNotFoundError as exc:
        log.log_error(
            "delta_fetch_yt_dlp_missing",
            fix_suggestion="Install yt-dlp (e.g. `pip install yt-dlp`) and re-run.",
            source_id=source_id,
            channel_id=channel_id,
        )
        raise YouTubeFetchError(
            "yt-dlp is not installed. Install it (e.g. `pip install yt-dlp`) and "
            f"re-run. See {_README_TROUBLESHOOTING_POINTER}."
        ) from exc

    all_uploads = _parse_uploads_ndjson(result.stdout, source_display_name)
    seen_video_ids = store.get_seen_ids(source_id)
    # Reason: delta — return only uploads whose video_id is not already in `seen` so a
    # previously-seen upload never re-appears as new. mark_seen is the pipeline driver's
    # job (post-success), NOT here, to avoid dropping items on a mid-pipeline crash.
    new_uploads = [upload for upload in all_uploads if upload.video_id not in seen_video_ids]

    log.log_info(
        "delta_fetch_completed",
        source_id=source_id,
        channel_id=channel_id,
        depth=depth,
        count=len(new_uploads),
    )
    return new_uploads
