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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable when this script is run directly (e.g.
# ``python3 scripts/orbit.py``). Mirrors the last30days reference so
# lifted modules import ``from lib import log, subproc`` unchanged in later phases.
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import store  # noqa: E402  (import must follow the sys.path insert above)
from lib import bird_x, classify, deliver, log, render, summarize  # noqa: E402
from lib.bird_x import Follow, Tweet, XAuthError  # noqa: E402
from lib.classify import LlmClassifier, _default_llm_classifier  # noqa: E402
from lib.cluster import Cluster, cluster_overlaps  # noqa: E402
from lib.config import OrbitConfig, load_config  # noqa: E402
from lib.density import TieredItem, assign_density_tiers  # noqa: E402
from lib.llm import load_dotenv, make_llm_classifier  # noqa: E402
from lib.external_trending import (  # noqa: E402
    build_trending_multiplier_map,
    detect_scoops,
    tag_external_corroboration,
)
from lib.rerank import (  # noqa: E402
    RankableItem,
    compute_creator_engagement_baselines,
    derank_items,
    winner_score,
)
from lib.setup_wizard import run_setup_wizard  # noqa: E402
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
from lib.transcribe import (  # noqa: E402
    SUMMARY_CAP_BY_DEPTH,
    SUMMARY_FLOOR,
    X_SUMMARY_CAP_BY_DEPTH,
    Transcript,
    fetch_transcript_with_cues,
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

# The pipeline stages, in execution order. All stages are now wired: Stage 0
# (sources), Stages 1-2 (delta fetch + classify/chapterize, for BOTH YouTube and X),
# and Stages 3-4 (rank + render).
PIPELINE_STAGES: tuple[str, ...] = (
    "stage_0_load_sources",
    "stage_1_ingest_metadata",
    "stage_2_classify",
    "stage_3_cluster",
    "stage_4_crown_winners",
    "stage_5_summarize_winners",
    "stage_6_trending_scoops",
    "stage_7_rank",
    "stage_8_render",
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
    for tweet in new_tweets:
        channel_category = category_by_handle.get(tweet.handle, "signal")
        classification = classify.classify_item(
            tweet,
            channel_category=channel_category,
            interests=config.interests,
            llm_classifier=llm_classifier,
        )
        rankable_items.append(
            RankableItem.from_tweet(tweet, classification, creator_external_id=tweet.handle)
        )

    log.log_info(
        "x_stage1_build_completed",
        platform="x",
        source_count=len(x_sources),
        tweet_count=len(new_tweets),
        rankable_count=len(rankable_items),
    )
    return rankable_items


def _select_recent_uploads(
    uploads: list[Upload],
    *,
    recency_cutoff: str,
    per_channel_cap: int,
) -> list[Upload]:
    """Keep a channel's recent uploads, newest first, capped to ``per_channel_cap``.

    Bounds the cold-DB first run (where the delta engine marks an entire back-catalogue
    "new"): only uploads with an ``upload_date`` (``YYYYMMDD``) on/after ``recency_cutoff``
    survive, sorted newest-first, then truncated to the cap. Uploads with no date are
    dropped — a daily digest can't place them in time, and including them would reopen the
    back-catalogue blowup. ``YYYYMMDD`` strings compare lexically == chronologically.

    Args:
        uploads: The channel's new (unseen) uploads from the delta engine.
        recency_cutoff: Inclusive lower bound as a ``YYYYMMDD`` string.
        per_channel_cap: Max uploads to return for this channel.

    Returns:
        The newest in-window uploads, at most ``per_channel_cap`` of them.

    Example:
        >>> _select_recent_uploads(uploads, recency_cutoff="20260620", per_channel_cap=5)
        [<newest>, ...]
    """
    recent = [upload for upload in uploads if upload.upload_date and upload.upload_date >= recency_cutoff]
    recent.sort(key=lambda upload: upload.upload_date, reverse=True)
    return recent[:per_channel_cap]


def run_stage1_build_youtube_items(
    config: OrbitConfig,
    depth: str,
    *,
    upload_delta: Optional[Callable[[dict, str], list[Upload]]] = None,
    mark_seen: Optional[Callable[[int, str], None]] = None,
    llm_classifier: LlmClassifier = _default_llm_classifier,
) -> list[RankableItem]:
    """Stage 1-2 (YouTube half): delta-fetch new uploads, classify, build RankableItems.

    The YouTube-source analog of :func:`run_stage1_build_x_items`. For each persisted
    YouTube source (``store.list_sources(platform="youtube")``) it delta-fetches the new
    uploads via the injectable ``upload_delta`` (defaults to
    :func:`lib.youtube_yt.fetch_new_uploads`, which already filters out ``seen`` ids),
    classifies EACH upload on the SAME two-axis :func:`lib.classify.classify_item` path as
    X (the channel-level Axis-A prior is the source row's ``category``), and adapts each
    into the shared :class:`RankableItem` via :meth:`RankableItem.from_parts` (carrying its
    ``duration`` so the clusterer can detect long-form). The result merges into the SAME
    unified stream X tweets feed, so videos and tweets cluster, rank, and render together.

    NOTE (pipeline reorder): transcript fetch + chapterization no longer happen here.
    They are EXPENSIVE and now run only for the ONE crowned winner of each topic cluster,
    in :func:`run_stage5_summarize_winners` (after clustering collapses duplicate coverage)
    — so a daily run transcribes a bounded handful of winners, never the whole feed. Items
    therefore leave this stage with an empty ``chapters`` list.

    Per-channel fetch failures are best-effort: a :class:`lib.youtube_yt.YouTubeFetchError`
    (a single channel's timeout / transient listing failure) is logged with an actionable
    ``fix_suggestion`` and SKIPPED so one bad channel never loses the whole YouTube half.
    A seen-mark is written AFTER each upload is successfully built (delta-engine contract:
    marking is the driver's post-success job, never pre-marked).

    No-op (returns ``[]``) when there are no YouTube sources. ``orbit.py`` stays
    wiring-only (Rule 5): delta/classify/build all live in lib/.

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``interests`` for Axis B).
        depth: ``quick`` | ``default`` | ``deep`` — passed through to the upload delta.
        upload_delta: Per-channel new-upload fetcher; defaults to
            :func:`lib.youtube_yt.fetch_new_uploads`. Injectable so tests mock the
            subprocess boundary.
        mark_seen: Seen-marker; defaults to :func:`store.mark_seen`. Injectable for tests.
        llm_classifier: The injectable classify LLM boundary; tests inject a mock, the
            host session wires the real caller at runtime.

    Returns:
        The new YouTube uploads as classified :class:`RankableItem`s (possibly empty).
    """
    youtube_sources = store.list_sources(platform="youtube")
    if not youtube_sources:
        log.log_info("youtube_stage1_no_sources", platform="youtube")
        return []

    active_upload_delta = upload_delta or fetch_new_uploads
    active_mark_seen = mark_seen or store.mark_seen

    total_new_uploads = 0
    classified_count = 0

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
        remaining_budget = _STAGE1_MAX_CLASSIFIED_UPLOADS - classified_count
        for upload in recent_uploads[:remaining_budget]:
            classified_count += 1
            classification = classify.classify_item(
                upload,
                channel_category=channel_category,
                interests=config.interests,
                llm_classifier=llm_classifier,
            )

            # Transcript + chapterize moved to run_stage5_summarize_winners (winners only).
            # Items leave Stage 1 with no chapters; ``duration`` flows through from_parts so
            # the clusterer can still detect long-form by duration.
            rankable_items.append(
                RankableItem.from_parts(
                    upload, classification, [], creator_external_id=channel_id
                )
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
        rankable_count=len(rankable_items),
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


def run_stage3_cluster(items: list[RankableItem], config: OrbitConfig) -> list[Cluster]:
    """Stage 3: cluster the unified stream so duplicate coverage of one topic collapses.

    Runs BEFORE summarization (the reorder): :func:`lib.cluster.cluster_overlaps` groups
    same-topic items (lexical similarity + entity overlap) so the next stage can crown ONE
    winner per topic and summarize only that one. Empty ``items`` -> ``[]``. Pure wiring.

    Because chapterization now happens AFTER clustering, items reach here with no chapters;
    the clusterer's default long-form predicate falls through to "short", so same-topic
    long-form videos merge into one cluster body too (the locked decision: don't surface 5
    summaries of one release). Each cluster carries ``all_member_item_ids`` — the full
    membership the crown stage ranks.

    Args:
        items: The unified classified :class:`RankableItem` stream.
        config: The loaded :class:`OrbitConfig` (``creator_weights`` for representatives).

    Returns:
        The clusters (one per topic group; singletons are 1-member clusters).
    """
    if not items:
        log.log_info("cluster_completed", cluster_count=0, item_count=0)
        return []
    clusters = cluster_overlaps(items, config)
    log.log_info("cluster_completed", cluster_count=len(clusters), item_count=len(items))
    return clusters


def run_stage4_crown_winners(
    items: list[RankableItem],
    clusters: list[Cluster],
    config: OrbitConfig,
) -> list[RankableItem]:
    """Stage 4: crown ONE winner per cluster; fold the rest in as footnotes.

    For each cluster, the winner is the member with the highest
    :func:`lib.rerank.winner_score` (the derank score biased toward longer / more
    in-depth videos — locked decision 3), ties broken by ``item_external_id`` for
    determinism. The non-winner members are attached to the winner's ``.footnotes`` (so
    the renderer folds them under its card as "Also covered" links) and recorded on the
    cluster (``winner_item_id`` / ``footnote_item_ids``). Only the winners proceed to
    summarize/rank/render — duplicate coverage of one story becomes a single card plus
    footnotes (rank still controls density among winners; nothing a user cares about is
    dropped — the losers are the footnotes).

    Creator engagement baselines are computed ONCE over the whole batch and reused for
    every cluster's winner pick so the choice is consistent.

    Args:
        items: The full unified :class:`RankableItem` stream (winners + losers).
        clusters: The Stage-3 clusters (mutated in place: winner/footnote ids set).
        config: The loaded :class:`OrbitConfig` (``creator_weights``).

    Returns:
        The winners — one :class:`RankableItem` per cluster (each carrying its footnotes).
    """
    if not items or not clusters:
        return list(items)

    items_by_id = {str(item.item_external_id): item for item in items if item.item_external_id}
    baselines = compute_creator_engagement_baselines(items)

    winners: list[RankableItem] = []
    for cluster in clusters:
        members = [items_by_id[mid] for mid in cluster.all_member_item_ids if mid in items_by_id]
        if not members:
            continue
        # Deterministic: highest winner_score, ties broken by id (matches derank's tiebreak).
        ranked = sorted(
            members,
            key=lambda it: (winner_score(it, config, creator_baselines=baselines), str(it.item_external_id)),
            reverse=True,
        )
        winner = ranked[0]
        losers = [member for member in members if member is not winner]
        winner.footnotes = losers
        cluster.winner_item_id = str(winner.item_external_id)
        cluster.footnote_item_ids = [str(member.item_external_id) for member in losers]
        winners.append(winner)

    log.log_info(
        "crown_winners_completed",
        cluster_count=len(clusters),
        winner_count=len(winners),
        footnoted_count=sum(len(cluster.footnote_item_ids) for cluster in clusters),
    )
    return winners


def _is_x_winner(item: RankableItem) -> bool:
    """True when a winner is an X post (its ``card_url`` is an x.com permalink)."""
    return "x.com/" in (getattr(item, "card_url", "") or "")


def run_stage5_summarize_winners(
    winners: list[RankableItem],
    depth: str,
    config: OrbitConfig,
    *,
    llm_classifier: LlmClassifier = _default_llm_classifier,
    transcript_fetcher: Optional[Callable[..., Optional[Transcript]]] = None,
    summarizer: Optional[summarize.Summarizer] = None,
) -> None:
    """Stage 5: transcribe + summarize the crowned winners (capped, floor of 8 videos).

    The expensive stage, run ONLY for winners (so it summarizes the deduplicated set):

      * VIDEO winners (sorted by :func:`lib.rerank.winner_score`, top N where
        ``N = max(SUMMARY_FLOOR, SUMMARY_CAP_BY_DEPTH[depth])`` — so at least 8 distinct
        video topics are summarized every run, even at ``quick``): fetch the transcript
        (``force=True`` to bypass the quick=0 feed-wide gate), then
        :func:`lib.summarize.summarize_video` into exactly 5 timestamped bullets attached
        to ``item.summary``. Winners beyond the cap render as plain cards (no summary).
      * X (tweet) winners (top ``X_SUMMARY_CAP_BY_DEPTH[depth]``):
        :func:`lib.summarize.summarize_tweet` into 2-3 timeless bullets.

    The 5 timestamped summary bullets ARE the per-winner deep-link navigation now (they
    supersede the old chapter list on the card), so winners are NOT separately chapterized.
    Mutates the winner items in place (sets ``.summary``). The same ``claude -p`` boundary
    serves classify and summarize.

    Args:
        winners: The Stage-4 winners (mutated in place).
        depth: ``quick`` | ``default`` | ``deep`` — selects the per-run summary caps.
        config: The loaded :class:`OrbitConfig`.
        llm_classifier: The live LLM boundary (used as the default summarizer).
        transcript_fetcher: Transcript fetcher; defaults to
            :func:`lib.transcribe.fetch_transcript_with_cues`. Must accept ``force=``.
        summarizer: Summarize LLM boundary; defaults to ``llm_classifier``.
    """
    if not winners:
        return

    active_transcript_fetcher = transcript_fetcher or fetch_transcript_with_cues
    active_summarizer = summarizer if summarizer is not None else llm_classifier

    baselines = compute_creator_engagement_baselines(winners)

    def by_winner_score(item: RankableItem) -> tuple[float, str]:
        return (winner_score(item, config, creator_baselines=baselines), str(item.item_external_id))

    video_winners = sorted(
        (winner for winner in winners if not _is_x_winner(winner)), key=by_winner_score, reverse=True
    )
    x_winners = sorted((winner for winner in winners if _is_x_winner(winner)), key=by_winner_score, reverse=True)

    video_cap = max(SUMMARY_FLOOR, SUMMARY_CAP_BY_DEPTH.get(depth, SUMMARY_CAP_BY_DEPTH["default"]))
    x_cap = X_SUMMARY_CAP_BY_DEPTH.get(depth, X_SUMMARY_CAP_BY_DEPTH["default"])

    video_summarized = 0
    for winner in video_winners[:video_cap]:
        # force=True: the quick=0 transcript gate is feed-wide; the bounded winner set opts in.
        transcript = active_transcript_fetcher(winner.item_external_id, depth, force=True)
        winner.summary = summarize.summarize_video(winner, transcript, summarizer=active_summarizer)
        video_summarized += 1

    x_summarized = 0
    for winner in x_winners[:x_cap]:
        winner.summary = summarize.summarize_tweet(winner, summarizer=active_summarizer)
        x_summarized += 1

    log.log_info(
        "summary_cap_applied",
        depth=depth,
        video_winners=len(video_winners),
        video_summarized=video_summarized,
        video_cap=video_cap,
        summary_floor=SUMMARY_FLOOR,
        x_winners=len(x_winners),
        x_summarized=x_summarized,
        x_cap=x_cap,
    )


def run_stage6_trending_scoops(
    clusters: list[Cluster],
    items: list[RankableItem],
    config: OrbitConfig,
    *,
    store_module: Any = store,
    search_fn: Optional[Callable[[str], list[Any]]] = None,
) -> tuple[list[TrendingItem], list[TrendingItem], dict[str, float]]:
    """Stage 6 (M3): internal trending -> external tag -> scoops, over the EXISTING clusters.

    Consumes the Stage-3 clusters (does NOT re-cluster) and sequences the deterministic
    trending lib functions (Rule 5 — all logic in lib/):

      1. :func:`lib.trending.compute_internal_trending` — baseline-relative velocity
         (reads ``store_module`` only for each creator's ``seen``-history DEPTH).
      2. :func:`lib.external_trending.tag_external_corroboration` — bounded keyless
         cross-search tagging corroborated-vs-scoop, throttled by ``config.depth``.
      3. :func:`lib.external_trending.detect_scoops` + ``build_trending_multiplier_map``
         for the rerank boost.

    Empty ``clusters`` -> ``([], [], {})`` (neutral multiplier map; rank unchanged).

    Args:
        clusters: The Stage-3 clusters.
        items: The items whose ids the clusters reference (for the deep-link map).
        config: The loaded :class:`OrbitConfig` (``depth`` for the cross-search budget).
        store_module: The store module for the history-depth lookup (injectable).
        search_fn: OPTIONAL keyless cross-search; None uses the lib default.

    Returns:
        ``(trending_items, scoops, trending_multipliers)``.
    """
    if not clusters:
        log.log_info("trending_scoops_completed", trending_count=0, scoop_count=0)
        return [], [], {}

    items_by_id = {str(item.item_external_id): item for item in items if item.item_external_id}
    trending_items = compute_internal_trending(clusters, items_by_id, store_module)

    if search_fn is not None:
        tag_external_corroboration(trending_items, search_fn=search_fn, depth=config.depth)
    else:
        tag_external_corroboration(trending_items, depth=config.depth)

    scoops = detect_scoops(trending_items)
    trending_multipliers = build_trending_multiplier_map(trending_items)

    log.log_info(
        "trending_scoops_completed",
        cluster_count=len(clusters),
        trending_count=len(trending_items),
        scoop_count=len(scoops),
        multiplier_count=len(trending_multipliers),
    )
    return trending_items, scoops, trending_multipliers


def run_stage6_rank_and_tier(
    items: list[RankableItem],
    config: OrbitConfig,
    *,
    trending_multipliers: Optional[dict[str, float]] = None,
) -> list[TieredItem]:
    """Stage 6: score the rankable items, then sort them into density tiers.

    Pure delegation to ``lib.rerank.derank_items`` (weighted score, descending) then
    ``lib.density.assign_density_tiers`` (rank -> hero/standard/compact/index). Rank
    controls density, NEVER inclusion — ``len(out) == len(items)``; nothing dropped.
    No LLM (Rule 5 — deterministic math in lib/); orbit.py stays wiring-only.

    Args:
        items: The :class:`RankableItem`s from the (upstream) classify/chapterize half.
        config: The loaded :class:`OrbitConfig` (supplies ``creator_weights``).
        trending_multipliers: OPTIONAL ``item_external_id`` -> trending/scoop multiplier
            map from Stage 5 (M3). None (the M1/M2 path) leaves every multiplier neutral,
            so ranking is byte-for-byte unchanged.

    Returns:
        The tiered, rank-ordered items ready for the renderer.
    """
    scored_items = derank_items(items, config, trending_multipliers=trending_multipliers)
    tiered_items = assign_density_tiers(scored_items)
    log.log_info("rank_and_tier_completed", item_count=len(tiered_items))
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


def run_stage7_render(
    tiered_items: list[TieredItem],
    config: OrbitConfig,
    *,
    html_path: Optional[Path] = None,
    writer: Callable[[Path, str], None] = _default_html_writer,
    clusters: Optional[list[Cluster]] = None,
    trending_items: Optional[list[TrendingItem]] = None,
    scoops: Optional[list[TrendingItem]] = None,
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

    pages = render.render_digest_pages(
        tiered_items,
        config,
        page_2_href=render.DEFAULT_PAGE_2_FILENAME,
        clusters=clusters,
        trending_items=trending_items,
        scoops=scoops,
    )

    written_paths: list[Path] = [page_1_path]
    writer(page_1_path, pages[0])
    if len(pages) > 1:
        writer(page_2_path, pages[1])
        written_paths.append(page_2_path)

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
    items and names the top scoop / top item so the iMessage/WhatsApp body is a useful
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
    config: OrbitConfig,
    page_1_path: Path,
) -> None:
    """Deliver the digest (iMessage core; WhatsApp/Briefcast gated stretch). Wiring only.

    Runs AFTER :func:`run_stage7_render`. Builds a deterministic one-line TL;DR
    (:func:`_build_delivery_summary` — no LLM, Rule 5) and routes it through the
    delivery channels in :mod:`lib.deliver`:

      * iMessage (core) — always attempted; :func:`lib.deliver.deliver_imessage` is a
        logged no-op when ``delivery.imessage_to`` is unset (the bare CLI run path).
      * WhatsApp (stretch) — gated behind ``delivery.whatsapp_to``; skipped by default.
        Wired only when a target is set; the host injects the HTTP boundary at runtime
        (not wired here, so a misconfigured target without that boundary fails loud
        rather than sending). Left unwired in this build to keep the stretch path gated.
      * Briefcast (stretch) — gated behind ``delivery.briefcast_path``; writes a payload
        file when configured, skipped otherwise.

    Business logic lives in :mod:`lib.deliver`; this stays sequencing only.

    Args:
        tiered_items: The Stage-6 tiered items (for the TL;DR + Briefcast episode list).
        scoops: The Stage-5 scoops (the TL;DR leads with the loudest one).
        config: The loaded :class:`OrbitConfig` (supplies the ``delivery`` targets).
        page_1_path: The page-1 HTML path the render stage wrote (the link target).
    """
    summary = _build_delivery_summary(tiered_items, scoops)

    imessage_to = config.delivery.get("imessage_to")
    deliver.deliver_imessage(summary, page_1_path, imessage_to)

    # Stretch channels — gated behind their config keys, skipped by default. Briefcast
    # is wired (a file, no auth surface); WhatsApp stays gated on its config key but is
    # not given an HTTP boundary here (the host wires it), so an accidental target fails
    # loud in lib.deliver rather than this stage sending without a configured client.
    briefcast_path = config.delivery.get("briefcast_path")
    if briefcast_path:
        deliver.emit_briefcast_payload(summary, list(tiered_items), briefcast_path)


def run_pipeline(depth: str) -> int:
    """Run the Orbit pipeline. Stage 0 is real; later stages are still stubs.

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

    # Stage 1-2 — delta-fetch new uploads/tweets, classify them on the live LLM boundary,
    # build RankableItems. Transcript/chapterize/summary are NOT done here anymore (they
    # run only for cluster winners in Stage 5). No-op (empty) when a source type is absent.
    youtube_items = run_stage1_build_youtube_items(config, depth, llm_classifier=llm_classifier)
    x_items = run_stage1_build_x_items(config, depth, llm_classifier=llm_classifier)

    # The unified stream merges YouTube uploads with X tweet RankableItems.
    rankable_items = youtube_items + x_items

    # Stage 3 — cluster so duplicate coverage of one topic collapses into one group.
    clusters = run_stage3_cluster(rankable_items, config)

    # Stage 4 — crown ONE winner per cluster (deterministic, duration-biased); the rest
    # become "Also covered" footnotes on the winner. Only winners proceed downstream.
    winners = run_stage4_crown_winners(rankable_items, clusters, config)

    # Stage 5 — transcribe + summarize the winners (5 timestamped bullets per video, 2-3
    # per tweet), capped per run with a floor of >=8 video topics. Mutates winners in place
    # (attaches .summary, and .chapters for long-form). The same llm boundary serves
    # classify, chapterize, and summarize.
    run_stage5_summarize_winners(
        winners, depth, config, llm_classifier=llm_classifier, summarizer=llm_classifier
    )

    # Stage 6 (M3) — internal trending + external corroboration + scoops over the existing
    # clusters. Feeds the trending/scoop multiplier into rank and the M3 render sections.
    trending_items, scoops, trending_multipliers = run_stage6_trending_scoops(clusters, rankable_items, config)

    # Stage 7-8 — rank the WINNERS (the deduplicated set) and render. Rank controls density
    # among winners; their footnotes ride along on each card.
    tiered_items = run_stage6_rank_and_tier(winners, config, trending_multipliers=trending_multipliers)
    written_paths = run_stage7_render(
        tiered_items, config, clusters=clusters, trending_items=trending_items, scoops=scoops
    )

    # Stage 7 (deliver) — notify the user the digest is ready. iMessage is opt-in: on
    # the bare CLI run delivery.imessage_to is unset, so this is a logged no-op and the
    # run stays a clean exit-0. page 1 is written_paths[0] (page 1 first by contract).
    run_stage7_deliver(tiered_items, scoops, config, written_paths[0])

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
