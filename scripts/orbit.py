#!/usr/bin/env python3
"""Orbit pipeline driver — the skill entrypoint.

This module is wiring only (Rule: orbit.py sequences stages, business logic lives
in lib/). For Phase 1 Sub-phase 1 it is a scaffold: it parses ``--depth`` and
``--setup``, then logs a structured "not yet implemented" notice per pipeline
stage. Real Stage 0 (subscription loading), classification, ranking, and render
land in later sub-phases/phases.

Run directly:
    python3 scripts/orbit.py --depth default
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable when this script is run directly (e.g.
# ``python3 scripts/orbit.py``). Mirrors the last30days reference so
# lifted modules import ``from lib import log, subproc`` unchanged in later phases.
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import store  # noqa: E402  (import must follow the sys.path insert above)
from lib import bird_x, classify, deliver, log, markdown_render, render, runlock  # noqa: E402
from lib.bird_x import Follow, Tweet, XAuthError  # noqa: E402
from lib.classify import LlmClassifier, _default_llm_classifier  # noqa: E402
from lib.cluster import Cluster, cluster_overlaps  # noqa: E402
from lib.config import OrbitConfig, load_config  # noqa: E402
from lib.density import TIER_HERO, TIER_STANDARD, TieredItem, assign_density_tiers  # noqa: E402
from lib.llm import LlmCliError, load_dotenv, make_llm_classifier  # noqa: E402
from lib.external_trending import (  # noqa: E402
    build_trending_multiplier_map,
    detect_scoops,
    tag_external_corroboration,
)
from lib.rerank import RankableItem, cap_x_items, derank_items  # noqa: E402
from lib.setup_wizard import run_setup_wizard  # noqa: E402
from lib.summarize import summarize_items, synthesize_verdict  # noqa: E402
from lib.trending import TrendingItem, compute_internal_trending  # noqa: E402
from lib.youtube_yt import (  # noqa: E402
    Subscription,
    Upload,
    YouTubeAuthError,
    YouTubeFetchError,
    fetch_new_uploads,
    load_youtube_subscriptions,
    persist_subscriptions,
)
from lib.transcribe import TRANSCRIPT_LIMITS, Transcript, fetch_transcript_with_cues  # noqa: E402
from lib.chapterize import (  # noqa: E402
    ChapterSegmenter,
    LONG_FORM_THRESHOLD_SECONDS,
    _default_chapter_segmenter,
    chapterize_episode,
)
from lib.stage1_youtube import (  # noqa: E402
    _select_recent_uploads,
    drop_short_form_uploads,
)

# Weekly-cache window (brief §3 Stage 0): sources are re-loaded at most once per
# week. Daily runs must NOT re-hit yt-dlp — they ride the cache.
_SOURCES_REFRESH_INTERVAL_SECONDS: int = 7 * 24 * 60 * 60

# Manual-run network guard. When set truthy, Stage 0 skips the (network-touching)
# loader even on a cold/empty DB, logging why. Lets ``--depth quick`` exit 0 without
# touching the network on a machine with no cookies. The tested guarantee uses a
# mocked loader; this env var is the equivalent escape hatch for a bare CLI run.
_STAGE0_SKIP_NETWORK_ENV_VAR: str = "ORBIT_STAGE0_SKIP_NETWORK"

# Stage-1 first-run bounds. The delta engine returns EVERY unseen upload, so a cold DB
# would mark a channel's entire back-catalogue "new" (one channel returned ~2900) and
# classify all of it — thousands of ``claude -p`` calls. A daily digest only cares about
# what's genuinely recent, so we (a) keep only uploads from the last
# ``_STAGE1_RECENCY_WINDOW_DAYS`` days, (b) cap each channel to its newest
# ``_STAGE1_MAX_UPLOADS_PER_CHANNEL``, and (c) cap the whole run at
# ``_STAGE1_MAX_CLASSIFIED_UPLOADS``. Items left unclassified stay unseen and are
# reconsidered next run (the recency filter ages out the old back-catalogue naturally).
_STAGE1_RECENCY_WINDOW_DAYS: int = 2
_STAGE1_MAX_UPLOADS_PER_CHANNEL: int = 5
_STAGE1_MAX_CLASSIFIED_UPLOADS: int = 60
# The X half has the same unbounded-cost exposure: a rotated set of handles can return
# hundreds of new tweets on a cold/stale DB (one run pulled 765), and each tweet is a
# separate ``claude -p`` classify call. Cap the per-run X classify budget the same way as
# YouTube so a single digest run stays bounded; deferred tweets are reconsidered next run
# (X items are not delta-marked, so nothing is lost — see run_stage1_build_x_items).
_STAGE1_MAX_CLASSIFIED_TWEETS: int = 60

# The pipeline stages, in execution order. All stages are now wired: Stage 0
# (sources), Stages 1-2 (delta fetch + classify/chapterize, for BOTH YouTube and X),
# and Stages 3-4 (rank + render).
PIPELINE_STAGES: tuple[str, ...] = (
    "stage_0_load_sources",
    "stage_1_delta_fetch",
    "stage_2_classify",
    "stage_3_rank",
    "stage_4_render",
)

# Default HTML output path when ``config.delivery`` carries no ``html_path``. The
# tilde is expanded at write time. Page 2 is written beside it as today-page2.html.
DEFAULT_HTML_PATH: str = "~/orbit/out/today.html"


def build_argument_parser() -> argparse.ArgumentParser:
    """Construct the Orbit CLI argument parser.

    Returns:
        An ``argparse.ArgumentParser`` exposing ``--depth`` and ``--setup``.

    Example:
        >>> parser = build_argument_parser()
        >>> parsed = parser.parse_args(["--depth", "quick"])
        >>> parsed.depth
        'quick'
    """
    parser = argparse.ArgumentParser(
        prog="orbit",
        description="Orbit — load your subscriptions and surface a ranked daily digest.",
    )
    parser.add_argument(
        "--depth",
        choices=["quick", "default", "deep"],
        default="default",
        help="How much work the pipeline does per run (default: %(default)s).",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run first-time setup (reads subs/follows, auto-classifies, writes orbit.config.json, prints a cron entry).",
    )
    return parser


def run_setup() -> int:
    """Run the first-time setup wizard (brief §8.3), writing ``orbit.config.json``.

    Wiring only (Rule 5): delegates to :func:`lib.setup_wizard.run_setup_wizard` with the
    real defaults — the live subscription/following loaders, the live Claude classify
    boundary (:func:`lib.llm.make_llm_classifier`, which shells out to ``claude -p`` on the
    Claude Code subscription), builtin ``input``, and ``./orbit.config.json``. The wizard
    reads subs/follows, auto-classifies
    via the existing classify path, confirms categories, picks priority creators, sets
    delivery + schedule, writes the config, and prints the OS cron entry.

    Returns:
        Process exit code (0 on success), propagated from the wizard.
    """
    return run_setup_wizard(llm_classifier=make_llm_classifier())


def _parse_iso_timestamp(text: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 ``last_refreshed_at`` value into an aware UTC datetime.

    ``persist_subscriptions`` writes ``datetime.now(timezone.utc).isoformat()``, but
    older / hand-written rows may be naive or non-ISO. A naive value is assumed UTC;
    anything unparseable returns None (the caller treats None as stale → refresh), so
    a bad timestamp never silently suppresses a needed refresh.

    Args:
        text: The stored ``last_refreshed_at`` string, or None.

    Returns:
        An aware UTC ``datetime``, or None if absent/unparseable.
    """
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sources_need_refresh(rows: list[dict]) -> bool:
    """Decide whether Stage 0 must re-load subscriptions (weekly-cache rule).

    Refresh if there are NO youtube sources yet (first run has no baseline for the
    delta engine), OR if the most-recent ``last_refreshed_at`` across rows is older
    than :data:`_SOURCES_REFRESH_INTERVAL_SECONDS` (or unparseable → treated stale).
    Otherwise the cache is warm and the loader is skipped — daily runs must not
    re-hit yt-dlp.

    Args:
        rows: ``store.list_sources(platform="youtube")`` output.

    Returns:
        True if the loader should run, False on a cache hit.
    """
    if not rows:
        return True
    refresh_times = [_parse_iso_timestamp(row.get("last_refreshed_at")) for row in rows]
    parsed_times = [time for time in refresh_times if time is not None]
    if len(parsed_times) < len(rows):
        # Reason: a row with a missing/unparseable timestamp can't be proven fresh —
        # treat the whole set as stale and refresh rather than risk a stuck cache.
        return True
    most_recent = max(parsed_times)
    age_seconds = (datetime.now(timezone.utc) - most_recent).total_seconds()
    return age_seconds >= _SOURCES_REFRESH_INTERVAL_SECONDS


