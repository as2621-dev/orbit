"""DoD test: an X (tweet) winner's summary persists to the shared summaries store.

Per Rule 9: the summary store is keyed by ``item_external_id`` so BOTH videos and tweets
upsert into one table (the override + cache contract is uniform across sources). A tweet
winner summarized via the shared path must persist under its ``tweet_id`` and round-trip
through ``store.get_summary`` — a regression that forked tweet storage (or skipped
persistence) would break override caching for X posts. NO network, NO live model.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths, summarize  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402


def _fresh_store(tmp_dir: Path) -> Path:
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def test_tweet_summary_persists_to_shared_store_by_tweet_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        tweet_item = RankableItem(
            item_external_id="999",
            title="an original tweet about agents",
            channel_name="bob",
            creator_external_id="bob",
            view_count=3,
            like_count=20,
            comment_count=1,
            upload_date="20260101",
            card_url="https://x.com/bob/status/999",
        )
        raw = json.dumps([{"text": "claim one"}, {"text": "claim two"}])
        summary = summarize.summarize_tweet(tweet_item, summarizer=lambda prompt: raw)

        assert summary.item_external_id == "999"
        persisted = store.get_summary("999")

    assert persisted is not None, "tweet summary must persist to the shared summaries table"
    assert persisted["item_external_id"] == "999"
    stored_bullets = json.loads(persisted["bullets_json"])
    assert [b["text"] for b in stored_bullets] == ["claim one", "claim two"]
