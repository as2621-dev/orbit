"""DoD tests for Stage-0 wiring + config loading (Sub-phase 4).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:
Stage 0 is the foundation the whole pipeline stands on, so the tests assert the
intents future phases depend on — (1) a first run populates ``sources`` so the
Phase-2 delta engine has a baseline, (2) a daily re-run rides the weekly cache and
does NOT re-hit yt-dlp, (3) a bad ``depth`` in config fails loud at the boundary
instead of silently defaulting.

All external boundaries are mocked: the subscriptions loader is injected (no real
yt-dlp, no network), and the store is pointed at a temp DB via ``ORBIT_DB_PATH`` +
``store._db_override`` (no real ``~/.local/share`` write). Mirrors the temp-DB setup
in ``tests/test_store.py`` / ``tests/test_youtube_yt.py``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# Make ``skills/orbit/scripts`` importable so ``import store`` / ``import orbit`` and
# ``from lib import ...`` resolve regardless of the working directory.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orbit  # noqa: E402
import store  # noqa: E402
from lib import paths  # noqa: E402
from lib.config import ConfigError, OrbitConfig, load_config  # noqa: E402
from lib.youtube_yt import Subscription  # noqa: E402


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def test_stage0_first_run_populates_sources_table() -> None:
    """First Stage-0 run must call the loader and land its channels in ``sources``.

    WHY: the first run is the only chance to give the delta engine a baseline set of
    channels to diff. If Stage 0 didn't populate ``sources`` on an empty DB, Phase 2
    would have nothing to fetch and the user would silently see no uploads.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        config = OrbitConfig(cookie_source="chrome")
        mock_loader = MagicMock(
            return_value=[
                Subscription(channel_id="UC_first_0001", display_name="Channel One"),
                Subscription(channel_id="UC_first_0002", display_name="Channel Two"),
            ]
        )

        orbit.run_stage0_load_sources(config, loader=mock_loader)

        mock_loader.assert_called_once_with("chrome")
        youtube_sources = store.list_sources(platform="youtube")
        stored_ids = {source["external_id"] for source in youtube_sources}
        assert stored_ids == {"UC_first_0001", "UC_first_0002"}


def test_stage0_second_run_is_cache_hit_and_skips_loader() -> None:
    """An immediate second Stage-0 run must NOT call the loader (weekly cache).

    WHY: Orbit runs daily, but subscriptions change weekly at most. Re-hitting yt-dlp
    every day is slow and burns the auth/cookie surface needlessly. The weekly cache
    is the contract that daily runs ride state instead of the network — a regression
    here would silently re-fetch on every run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        config = OrbitConfig(cookie_source="chrome")

        first_loader = MagicMock(
            return_value=[Subscription(channel_id="UC_cache_0001", display_name="Cached One")]
        )
        orbit.run_stage0_load_sources(config, loader=first_loader)
        first_loader.assert_called_once()

        # persist_subscriptions stamps last_refreshed_at = now, so the second run sees
        # a warm cache and must skip a fresh loader entirely.
        second_loader = MagicMock(return_value=[])
        orbit.run_stage0_load_sources(config, loader=second_loader)
        second_loader.assert_not_called()


def test_stage0_refreshes_when_sources_are_stale() -> None:
    """A source older than 7 days must trigger a refresh (loader IS called).

    WHY: the cache is a 7-day window, not forever. If a stale list never refreshed,
    newly-followed channels would never enter the pipeline. This locks the upper
    boundary of the weekly-cache rule.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        config = OrbitConfig(cookie_source="chrome")

        stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        store.upsert_source(
            platform="youtube",
            external_id="UC_stale_0001",
            display_name="Stale Channel",
            last_refreshed_at=stale_timestamp,
        )

        refresh_loader = MagicMock(
            return_value=[Subscription(channel_id="UC_fresh_0002", display_name="Fresh Channel")]
        )
        orbit.run_stage0_load_sources(config, loader=refresh_loader)
        refresh_loader.assert_called_once_with("chrome")


def test_load_config_rejects_invalid_depth() -> None:
    """load_config must raise ConfigError on a depth outside {quick,default,deep}.

    WHY: depth is the cost/time throttle. A typo (``"turbo"``) that silently defaulted
    would run the wrong amount of work with no signal to the user. Rule 12: a bad
    config fails loud at the boundary, not silently.
    """
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "orbit.config.json"
        config_path.write_text(json.dumps({"depth": "turbo"}), encoding="utf-8")

        raised: ConfigError | None = None
        try:
            load_config(config_path)
        except ConfigError as exc:
            raised = exc

        assert raised is not None, "invalid depth must raise ConfigError, not default silently"
        message = str(raised)
        assert "depth" in message
        assert "turbo" in message
        assert "default" in message  # names the allowed set


def test_load_config_missing_file_returns_defaults() -> None:
    """An absent config file must yield all-default OrbitConfig (first-run friendly).

    WHY: Orbit runs on a clean machine before the user writes a config. The pipeline
    must start with sane defaults rather than crash on a missing file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        missing_path = Path(tmp) / "does_not_exist.json"
        config = load_config(missing_path)
        assert config.cookie_source == "chrome"
        assert config.depth == "default"
        assert config.schedule == "0 7 * * *"


def _run_all_standalone() -> int:
    """Run every ``test_*`` function in this module without pytest. Returns exit code."""
    test_functions = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures: list[str] = []
    for test_function in test_functions:
        try:
            test_function()
            print(f"PASS {test_function.__name__}")
        except Exception as exc:  # noqa: BLE001 — standalone runner surfaces any failure
            failures.append(f"FAIL {test_function.__name__}: {exc!r}")
            print(failures[-1])
    print(f"\n{len(test_functions) - len(failures)}/{len(test_functions)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all_standalone())