def run_stage0_load_sources(
    config: OrbitConfig,
    *,
    db_path: Optional[Path] = None,
    loader: Optional[Callable[[str], list[Subscription]]] = None,
    persist: Optional[Callable[[list[Subscription]], int]] = None,
    x_loader: Optional[Callable[[str], list[Follow]]] = None,
    x_persist: Optional[Callable[[list[Follow]], int]] = None,
) -> None:
    """Stage 0: load YouTube subscriptions AND the X following list (deterministic, no LLM).

    Initializes the DB, reads existing youtube sources, and applies the weekly-cache
    rule (:func:`_sources_need_refresh`): on a cache hit it logs ``sources_cache_hit``
    and returns WITHOUT calling the loader (daily runs ride the cache); on a needed
    refresh it calls the (injectable) loader with ``config.cookie_source``, persists,
    and logs ``sources_refreshed``. A :class:`YouTubeAuthError` is logged with an
    actionable ``fix_suggestion`` and RE-RAISED (fail loud, Rule 12 — never swallowed).

    The X half (Phase 4 / M2) loads the user's following list via the injectable
    ``x_loader`` / ``x_persist`` and is best-effort: an :class:`XAuthError` (e.g. missing
    ``X_USER_ID`` / cookies) is logged with an actionable ``fix_suggestion`` and SWALLOWED
    so a YouTube-only user still gets a digest — Orbit must not abort the whole run just
    because the optional X source is unconfigured. (A YouTube auth failure is fatal and
    re-raised; X is additive.) The X load is skipped entirely when ``x_loader`` is None
    AND no X loader is wired (the default uses :func:`lib.bird_x.load_x_following`, which
    fails loud-but-swallowed if cookies/``X_USER_ID`` are absent).

    All loader/persist callables default to the real source functions and are injectable
    so tests pass mocks without monkeypatching internals.

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``cookie_source``).
        db_path: Explicit DB path (tests pass a temp path); defaults to the per-user DB.
        loader: Subscription loader; defaults to ``load_youtube_subscriptions``.
        persist: Persist function; defaults to ``persist_subscriptions``.
        x_loader: X following loader; defaults to ``bird_x.load_x_following``.
        x_persist: X following persist; defaults to ``bird_x.persist_following``.

    Raises:
        YouTubeAuthError: If the YouTube loader fails to authenticate (re-raised — fatal).
    """
    active_loader = loader or load_youtube_subscriptions
    active_persist = persist or persist_subscriptions

    store.init_db(db_path)
    existing_rows = store.list_sources(platform="youtube")

    if not _sources_need_refresh(existing_rows):
        log.log_info(
            "sources_cache_hit",
            platform="youtube",
            count=len(existing_rows),
            detail="Sources refreshed within the last 7 days; skipping yt-dlp re-load.",
        )
        _load_x_sources(config, x_loader=x_loader, x_persist=x_persist)
        return

    # Manual-run escape hatch: skip the network-touching loader on a cold DB. Tests
    # use a mocked loader instead, so this only affects bare CLI runs.
    if loader is None and os.environ.get(_STAGE0_SKIP_NETWORK_ENV_VAR):
        log.log_warning(
            "sources_refresh_skipped_network_guard",
            platform="youtube",
            env_var=_STAGE0_SKIP_NETWORK_ENV_VAR,
            detail="Network guard set; skipping subscription load. Unset to fetch live.",
        )
        _load_x_sources(config, x_loader=x_loader, x_persist=x_persist)
        return

    log.log_info("sources_refresh_started", platform="youtube", reason="empty_or_stale")
    try:
        subscriptions = active_loader(config.cookie_source)
    except YouTubeAuthError:
        # Reason: fail loud (Rule 12). The loader already logged a redacted, actionable
        # error; we add Stage-0 context and re-raise so the run does not "succeed" with
        # an empty sources table the delta engine would silently watch as nothing-new.
        log.log_error(
            "stage0_sources_refresh_failed",
            fix_suggestion=(
                "YouTube authentication failed during Stage 0. Log into YouTube in the "
                "browser named by cookie_source (or set cookie_source='env' + .env), then "
                "re-run."
            ),
            platform="youtube",
        )
        raise

    persisted_count = active_persist(subscriptions)
    log.log_info("sources_refreshed", platform="youtube", count=persisted_count)
    _load_x_sources(config, x_loader=x_loader, x_persist=x_persist)


