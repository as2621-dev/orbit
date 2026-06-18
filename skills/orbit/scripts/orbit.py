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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Make ``lib`` importable when this script is run directly (e.g.
# ``python3 skills/orbit/scripts/orbit.py``). Mirrors the last30days reference so
# lifted modules import ``from lib import log, subproc`` unchanged in later phases.
sys.path.insert(0, str(Path(__file__).parent.resolve()))

import store  # noqa: E402  (import must follow the sys.path insert above)
from lib import log, render  # noqa: E402
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
) -> None:
    """Stage 0: load YouTube subscriptions into ``sources`` (deterministic, no LLM).

    Initializes the DB, reads existing youtube sources, and applies the weekly-cache
    rule (:func:`_sources_need_refresh`): on a cache hit it logs ``sources_cache_hit``
    and returns WITHOUT calling the loader (daily runs ride the cache); on a needed
    refresh it calls the (injectable) loader with ``config.cookie_source``, persists,
    and logs ``sources_refreshed``. A :class:`YouTubeAuthError` is logged with an
    actionable ``fix_suggestion`` and RE-RAISED (fail loud, Rule 12 — never swallowed).

    The ``loader``/``persist`` callables default to the real youtube_yt functions and
    are injectable so tests pass a mock without monkeypatching internals.

    Args:
        config: The loaded :class:`OrbitConfig` (supplies ``cookie_source``).
        db_path: Explicit DB path (tests pass a temp path); defaults to the per-user DB.
        loader: Subscription loader; defaults to ``load_youtube_subscriptions``.
        persist: Persist function; defaults to ``persist_subscriptions``.

    Raises:
        YouTubeAuthError: If the loader fails to authenticate (re-raised, not swallowed).
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

    # Stages 1-2 (delta fetch + classify/chapterize) are still upstream stubs — the
    # real producers land in a later phase. They yield NO rankable items yet, so a
    # bare CLI run ranks+renders an empty (but valid) digest. The Stage 6->7 code
    # below is REAL and runs on whatever items the upstream provides.
    for stage_name in _STUBBED_UPSTREAM_STAGES:
        log.log_warning(
            "stage_not_yet_implemented",
            stage=stage_name,
            depth=depth,
            detail="Upstream producer (delta fetch / classify+chapterize) lands in a later phase.",
        )

    # Stage 6->7 (rank + render) — REAL this phase (Phase 3 / M1's render half).
    rankable_items: list[RankableItem] = []  # upstream stubbed; empty until a later phase
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
