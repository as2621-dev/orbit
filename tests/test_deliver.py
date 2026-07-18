"""Tests for lib.deliver — the Briefcast file emit + the shared message-body helper.

iMessage and WhatsApp delivery were deleted (PRD story #8: one delivery path to
configure, permission, and debug). What remains here has NO auth surface:

  * :func:`lib.deliver.emit_briefcast_payload` — writes the TL;DR + episode list as a
    JSON payload file (integrations §6). A file deliverable, not a network send.
  * :func:`lib.deliver.build_message_body` — a PURE helper composing the one-line
    delivery body. It survives the iMessage/WhatsApp removal because the email-delivery
    slice reuses it as the email body; this test locks its contract in the interim.

Why these tests matter (Rule 9 — encode WHY, not just WHAT):

  * The removal is a product decision, not a refactor: the send functions must NOT come
    back. ``test_send_functions_are_gone`` fails if iMessage/WhatsApp are reintroduced.
  * ``build_message_body`` is now the seam the next slice builds the email body on top of.
    Its tests pin the exact composed shape so that reuse starts from a known contract.
  * Briefcast writes real files; the tests assert the on-disk JSON carries every episode.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the skill's scripts dir importable so ``from lib import deliver`` resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import deliver  # noqa: E402


# --- build_message_body (shared body helper) ------------------------------------


def test_build_message_body_joins_summary_and_link() -> None:
    """The body is the summary and the link joined with an em-dash separator.

    WHY: this is the exact seam the email-delivery slice reuses as the email body. If the
    composed shape drifts (dropped link, different separator), the next slice inherits a
    silently-wrong body. Pin the contract now that its former iMessage caller is gone.
    """
    body = deliver.build_message_body("3 new items", "file:///tmp/today.html")
    assert body == "3 new items — file:///tmp/today.html"


def test_build_message_body_falls_back_when_summary_blank() -> None:
    """A blank summary falls back to a sane default line, never an empty lead.

    WHY: the summary can be empty on a quiet feed day; the body must still read as a
    notification, not a bare separator + link. This pins the fallback so an empty-feed
    email is not a broken-looking one.
    """
    body = deliver.build_message_body("   ", "file:///tmp/today.html")
    assert body == "Your Orbit digest is ready. — file:///tmp/today.html"


# --- removal is a product decision, not a refactor ------------------------------


def test_send_functions_are_gone() -> None:
    """The iMessage/WhatsApp send surface must not exist on the module (PRD story #8).

    WHY: iMessage and WhatsApp were deleted, not deprecated — one delivery path to
    maintain. If any of these come back (a re-added helper, a stray constant), this fails.
    It guards the product decision at the module boundary.
    """
    removed = [
        "deliver_imessage",
        "deliver_whatsapp",
        "_escape_for_applescript",
        "_build_imessage_applescript",
        "TWILIO_AUTH_TOKEN_ENV_VAR",
        "TWILIO_ACCOUNT_SID_ENV_VAR",
        "TWILIO_WHATSAPP_FROM_ENV_VAR",
    ]
    present = [name for name in removed if hasattr(deliver, name)]
    assert present == [], f"deleted delivery surface reappeared on lib.deliver: {present}"


# --- emit_briefcast_payload ------------------------------------------------------


def test_emit_briefcast_payload_writes_file_with_episode_list(tmp_path: Path) -> None:
    """The Briefcast payload file contains the summary and the episode list.

    WHY: Briefcast is a file deliverable (integrations §6) — the test confirms it writes
    valid JSON carrying every episode, so a downstream Briefcast consumer gets the list.
    """
    out_path = tmp_path / "briefcast" / "payload.json"
    episodes = [
        SimpleNamespace(title="Episode One", card_url="https://x.com/a/status/1"),
        SimpleNamespace(title="Episode Two", card_url=""),
    ]

    written = deliver.emit_briefcast_payload("Orbit: 2 new items", episodes, out_path)

    assert written.exists()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["summary"] == "Orbit: 2 new items"
    assert payload["episode_count"] == 2
    titles = [ep["title"] for ep in payload["episodes"]]
    assert titles == ["Episode One", "Episode Two"], "every episode must appear in the payload"


def test_emit_briefcast_payload_unwraps_tiered_items(tmp_path: Path) -> None:
    """A TieredItem-shaped episode is unwrapped to its RankableItem title/url.

    WHY: orbit.py passes the Stage-6 tiered items straight in; the payload writer must
    reach through .scored_item.item so the file carries real titles, not repr noise.
    """
    out_path = tmp_path / "payload.json"
    rankable = SimpleNamespace(title="Deep Dive", card_url="https://x.com/a/status/9")
    tiered = SimpleNamespace(scored_item=SimpleNamespace(item=rankable))

    written = deliver.emit_briefcast_payload("Orbit: 1 new item", [tiered], out_path)

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["episodes"][0] == {"title": "Deep Dive", "url": "https://x.com/a/status/9"}


# --- orbit.py delivery seam (no send step after the iMessage removal) -----------


def test_orbit_delivery_seam_sends_nothing_on_bare_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_stage7_deliver completes without a send and without emitting Briefcast.

    WHY: after the iMessage removal the delivery stage has NO send step until the email
    slice wires one — the interim state is a clean no-op that still lets the pipeline
    finish. This pins that: a bare config (no briefcast_path) must NOT fire Briefcast and
    run_stage7_deliver must return cleanly, so run_pipeline stays green end-to-end.
    """
    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    monkeypatch.setattr(
        orbit.deliver,
        "emit_briefcast_payload",
        lambda *a, **k: pytest.fail("Briefcast must not fire without delivery.briefcast_path"),
    )

    config = OrbitConfig(delivery={})  # no briefcast_path — the bare interim path

    result = orbit.run_stage7_deliver([], [], config)

    assert result is None, "the delivery seam is a clean no-op on the bare path"