def _load_x_sources(
    config: OrbitConfig,
    *,
    x_loader: Optional[Callable[[str], list[Follow]]] = None,
    x_persist: Optional[Callable[[list[Follow]], int]] = None,
) -> None:
    """Load + persist the X following list (best-effort, additive to YouTube Stage 0).

    The X source is OPTIONAL: an :class:`XAuthError` (missing cookies / ``X_USER_ID``)
    is logged with an actionable ``fix_suggestion`` and swallowed so a YouTube-only user
    still gets a digest. The loader/persist are injectable (tests inject mocks; the
    default uses :mod:`lib.bird_x`).

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``cookie_source``).
        x_loader: X following loader; defaults to ``bird_x.load_x_following``.
        x_persist: X following persist; defaults to ``bird_x.persist_following``.
    """
    active_x_loader = x_loader or bird_x.load_x_following
    active_x_persist = x_persist or bird_x.persist_following

    log.log_info("x_sources_refresh_started", platform="x")
    try:
        follows = active_x_loader(config.cookie_source)
    except XAuthError as exc:
        # Reason: X is an additive source — do not abort the whole run when the user
        # has not configured X. Surface an actionable message and continue YouTube-only.
        log.log_warning(
            "stage0_x_sources_skipped",
            platform="x",
            fix_suggestion=(
                "X following not loaded (auth/config). Set AUTH_TOKEN/CT0 + X_USER_ID to "
                "include X in the digest; YouTube-only digest produced this run."
            ),
            error_message=str(exc),
        )
        return

    persisted_count = active_x_persist(follows)
    log.log_info("x_sources_refreshed", platform="x", count=persisted_count)


def run_stage1_build_x_items(
    config: OrbitConfig,
    depth: str,
    *,
    run_day_ordinal: Optional[int] = None,
    x_delta: Optional[Callable[..., list[Tweet]]] = None,
    llm_classifier: LlmClassifier = _default_llm_classifier,
) -> list[RankableItem]:
    """Stage 1 (X half): delta-fetch X tweets, classify them, build unified RankableItems.

    Loads the persisted X sources (``store.list_sources(platform="x")``), pulls each
    rotated handle's new tweets via the injectable ``x_delta`` (defaults to
    :func:`lib.bird_x.fetch_new_tweets`), classifies EACH tweet on the SAME two-axis
    :func:`lib.classify.classify_item` path as YouTube (the channel-level Axis-A prior
    comes from the source row's ``category``), and adapts every tweet into the shared
    :class:`RankableItem` via :meth:`RankableItem.from_tweet` (carrying its x.com
    ``card_url``). The result merges into the SAME unified stream YouTube uploads feed,
    so X tweets and videos rank + render together (the M2 unified digest).

    No-op (returns ``[]``) when there are no X sources. ``orbit.py`` stays wiring-only
    (Rule 5): rotation/delta/classify/build all live in lib/.

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``interests`` for Axis B).
        depth: ``quick`` | ``default`` | ``deep`` — selects the X handle budget.
        run_day_ordinal: The run's day ordinal driving handle rotation; defaults to a
            day count since the Unix epoch so rotation advances across daily runs.
        x_delta: X delta fetcher; defaults to ``bird_x.fetch_new_tweets``. Injectable so
            tests mock the subprocess boundary.
        llm_classifier: The injectable classify LLM boundary; tests inject a mock, the
            host session wires the real caller at runtime.

    Returns:
        The X tweets as classified :class:`RankableItem`s (possibly empty).
    """
    x_sources = store.list_sources(platform="x")
    if not x_sources:
        log.log_info("x_stage1_no_sources", platform="x")
        return []

    active_x_delta = x_delta or bird_x.fetch_new_tweets
    ordinal = run_day_ordinal if run_day_ordinal is not None else _current_day_ordinal()

    new_tweets = active_x_delta(x_sources, depth, ordinal)

    # The channel-level Axis-A prior is the source row's category, keyed by handle.
    category_by_handle = {str(row["external_id"]): str(row.get("category") or "signal") for row in x_sources}

    rankable_items: list[RankableItem] = []
    classified_count = 0
    dropped_noise_count = 0
    category_dropped_count = 0
    for tweet in new_tweets:
        if classified_count >= _STAGE1_MAX_CLASSIFIED_TWEETS:
            # Per-run classify budget reached; remaining tweets are left unclassified and
            # reconsidered next run. Logged so the cap is never a silent truncation (mirrors
            # the YouTube half's youtube_stage1_classify_cap_reached).
            log.log_warning(
                "x_stage1_classify_cap_reached",
                platform="x",
                fix_suggestion=(
                    "Per-run X classify cap hit; remaining tweets deferred to the next run. "
                    "Raise _STAGE1_MAX_CLASSIFIED_TWEETS if you want a larger single digest."
                ),
                classify_cap=_STAGE1_MAX_CLASSIFIED_TWEETS,
            )
            break

        channel_category = category_by_handle.get(tweet.handle, "signal")
        try:
            classification = classify.classify_item(
                tweet,
                channel_category=channel_category,
                interests=config.interests,
                llm_classifier=llm_classifier,
            )
        except LlmCliError as exc:
            # A transient ``claude -p`` hang/timeout must not abort the digest (Rule 12).
            # Skip just this tweet; the rest still process. (X items are not delta-marked
            # here, so it is naturally reconsidered on the next run.)
            log.log_warning(
                "x_stage1_item_classify_skipped",
                platform="x",
                handle=tweet.handle,
                fix_suggestion=(
                    "Classifying this tweet failed (claude -p timeout/transient); skipped "
                    "for this run. Other items still processed."
                ),
                error_message=str(exc),
            )
            continue
        # A real classify call completed — count it against the per-run budget. Tweets
        # dropped by the gates below still count (the ``claude -p`` cost was already paid).
        classified_count += 1
        # Alpha gate (X-only): drop generic/low-signal tweets outright rather than merely
        # ranking them down. Axis-A == 0 means the classifier judged the post noise (gm,
        # platitudes, engagement-bait). YouTube inclusion is deliberately left unchanged.
        if classification.axis_a_signal == 0:
            dropped_noise_count += 1
            continue
        # Category gate (2026-07-06 taxonomy ruling): drop items outside the fixed
        # taxonomy (category == "other") outright, same as the YouTube half — a shared
        # classify path, so a shared drop rule. A missing/garbled category defaults to a
        # keep sentinel in classify.py (never "other"), so a prompt regression can't
        # silently empty the digest (Rule 12).
        if classification.category == "other":
            category_dropped_count += 1
            continue
        rankable_items.append(RankableItem.from_tweet(tweet, classification, creator_external_id=tweet.handle))

    log.log_info(
        "x_stage1_build_completed",
        platform="x",
        source_count=len(x_sources),
        tweet_count=len(new_tweets),
        classified_count=classified_count,
        rankable_count=len(rankable_items),
        dropped_noise_count=dropped_noise_count,
        category_dropped_count=category_dropped_count,
    )
    return rankable_items


