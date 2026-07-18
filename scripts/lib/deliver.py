"""Orbit Stage-7 delivery — the shared body helper + the Briefcast file emit.

The pipeline writes the digest HTML locally; the send step that turns it into a
delivered message lives in the email-delivery slice (PRD M5). iMessage and WhatsApp
delivery were DELETED, not deprecated (PRD story #8: one delivery path to configure,
permission, and debug) — so this module carries no network/credential surface at all.

What remains here (Rule 5 — deterministic, no LLM):

  * :func:`build_message_body` — a PURE helper composing the one-line delivery body
    (TL;DR + a link). It outlives the iMessage removal because the email-delivery slice
    reuses it as the email body.
  * :func:`emit_briefcast_payload` — OPTIONAL / STRETCH. Writes the TL;DR + episode list
    as a JSON Briefcast payload file (integrations §6). A file, not a live integration —
    NO auth surface.

Security (hard rule, brief §4/§8.5 + CLAUDE.md): no credential is hardcoded, logged, or
transmitted. Nothing in this module reaches the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Make ``lib`` importable whether this module is imported as ``lib.deliver`` or run
# from the scripts dir directly. Mirrors config.py / store.py so ``from lib import
# log`` resolves in both cases.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)


def build_message_body(summary: str, html_link: str) -> str:
    """Compose the short delivery body: the TL;DR summary + a link to the digest page.

    A PURE helper (deterministic, no I/O — Rule 5) shared by the delivery path so the body
    stays consistent. The ``summary`` already folds in the TL;DR + scoops (the caller in
    ``orbit.py`` builds it from the tiered items / scoops). Kept to one line so it reads as
    a notification, not a wall of text. The email-delivery slice reuses this as the email
    body.

    Args:
        summary: The one-line TL;DR (already includes any scoops prefix).
        html_link: A link to the HTML digest.

    Returns:
        The composed message body string.

    Example:
        >>> build_message_body("3 new items", "file:///tmp/today.html")
        '3 new items — file:///tmp/today.html'
    """
    summary_text = summary.strip() or "Your Orbit digest is ready."
    return f"{summary_text} — {html_link}"


def emit_briefcast_payload(summary: str, episodes: list[Any], out_path: Path | str) -> Path:
    """Write the TL;DR + episode list as a Briefcast JSON payload — OPTIONAL / STRETCH.

    Stretch path (integrations §6): a file/format, NOT a live integration — no auth
    surface. Writes a small JSON document (``{summary, episode_count, episodes}``) to
    ``out_path``, creating parent directories as needed. Each episode is coerced to a
    light, JSON-safe ``{title, url}`` shape via :func:`_episode_to_payload` so a list of
    :class:`lib.density.TieredItem` / :class:`lib.rerank.RankableItem` / plain dicts all
    serialize cleanly.

    Args:
        summary: The one-line TL;DR for the payload header.
        episodes: The episode list (TieredItems / RankableItems / dicts / strings).
        out_path: Where to write the JSON payload (``~``/relative ok).

    Returns:
        The absolute path the payload was written to.
    """
    resolved_path = Path(out_path).expanduser()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "summary": summary,
        "episode_count": len(episodes),
        "episodes": [_episode_to_payload(episode) for episode in episodes],
    }
    resolved_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    log.log_info(
        "briefcast_payload_written",
        channel="briefcast",
        out_path=str(resolved_path),
        episode_count=len(episodes),
    )
    return resolved_path


def _episode_to_payload(episode: Any) -> dict[str, Any]:
    """Coerce one episode of any shape into a JSON-safe ``{title, url}`` dict.

    Tolerant by design (Briefcast is a stretch convenience): accepts a TieredItem
    (``.scored_item.item``), a RankableItem-like object (``.title`` / ``.card_url``), a
    dict, or a bare string — so the caller need not pre-shape the list.

    Args:
        episode: One episode of any supported shape.

    Returns:
        A ``{"title": ..., "url": ...}`` dict (url may be an empty string).
    """
    # A TieredItem wraps the RankableItem under .scored_item.item.
    rankable = getattr(getattr(episode, "scored_item", None), "item", None) or episode

    if isinstance(rankable, dict):
        return {"title": str(rankable.get("title", "")), "url": str(rankable.get("url") or rankable.get("card_url", ""))}

    title = getattr(rankable, "title", None)
    if title is None:
        return {"title": str(rankable), "url": ""}
    return {"title": str(title), "url": str(getattr(rankable, "card_url", "") or "")}
