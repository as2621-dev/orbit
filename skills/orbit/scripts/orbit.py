#!/usr/bin/env python3
"""Orbit pipeline driver — the skill entrypoint.

This module is wiring only (Rule: orbit.py sequences stages, business logic lives
in lib/). For Phase 1 Sub-phase 1 it is a scaffold: it parses ``--depth`` and
``--setup``, then logs a structured "not yet implemented" notice per pipeline
stage. Real Stage 0 (subscription loading), classification, ranking, and render
land in later sub-phases/phases.

Run directly:
    python3 skills/orbit/scripts/orbit.py --depth default
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Make ``lib`` importable when this script is run directly (e.g.
# ``python3 skills/orbit/scripts/orbit.py``). Mirrors the last30days reference so
# lifted modules import ``from lib import log, subproc`` unchanged in later phases.
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import store  # noqa: E402  (import must follow the sys.path insert above)
from lib import bird_x, classify, log, render  # noqa: E402
from lib.bird_x import Follow, Tweet, XAuthError  # noqa: E402
from lib.classify import LlmClassifier, _default_llm_classifier  # noqa: E402
from lib.config import OrbitConfig, load_config  # noqa: E402
from lib.density import TieredItem, assign_density_tiers  # noqa: E402
from lib.rerank import RankableItem, derank_items  # noqa: E402
from lib.youtube_yt import (  # noqa: E402
    Subscription,
    YouTubeAuthError,
    load_youtube_subscriptions,
    persist_subscriptions,
)

# Weekly-cache window (brief §3 Stage 0): sources are re-loaded at most once per
# week. Daily runs must NOT re-hit yt-dlp — they ride the cache.
_SOURCES_REFRESH_INTERVAL_SECONDS: int = 7 * 24 * 60 * 60

# Manual-run network guard. When set truthy, Stage 0 skips the (network-touching)
# loader even on a cold/empty DB, logging why. Lets ``--depth quick`` exit 0 without
# touching the network on a machine with no cookies. The tested guarantee uses a
# mocked loader; this env var is the equivalent escape hatch for a bare CLI run.
_STAGE0_SKIP_NETWORK_ENV_VAR: str = "ORBIT_STAGE0_SKIP_NETWORK"

# The pipeline stages, in execution order. Stage 0 (sources) and Stage 3-4
# (rank + render, this phase) are real; Stages 1-2 (delta fetch, classify,
# chapterize) are still upstream stubs wired in a later phase.
PIPELINE_STAGES: tuple[str, ...] = (
    "stage_0_load_sources",
    "stage_1_delta_fetch",
    "stage_2_classify",
    "stage_3_rank",
    "stage_4_render",
)

# Stages still stubbed (delta fetch + classify/chapterize) — the upstream half that
# feeds rankable items. This phase (Phase 3, M1) builds only the rank+render half;
# the real producers land in a later phase. Listed here so run_pipeline logs each
# as "not yet implemented" while still running the real Stage 6->7 on whatever items
# the upstream provides.
_STUBBED_UPSTREAM_STAGES: tuple[str, ...] = ("stage_1_delta_fetch", "stage_2_classify")

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
        help="Run first-time setup (cookie source, interests, delivery). Stub for now.",
    )
    return parser


def run_setup() -> int:
    """Run the first-time setup flow (stub).

    Returns:
        Process exit code (0 on success).
    """
    log.log_info("setup_started", mode="stub")
    log.log_warning(
        "setup_not_yet_implemented",
        detail="The --setup wizard ships in a later milestone (M4).",
    )
    log.log_info("setup_completed", mode="stub")
    return 0


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


def _current_day_ordinal() -> int:
    """Return today's day count since the Unix epoch (UTC) for X handle rotation.

    A monotonically increasing per-day integer so :func:`lib.bird_x.fetch_new_tweets`'s
    round-robin rotation advances every day, widening handle coverage across runs.

    Returns:
        The number of whole days since 1970-01-01 (UTC).
    """
    return (datetime.now(timezone.utc).date() - date(1970, 1, 1)).days


def run_stage6_rank_and_tier(items: list[RankableItem], config: OrbitConfig) -> list[TieredItem]:
    """Stage 6: score the rankable items, then sort them into density tiers.

    Pure delegation to ``lib.rerank.derank_items`` (weighted score, descending) then
    ``lib.density.assign_density_tiers`` (rank -> hero/standard/compact/index). Rank
    controls density, NEVER inclusion — ``len(out) == len(items)``; nothing dropped.
    No LLM (Rule 5 — deterministic math in lib/); orbit.py stays wiring-only.

    Args:
        items: The :class:`RankableItem`s from the (upstream) classify/chapterize half.
        config: The loaded :class:`OrbitConfig` (supplies ``creator_weights``).

    Returns:
        The tiered, rank-ordered items ready for the renderer.
    """
    scored_items = derank_items(items, config)
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

    pages = render.render_digest_pages(tiered_items, config, page_2_href=render.DEFAULT_PAGE_2_FILENAME)

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

    # The YouTube delta-fetch + classify/chapterize half (Stages 1-2 for YouTube) is
    # still an upstream stub — its real producer lands in a later phase. It yields NO
    # rankable items yet. The X half's Stage 1 (delta + classify + build) IS real this
    # phase, but classification needs a live LLM boundary that does not exist in this
    # build env (the default fails loud, Rule 12). A bare CLI run therefore cannot
    # classify, so it logs the upstream stubs and ranks+renders whatever non-classified
    # items exist (currently none). The unified merge + X Stage-1 path is exercised
    # end-to-end by the integration test, which injects a mock LLM + mock X delta.
    for stage_name in _STUBBED_UPSTREAM_STAGES:
        log.log_warning(
            "stage_not_yet_implemented",
            stage=stage_name,
            depth=depth,
            detail=(
                "Upstream producer (YouTube delta fetch / classify+chapterize) lands in a "
                "later phase; the X Stage-1 path is wired but needs a runtime LLM classifier."
            ),
        )

    # Stage 6->7 (rank + render) — REAL this phase. The unified stream merges YouTube
    # uploads (stubbed-empty in a bare run) with X tweet RankableItems; both flow
    # through the SAME rank/tier/render (the M2 unified-digest seam).
    youtube_items: list[RankableItem] = []  # upstream stubbed; empty until a later phase
    x_items: list[RankableItem] = []  # needs a runtime LLM classifier; empty in a bare CLI run
    rankable_items = youtube_items + x_items
    tiered_items = run_stage6_rank_and_tier(rankable_items, config)
    written_paths = run_stage7_render(tiered_items, config)

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

    if parsed_args.setup:
        return run_setup()
    return run_pipeline(depth=parsed_args.depth)


if __name__ == "__main__":
    raise SystemExit(main())