def run_stage1_build_youtube_items(
    config: OrbitConfig,
    depth: str,
    *,
    upload_delta: Optional[Callable[[dict, str], list[Upload]]] = None,
    transcript_fetcher: Optional[Callable[..., Optional[Transcript]]] = None,
    mark_seen: Optional[Callable[[int, str], None]] = None,
    llm_classifier: LlmClassifier = _default_llm_classifier,
    segmenter: ChapterSegmenter = _default_chapter_segmenter,
) -> list[RankableItem]:
    """Stage 1-2 (YouTube half): delta-fetch new uploads, classify + chapterize, build RankableItems.

    The YouTube-source analog of :func:`run_stage1_build_x_items`. For each persisted
    YouTube source (``store.list_sources(platform="youtube")``) it delta-fetches the new
    uploads via the injectable ``upload_delta`` (defaults to
    :func:`lib.youtube_yt.fetch_new_uploads`, which already filters out ``seen`` ids),
    classifies EACH upload on the SAME two-axis :func:`lib.classify.classify_item` path as
    X (the channel-level Axis-A prior is the source row's ``category``), chapterizes
    long-form uploads via :func:`lib.chapterize.chapterize_episode`, and adapts each into
    the shared :class:`RankableItem` via :meth:`RankableItem.from_parts`. The result merges
    into the SAME unified stream X tweets feed, so videos and tweets rank + render together.

    Chapterization is budgeted (Rule 5 — deterministic gating, no LLM in the decision):
    a long-form upload (``duration > LONG_FORM_THRESHOLD_SECONDS``) with creator-supplied
    chapters is mapped deterministically (no transcript, no model); a long-form upload
    WITHOUT them needs a transcript to segment, and transcript fetches are capped per run
    at :data:`lib.transcribe.TRANSCRIPT_LIMITS` ``[depth]`` (``quick`` 0, ``default`` 2,
    ``deep`` 8) so a daily run never transcribes the whole feed. Over-budget long-form
    uploads simply render without chapters.

    Per-channel fetch failures are best-effort: a :class:`lib.youtube_yt.YouTubeFetchError`
    (a single channel's timeout / transient listing failure) is logged with an actionable
    ``fix_suggestion`` and SKIPPED so one bad channel never loses the whole YouTube half.
    A seen-mark is written AFTER each upload is successfully built (delta-engine contract:
    marking is the driver's post-success job, never pre-marked).

    No-op (returns ``[]``) when there are no YouTube sources. ``orbit.py`` stays
    wiring-only (Rule 5): delta/classify/chapterize/build all live in lib/.

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``interests`` for Axis B).
        depth: ``quick`` | ``default`` | ``deep`` — selects the per-run transcript budget.
        upload_delta: Per-channel new-upload fetcher; defaults to
            :func:`lib.youtube_yt.fetch_new_uploads`. Injectable so tests mock the
            subprocess boundary.
        transcript_fetcher: Transcript fetcher; defaults to
            :func:`lib.transcribe.fetch_transcript_with_cues`. Injectable for tests.
        mark_seen: Seen-marker; defaults to :func:`store.mark_seen`. Injectable for tests.
        llm_classifier: The injectable classify LLM boundary; tests inject a mock, the
            host session wires the real caller at runtime.
        segmenter: The injectable chapterize LLM boundary (same shape); tests inject a
            mock, the host session wires the real caller at runtime.

    Returns:
        The new YouTube uploads as classified + chapterized :class:`RankableItem`s
        (possibly empty).
    """
    youtube_sources = store.list_sources(platform="youtube")
    if not youtube_sources:
        log.log_info("youtube_stage1_no_sources", platform="youtube")
        return []

    active_upload_delta = upload_delta or fetch_new_uploads
    active_transcript_fetcher = transcript_fetcher or fetch_transcript_with_cues
    active_mark_seen = mark_seen or store.mark_seen

    transcript_limit = TRANSCRIPT_LIMITS.get(depth, TRANSCRIPT_LIMITS["default"])
    transcripts_fetched = 0
    total_new_uploads = 0
    classified_count = 0
    short_form_dropped_count = 0
    category_dropped_count = 0

    # Recency cutoff (YYYYMMDD) — only uploads on/after this date are eligible. Bounds a
    # cold-DB first run to genuinely-recent items instead of whole back-catalogues.
    recency_cutoff = (datetime.now(timezone.utc).date() - timedelta(days=_STAGE1_RECENCY_WINDOW_DAYS)).strftime(
        "%Y%m%d"
    )

    rankable_items: list[RankableItem] = []
    for source in youtube_sources:
        if classified_count >= _STAGE1_MAX_CLASSIFIED_UPLOADS:
            # Per-run classify budget reached; remaining channels' uploads stay unseen and
            # are reconsidered next run. Logged so the cap is never a silent truncation.
            log.log_warning(
                "youtube_stage1_classify_cap_reached",
                platform="youtube",
                fix_suggestion=(
                    "Per-run classify cap hit; remaining channels deferred to the next run. "
                    "Raise _STAGE1_MAX_CLASSIFIED_UPLOADS if you want a larger single digest."
                ),
                classify_cap=_STAGE1_MAX_CLASSIFIED_UPLOADS,
            )
            break

        source_id = source["source_id"]
        channel_id = str(source["external_id"])
        channel_category = str(source.get("category") or "signal")

        try:
            new_uploads = active_upload_delta(source, depth)
        except YouTubeFetchError as exc:
            # Reason: one channel's listing failure must not lose the whole YouTube half.
            # Log loud-and-actionable (Rule 12) and skip just this channel — the rest of
            # the feed (and the X half) still produces a digest.
            log.log_warning(
                "youtube_stage1_channel_skipped",
                platform="youtube",
                source_id=source_id,
                channel_id=channel_id,
                fix_suggestion=(
                    "Listing uploads for this channel failed (timeout / transient); skipped "
                    "it for this run. Other channels still processed. See the error above."
                ),
                error_message=str(exc),
            )
            continue

        total_new_uploads += len(new_uploads)
        # Keep only recent uploads, newest first, capped per channel and by the remaining
        # global classify budget — so one channel's back-catalogue can't dominate the run.
        recent_uploads = _select_recent_uploads(
            new_uploads,
            recency_cutoff=recency_cutoff,
            per_channel_cap=_STAGE1_MAX_UPLOADS_PER_CHANNEL,
        )
        # Long-form floor (2026-07-06 ruling): drop known-short clips BEFORE the classify
        # call so the LLM budget is only ever spent on long-form candidates. A missing
        # duration is kept (fail-open) — see drop_short_form_uploads.
        long_form_uploads, short_form_dropped = drop_short_form_uploads(recent_uploads)
        short_form_dropped_count += short_form_dropped
        remaining_budget = _STAGE1_MAX_CLASSIFIED_UPLOADS - classified_count
        for upload in long_form_uploads[:remaining_budget]:
            classified_count += 1
            try:
                classification = classify.classify_item(
                    upload,
                    channel_category=channel_category,
                    interests=config.interests,
                    llm_classifier=llm_classifier,
                )
            except LlmCliError as exc:
                # A transient ``claude -p`` hang/timeout must not abort the whole digest
                # (Rule 12). Skip just this upload; it stays unseen (mark_seen not reached)
                # and is reconsidered next run. Every other item still processes.
                log.log_warning(
                    "youtube_stage1_item_classify_skipped",
                    platform="youtube",
                    source_id=source_id,
                    item_external_id=upload.video_id,
                    fix_suggestion=(
                        "Classifying this upload failed (claude -p timeout/transient); skipped "
                        "for this run, left unseen for the next. Other items still processed."
                    ),
                    error_message=str(exc),
                )
                continue

            # Category gate (2026-07-06 taxonomy ruling): an item the classifier placed
            # outside the user's world (category == "other") is dropped outright, mirroring
            # the X alpha gate. A missing/garbled category defaults to a keep sentinel in
            # classify.py (never "other"), so a prompt regression can't silently empty the
            # digest (Rule 12). This upload is left UNSEEN (mark_seen not reached) so a later
            # prompt fix reconsiders it.
            if classification.category == "other":
                category_dropped_count += 1
                continue

            # Only a long-form upload WITHOUT creator chapters needs a transcript (the LLM
            # segmentation path); short items and creator-chaptered items need none. Fetch
            # one only while under the per-run budget — chapterize_episode then decides the
            # rest (short -> [], creator chapters -> deterministic, else snap to cues).
            transcript: Optional[Transcript] = None
            needs_transcript = (
                upload.duration is not None and upload.duration > LONG_FORM_THRESHOLD_SECONDS and not upload.chapters
            )
            if needs_transcript and transcripts_fetched < transcript_limit:
                transcript = active_transcript_fetcher(upload.video_id, depth)
                transcripts_fetched += 1
            try:
                chapters = chapterize_episode(upload, transcript, segmenter=segmenter)
            except LlmCliError as exc:
                # Chapter segmentation is best-effort: a transient ``claude -p`` failure
                # degrades to no chapters (still a valid tile), never aborts the run (Rule 12).
                log.log_warning(
                    "youtube_stage1_item_chapterize_degraded",
                    platform="youtube",
                    source_id=source_id,
                    item_external_id=upload.video_id,
                    fix_suggestion=(
                        "Chapterizing this upload failed (claude -p timeout/transient); "
                        "rendering it without chapters. Re-run later for chapter deep-links."
                    ),
                    error_message=str(exc),
                )
                chapters = []

            rankable_items.append(
                RankableItem.from_parts(upload, classification, chapters, creator_external_id=channel_id)
            )
            # Mark seen AFTER a successful build so a mid-run crash never silently drops an
            # item by pre-marking it (delta-engine contract, youtube_yt.fetch_new_uploads).
            active_mark_seen(source_id, upload.video_id)

    log.log_info(
        "youtube_stage1_build_completed",
        platform="youtube",
        source_count=len(youtube_sources),
        upload_count=total_new_uploads,
        classified_count=classified_count,
        transcripts_fetched=transcripts_fetched,
        rankable_count=len(rankable_items),
        short_form_dropped_count=short_form_dropped_count,
        category_dropped_count=category_dropped_count,
    )
    return rankable_items


def _current_day_ordinal() -> int:
    """Return today's day count since the Unix epoch (UTC) for X handle rotation.

    A monotonically increasing per-day integer so :func:`lib.bird_x.fetch_new_tweets`'s
    round-robin rotation advances every day, widening handle coverage across runs.

    Returns:
        The number of whole days since 1970-01-01 (UTC).
    """
    return (datetime.now(timezone.utc).date() - date(1970, 1, 1)).days


def run_stage5_overlap_trending_scoops(
    items: list[RankableItem],
    config: OrbitConfig,
    *,
    store_module: Any = store,
    search_fn: Optional[Callable[[str], list[Any]]] = None,
) -> tuple[list[Cluster], list[TrendingItem], list[TrendingItem], dict[str, float]]:
    """Stage 5 (M3): cluster overlaps -> internal trending -> external tag -> scoops.

    The M3 seam, run BETWEEN classify (Stage 2) and rank (Stage 6). Pure wiring (Rule 5:
    all logic lives in lib/): it sequences the four deterministic lib functions and
    returns their outputs for Stage 6 (the trending/scoop multiplier map) and Stage 7
    (the three render sections):

      1. :func:`lib.cluster.cluster_overlaps` — short-merge / long-cross-link clusters.
      2. :func:`lib.trending.compute_internal_trending` — baseline-relative velocity
         (reads the injected ``store_module`` only for each creator's ``seen``-history
         DEPTH — the dormancy signal — never engagement).
      3. :func:`lib.external_trending.tag_external_corroboration` — bounded keyless
         cross-search tagging corroborated-vs-scoop, throttled by ``config.depth``. When
         ``search_fn`` is None the lib default keyless search is used; tests inject a
         fake so no live web call is made.
      4. :func:`lib.external_trending.detect_scoops` — dormant-account acceleration, the
         loud scoops strip — plus
         :func:`lib.external_trending.build_trending_multiplier_map` for the rerank boost.

    Empty ``items`` returns ``([], [], [], {})`` — the M1/M2 quiet path produces no M3
    sections and a neutral multiplier map, so rank+render are byte-for-byte unchanged.

    Args:
        items: The unified classified :class:`RankableItem` stream.
        config: The loaded :class:`OrbitConfig` (``creator_weights`` for representatives,
            ``depth`` for the cross-search budget).
        store_module: The store module/object for the history-depth lookup (injectable;
            defaults to :mod:`store`).
        search_fn: OPTIONAL keyless cross-search ``(query) -> list``; None uses the lib
            default. Tests inject a fake so no live web call fires.

    Returns:
        ``(clusters, trending_items, scoops, trending_multipliers)``.
    """
    if not items:
        log.log_info("overlap_trending_scoops_completed", cluster_count=0, trending_count=0, scoop_count=0)
        return [], [], [], {}

    clusters = cluster_overlaps(items, config)
    items_by_id = {str(item.item_external_id): item for item in items if item.item_external_id}
    trending_items = compute_internal_trending(clusters, items_by_id, store_module)

    # Reason: external corroboration is bounded by the user's depth throttle (CSO). The
    # search_fn defaults to the lib keyless search; tests inject a fake to stay offline.
    if search_fn is not None:
        tag_external_corroboration(trending_items, search_fn=search_fn, depth=config.depth)
    else:
        tag_external_corroboration(trending_items, depth=config.depth)

    scoops = detect_scoops(trending_items)
    trending_multipliers = build_trending_multiplier_map(trending_items)

    log.log_info(
        "overlap_trending_scoops_completed",
        cluster_count=len(clusters),
        trending_count=len(trending_items),
        scoop_count=len(scoops),
        multiplier_count=len(trending_multipliers),
    )
    return clusters, trending_items, scoops, trending_multipliers


def run_stage6_rank_and_tier(
    items: list[RankableItem],
    config: OrbitConfig,
    *,
    trending_multipliers: Optional[dict[str, float]] = None,
) -> list[TieredItem]:
    """Stage 6: score the rankable items, then sort them into density tiers.

    Pure delegation to ``lib.rerank.derank_items`` (weighted score, descending), the X-half
    top-N cap (``lib.rerank.cap_x_items`` — Phase 8 Sub-phase 3: at most
    :data:`lib.rerank.X_DIGEST_TWEET_CAP` tweets survive, YouTube never capped, run BEFORE
    tiering so tiering keeps its ``len(out) == len(items)`` invariant for what it receives),
    then ``lib.density.assign_density_tiers``. No LLM; orbit.py stays wiring-only (Rule 5).

    Args:
        items: The :class:`RankableItem`s from the (upstream) classify/chapterize half.
        config: The loaded :class:`OrbitConfig` (supplies ``creator_weights``).
        trending_multipliers: OPTIONAL Stage-5 (M3) multiplier map; None leaves it neutral.

    Returns:
        The tiered, rank-ordered items ready for the renderer (YouTube in full, X capped).
    """
    scored_items = derank_items(items, config, trending_multipliers=trending_multipliers)
    capped_items, x_cap_dropped_count = cap_x_items(scored_items)
    tiered_items = assign_density_tiers(capped_items)
    log.log_info("rank_and_tier_completed", item_count=len(tiered_items), x_cap_dropped_count=x_cap_dropped_count)
    return tiered_items


def _default_html_writer(path: Path, html: str) -> None:
    """Write ``html`` to ``path`` (UTF-8), creating parent directories as needed.

    The default Stage-7 writer. Kept tiny and injectable so tests pass their own
    writer / a temp path and never touch the real per-user output location.

    Args:
        path: The (already tilde-expanded) absolute file path to write.
        html: The HTML string to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _resolve_html_path(config: OrbitConfig) -> Path:
    """Resolve the page-1 HTML output path from ``config.delivery`` (tilde-expanded).

    Reads ``config.delivery["html_path"]`` (default :data:`DEFAULT_HTML_PATH`), expands
    a leading ``~``, and returns an absolute Path. Page 2 is written beside it.

    Args:
        config: The loaded :class:`OrbitConfig`.

    Returns:
        The absolute, tilde-expanded page-1 output path.
    """
    raw_path = str(config.delivery.get("html_path") or DEFAULT_HTML_PATH)
    return Path(raw_path).expanduser()


def _write_digest_markdown(
    markdown_path: Path,
    tiered_items: list[TieredItem],
    config: OrbitConfig,
    *,
    writer: Callable[[Path, str], None],
    clusters: Optional[list[Cluster]],
    trending_items: Optional[list[TrendingItem]],
    scoops: Optional[list[TrendingItem]],
    verdict: str,
    summaries: Optional[dict[str, str]],
) -> None:
    """Write the self-contained ``digest.md`` twin beside page 1 (issue #6), fail-soft.

    The markdown twin (:func:`lib.markdown_render.render_digest_markdown`) is a SECONDARY
    output that gates #7/#9; the primary product is the HTML digest + email. A render/write
    failure here is logged loudly (Rule 12) but SWALLOWED so it can never abort the HTML
    render contract or block delivery — matching Stage 7's loud-but-non-fatal delivery posture.
    The path is passed in (beside page 1), and the twin is deliberately kept OUT of
    ``written_paths`` so it never rides the email as a ``text/html`` attachment.

    Args:
        markdown_path: The ``digest.md`` path (beside page 1 — the contract path #7 reads).
        tiered_items: The Stage-6 tiered items.
        config: The loaded config (supplies ``digest_title``).
        writer: The same ``(path, text) -> None`` writer the HTML pages use (tests inject a temp writer).
        clusters: OPTIONAL clusters (feed cross-links + masthead count), forwarded verbatim.
        trending_items: OPTIONAL trending list (the trio), forwarded verbatim.
        scoops: OPTIONAL detected scoops (the trio + dormant count), forwarded verbatim.
        verdict: The pre-computed LLM verdict sentence, forwarded verbatim.
        summaries: The pre-computed blurb map, forwarded verbatim.
    """
    try:
        markdown = markdown_render.render_digest_markdown(
            tiered_items,
            config,
            clusters=clusters,
            trending_items=trending_items,
            scoops=scoops,
            verdict=verdict,
            summaries=summaries or {},
        )
        writer(markdown_path, markdown)
        log.log_info("markdown_digest_written", markdown_path=str(markdown_path), char_count=len(markdown))
    except Exception as exc:
        log.log_error(
            "markdown_digest_write_failed",
            fix_suggestion=(
                "The digest.md twin (#7's input) could not be written; the HTML digest and email "
                "were unaffected. Check the output directory is writable."
            ),
            markdown_path=str(markdown_path),
            error_message=str(exc),
        )


def run_stage7_render(
    tiered_items: list[TieredItem],
    config: OrbitConfig,
    *,
    html_path: Optional[Path] = None,
    writer: Callable[[Path, str], None] = _default_html_writer,
    clusters: Optional[list[Cluster]] = None,
    trending_items: Optional[list[TrendingItem]] = None,
    scoops: Optional[list[TrendingItem]] = None,
    verdict: str = "",
    summaries: Optional[dict[str, str]] = None,
    inline_image: Optional[Callable[[str], Optional[str]]] = None,
) -> list[Path]:
    """Stage 7: render the tiered items to HTML and write page 1 (and page 2 if spilled).

    Delegates rendering to ``lib.render.render_digest_pages`` (which decides the 1-vs-2
    page split via the height budget, hard-capped at 2 pages), then writes page 1 to
    ``html_path`` (default from ``config.delivery``, tilde-expanded, parents created)
    and, when the digest spilled, page 2 to ``render.DEFAULT_PAGE_2_FILENAME`` in the
    SAME directory. The ``writer`` is injectable so tests write to a temp dir without
    touching the real per-user path. Logs ``render_completed`` with the page count.

    Args:
        tiered_items: The Stage-6 output (tiered, rank-ordered).
        config: The loaded :class:`OrbitConfig` (supplies ``delivery.html_path``).
        html_path: Explicit page-1 path (tests pass a temp path); defaults to the
            resolved ``config.delivery.html_path``.
        writer: ``(path, html) -> None`` writer; defaults to :func:`_default_html_writer`.

    Returns:
        The list of paths actually written (``[page1]`` or ``[page1, page2]``).
    """
    page_1_path = html_path if html_path is not None else _resolve_html_path(config)
    page_2_path = page_1_path.parent / render.DEFAULT_PAGE_2_FILENAME

    # The inline_image seam defaults to render's real build-time fetch; tests pass a stub
    # so the render path never touches the network.
    render_kwargs: dict[str, Any] = {}
    if inline_image is not None:
        render_kwargs["inline_image"] = inline_image

    pages = render.render_digest_pages(
        tiered_items,
        config,
        page_2_href=render.DEFAULT_PAGE_2_FILENAME,
        clusters=clusters,
        trending_items=trending_items,
        scoops=scoops,
        verdict=verdict,
        summaries=summaries,
        **render_kwargs,
    )

    written_paths: list[Path] = [page_1_path]
    writer(page_1_path, pages[0])
    if len(pages) > 1:
        writer(page_2_path, pages[1])
        written_paths.append(page_2_path)

    # Write the self-contained digest.md twin beside page 1 (issue #6 — the input #7 hands
    # to a fresh Claude session). It is a SIDE output: deliberately NOT appended to
    # written_paths, because run_stage7_deliver attaches every written path to the email as
    # a text/html attachment. Loud-but-non-fatal — a markdown failure never aborts delivery.
    _write_digest_markdown(
        markdown_render.resolve_digest_md_path(page_1_path),
        tiered_items,
        config,
        writer=writer,
        clusters=clusters,
        trending_items=trending_items,
        scoops=scoops,
        verdict=verdict,
        summaries=summaries,
    )

    log.log_info(
        "render_completed",
        stage="stage_7_render",
        item_count=len(tiered_items),
        page_count=len(pages),
        spilled=len(pages) > 1,
        html_path=str(page_1_path),
    )
    return written_paths


def _build_delivery_summary(tiered_items: list[TieredItem], scoops: list[TrendingItem]) -> str:
    """Build the one-line delivery TL;DR from the tiered items + scoops (deterministic).

    A PURE, deterministic helper (Rule 5 — no LLM in the delivery path): it counts the
    items and names the top scoop / top item so the delivery body is a useful
    one-liner without any model call. Leads with the loudest signal (a scoop) when one
    exists (brief §3 Stage 7: "TL;DR + scoops + a link"), else falls back to the
    top-ranked item's title, else a quiet "no new items" line.

    Args:
        tiered_items: The Stage-6 output (rank-ordered; index 0 is the top item).
        scoops: The Stage-5 scoops (the highest-value signal), possibly empty.

    Returns:
        A one-line TL;DR string suitable for the message body.
    """
    item_count = len(tiered_items)
    if item_count == 0:
        return "Orbit: no new items in your feed today."

    noun = "item" if item_count == 1 else "items"
    lead = f"Orbit: {item_count} new {noun}"

    if scoops:
        scoop_count = len(scoops)
        scoop_label = "scoop" if scoop_count == 1 else "scoops"
        top_scoop_title = (scoops[0].title or "").strip()
        if top_scoop_title:
            return f"{lead}, {scoop_count} {scoop_label} — top: {top_scoop_title}"
        return f"{lead}, {scoop_count} {scoop_label}"

    top_title = (tiered_items[0].scored_item.item.title or "").strip()
    if top_title:
        return f"{lead} — top: {top_title}"
    return lead


def run_stage7_deliver(
    tiered_items: list[TieredItem],
    scoops: list[TrendingItem],
    written_paths: list[Path],
    config: OrbitConfig,
    *,
    transport: Optional[deliver.SmtpTransport] = None,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    """Deliver the rendered digest — email send (PRD M5) plus the optional Briefcast file.

    Runs AFTER :func:`run_stage7_render`, which returns ``written_paths`` (page 1 first,
    page 2 when the digest spilled). iMessage and WhatsApp delivery were deleted (PRD story
    #8); email is the single network delivery path.

    Two outputs, both no-ops until configured:

      * Email (:func:`lib.deliver.deliver_email`) — sends the deterministic one-line TL;DR
        (:func:`_build_delivery_summary` — no LLM, Rule 5) as the body with every
        ``written_paths`` page attached. It SKIPS cleanly when ``delivery.email_to`` or the
        ``.env`` credentials are unset, and a send failure is loud but non-fatal — so this
        stage never crashes the pipeline or changes ``seen`` state.
      * Briefcast (stretch, integrations §6) — a payload FILE gated behind
        ``delivery.briefcast_path`` and skipped by default. No auth surface.

    Business logic lives in :mod:`lib.deliver`; this stays sequencing only. The SMTP
    ``transport`` and ``env`` are injectable so tests fake the boundary (default: real
    ``smtplib.SMTP_SSL`` + ``os.environ``, resolved inside :func:`lib.deliver.deliver_email`).

    Args:
        tiered_items: The Stage-6 tiered items (for the TL;DR + Briefcast episode list).
        scoops: The Stage-5 scoops (the TL;DR leads with the loudest one).
        written_paths: The rendered page paths from Stage 7 (page 1 first); attached to the email.
        config: The loaded :class:`OrbitConfig` (supplies the ``delivery`` targets).
        transport: Optional injected SMTP factory; forwarded to ``deliver_email`` when set.
        env: Optional injected environment mapping; forwarded to ``deliver_email`` when set.
    """
    summary = _build_delivery_summary(tiered_items, scoops)

    # Briefcast (stretch) — a payload file, gated behind its config key, skipped by default.
    briefcast_path = config.delivery.get("briefcast_path")
    if briefcast_path:
        deliver.emit_briefcast_payload(summary, list(tiered_items), briefcast_path)

    # Email — the single delivery path (PRD M5). deliver_email owns the skip/retry/no-leak
    # posture, so it is safe to call unconditionally: an unset recipient/credential skips.
    # Only forward the injected boundaries when a caller supplied them (mirrors the
    # inline_image seam in run_stage7_render) so the real defaults stay in one place.
    email_kwargs: dict[str, Any] = {}
    if transport is not None:
        email_kwargs["transport"] = transport
    if env is not None:
        email_kwargs["env"] = env
    deliver.deliver_email(summary, written_paths, config.delivery.get("email_to"), **email_kwargs)


def run_pipeline(depth: str) -> int:
    """Run the Orbit pipeline under the single-run lock, skipping if another run holds it.

    Both the launchd 7am agent (which fires a missed run on wake) and a manual ``/orbit``
    reach here. The run-lock (:func:`lib.runlock.acquire_run_lock`) makes overlapping runs
    exclusive: if a previous run is still in flight, this one exits early (0) with a clear
    ``pipeline_skipped_already_running`` log rather than racing on the shared SQLite state.
    A crashed run releases the lock automatically (kernel-managed ``flock``), so the next
    scheduled run is never blocked by a dead one.

    Args:
        depth: One of ``quick``, ``default``, ``deep`` — selects how much work each stage
            performs.

    Returns:
        Process exit code (0 on success or an intentional already-running skip, non-zero on
        a Stage-0 auth failure).
    """
    try:
        with runlock.acquire_run_lock():
            return _run_pipeline_stages(depth)
    except runlock.RunLockHeld as exc:
        log.log_warning(
            "pipeline_skipped_already_running",
            fix_suggestion=(
                "Another Orbit run is still in flight; this run exited without touching the "
                "shared SQLite state. It will run again at the next scheduled time."
            ),
            depth=depth,
            error_message=str(exc),
        )
        return 0


def _run_pipeline_stages(depth: str) -> int:
    """Run the Orbit pipeline stages (Stage 0 real; the run-lock is held by the caller).

    Args:
        depth: One of ``quick``, ``default``, ``deep`` — selects how much work
            each stage performs in later phases.

    Returns:
        Process exit code (0 on success, non-zero on a Stage-0 auth failure).
    """
    log.log_info("pipeline_started", depth=depth)

    config = load_config()
    try:
        run_stage0_load_sources(config)
    except YouTubeAuthError as exc:
        # Surface the actionable message and exit non-zero — a real missing-cookies run
        # must fail loud, not pretend the pipeline ran.
        log.log_error(
            "pipeline_aborted_stage0",
            fix_suggestion="Resolve the YouTube auth failure above, then re-run.",
            depth=depth,
            error_message=str(exc),
        )
        return 1

    # The live Claude boundary for classification (runs ``claude -p`` on the user's Claude
    # Code subscription — no ANTHROPIC_API_KEY). Built once and threaded into the producers below.
    llm_classifier = make_llm_classifier()

    # Stage 1-2 (YouTube half) — delta-fetch new uploads, classify them on the live LLM
    # boundary, chapterize long-form episodes (creator chapters deterministically, else a
    # budgeted transcript + LLM segmentation), build RankableItems. The same llm boundary
    # serves both classify and chapterize. No-op (empty) when the user has no YouTube
    # sources configured.
    youtube_items = run_stage1_build_youtube_items(
        config, depth, llm_classifier=llm_classifier, segmenter=llm_classifier
    )

    # Stage 1 (X half) — delta-fetch X tweets, classify them on the live LLM boundary,
    # build RankableItems. No-op (empty) when the user has no X sources configured
    # (YouTube-only setups).
    x_items = run_stage1_build_x_items(config, depth, llm_classifier=llm_classifier)

    # Stage 6->7 (rank + render). The unified stream merges YouTube uploads with X tweet
    # RankableItems; both flow through the SAME rank/tier/render (the M2 unified-digest seam).
    rankable_items = youtube_items + x_items

    # Stage 5 (M3) — cluster overlaps, compute baseline-relative trending, tag external
    # corroboration vs scoop, detect dormant-account scoops. Feeds the trending/scoop
    # multiplier into rank and the three M3 sections into render. On the bare CLI run
    # rankable_items is empty, so this yields no clusters/trending/scoops and a neutral
    # multiplier map (the M1/M2 path is unchanged).
    clusters, trending_items, scoops, trending_multipliers = run_stage5_overlap_trending_scoops(rankable_items, config)
    tiered_items = run_stage6_rank_and_tier(rankable_items, config, trending_multipliers=trending_multipliers)

    # LLM editorial prose (Rule 5 — summarizing the day's feed is a valid model use).
    # Both go through the live claude-CLI boundary and are FAIL-SOFT (a flaky/absent LLM
    # returns ""/{} so the digest degrades to structural-only, never crashes the pipeline,
    # Rule 12). Only Hero/Standard items get per-item blurbs (cost control); the verdict is
    # grounded in the scoop + cluster + top-headline context.
    verdict = synthesize_verdict(tiered_items, scoops, clusters)
    hero_standard_items = [
        tiered_item.scored_item.item
        for tiered_item in tiered_items
        if tiered_item.density_tier in (TIER_HERO, TIER_STANDARD)
    ]
    summaries = summarize_items(hero_standard_items)

    written_paths = run_stage7_render(
        tiered_items,
        config,
        clusters=clusters,
        trending_items=trending_items,
        scoops=scoops,
        verdict=verdict,
        summaries=summaries,
    )

    # Stage 7 (deliver) — email the rendered pages (PRD M5). deliver_email skips cleanly
    # when the recipient/credentials are unconfigured and is loud-but-non-fatal on failure,
    # so the run still exits 0 and seen state (written in Stage 1) is never disturbed.
    run_stage7_deliver(tiered_items, scoops, written_paths, config)

    log.log_info(
        "pipeline_completed",
        depth=depth,
        status="rank_render_half",
        item_count=len(tiered_items),
        pages_written=len(written_paths),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to setup or the pipeline.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). Injectable
            for tests.

    Returns:
        Process exit code.
    """
    parser = build_argument_parser()
    parsed_args = parser.parse_args(argv)

    # Seed the local .env into os.environ ONCE, before any stage runs. The X loader
    # (lib.bird_x) reads AUTH_TOKEN / CT0 / X_USER_ID straight from os.environ, so this must
    # happen up front — not as a side effect of a later classify call. No-op if .env absent.
    load_dotenv()

    if parsed_args.setup:
        return run_setup()
    return run_pipeline(depth=parsed_args.depth)


if __name__ == "__main__":
    raise SystemExit(main())
