"""Tests for lib.deliver — iMessage (core) + WhatsApp / Briefcast (stretch).

Why these tests matter (Rule 9 — encode WHY, not just WHAT):

  * Delivery is OPT-IN. The single most important security/UX property is that Orbit
    NEVER messages without a configured target. The no-op tests fail if that intent
    regresses (e.g. someone makes an unset target send "to nobody" or raise).
  * AppleScript injection is a real risk: a TL;DR or handle the user pastes could
    contain a double-quote and break out of the script literal. The escaping test
    fails if the escaping is dropped — that is a code-injection regression, not a
    cosmetic one.
  * WhatsApp is a credential surface. The fail-loud test encodes that a configured
    WhatsApp target with NO credential must raise (Rule 12) rather than silently
    no-op — a silent skip would make the user think delivery worked when it didn't.
  * ALL external boundaries are mocked: no real osascript/iMessage, no real Twilio
    HTTP. A test that hit a real service would be both flaky and a security hazard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

# Make the skill's scripts dir importable so ``from lib import deliver`` resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "orbit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import deliver  # noqa: E402


class _FakeCompleted:
    """A minimal stand-in for ``subprocess.CompletedProcess`` the runner returns."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_recording_runner(returncode: int = 0):
    """Build a fake subprocess runner that records its argv and returns a fixed code.

    Returns a ``(runner, calls)`` pair; ``calls`` is a list the runner appends each
    invocation's argv to, so a test can assert ``osascript`` was (or was not) called.
    """
    calls: list[Sequence[str]] = []

    def runner(argv: Sequence[str]) -> Any:
        calls.append(argv)
        return _FakeCompleted(returncode=returncode, stderr="permission denied" if returncode else "")

    return runner, calls


# --- deliver_imessage: send path -------------------------------------------------


def test_deliver_imessage_sends_applescript_with_tldr_and_link() -> None:
    """A set target issues an osascript send whose script contains the TL;DR + HTML link.

    WHY: this is the core delivery contract (integrations §4) — when the user has
    configured a target, the digest TL;DR and a link to the local page must reach them
    via Messages. The test asserts the AppleScript carries both the summary text and a
    file:// link and that the subprocess boundary was invoked with 'osascript'.
    """
    runner, calls = _make_recording_runner(returncode=0)
    summary = "Orbit: 3 new items, 1 scoop"

    result = deliver.deliver_imessage(
        summary,
        "/tmp/orbit-test/today.html",
        "+15551234567",
        runner=runner,
    )

    assert result is True
    assert len(calls) == 1, "osascript should be invoked exactly once on the send path"
    argv = calls[0]
    assert argv[0] == "osascript", "the subprocess boundary must be invoked as osascript"
    assert argv[1] == "-e", "osascript must run the inline script via -e"
    script = argv[2]
    assert summary in script, "the AppleScript must carry the TL;DR text"
    assert "file://" in script and "today.html" in script, "the AppleScript must carry the HTML link"


def test_deliver_imessage_returns_false_on_osascript_nonzero() -> None:
    """A non-zero osascript exit returns False (fail loud, actionable), never a crash.

    WHY: the dominant real-world failure is a missing macOS Automation permission.
    Orbit must surface that as a recoverable False (with a fix_suggestion logged), not
    raise an opaque error or pretend the send worked.
    """
    runner, calls = _make_recording_runner(returncode=1)

    result = deliver.deliver_imessage("Orbit: 1 new item", "/tmp/orbit-test/today.html", "+15551234567", runner=runner)

    assert result is False
    assert len(calls) == 1, "osascript is still attempted; the failure is in its exit code"


# --- deliver_imessage: opt-in no-op ---------------------------------------------


@pytest.mark.parametrize("imessage_to", [None, ""])
def test_deliver_imessage_is_logged_noop_when_target_unset(imessage_to: Any) -> None:
    """An unset target is a logged no-op: returns False, runner NOT called.

    WHY: this encodes the opt-in intent — Orbit must NEVER message without a configured
    target. If this regresses to a send (or a raise), the bare CLI run would either spam
    or crash. The runner-not-called assertion is the security-relevant one.
    """
    runner, calls = _make_recording_runner(returncode=0)

    result = deliver.deliver_imessage("Orbit: 3 new items", "/tmp/orbit-test/today.html", imessage_to, runner=runner)

    assert result is False
    assert calls == [], "no subprocess (and thus no message) may fire when the target is unset"


# --- AppleScript escaping (injection safety) ------------------------------------


def test_deliver_imessage_escapes_double_quotes_in_summary() -> None:
    """A double-quote in the TL;DR is escaped in the built script (no injection/break).

    WHY: a TL;DR containing a raw double-quote would otherwise terminate the AppleScript
    string literal early and could inject arbitrary AppleScript. The escaping test fails
    if that defense is dropped — a code-injection regression, not cosmetic.
    """
    runner, calls = _make_recording_runner(returncode=0)
    # A title containing a double-quote — the injection vector.
    summary = 'He said "ship it" today'

    deliver.deliver_imessage(summary, "/tmp/orbit-test/today.html", "+15551234567", runner=runner)

    script = calls[0][2]
    # The raw unescaped sequence must NOT appear; the escaped form must.
    assert '"ship it"' not in script, "a raw double-quote must not survive into the script literal"
    assert '\\"ship it\\"' in script, "the double-quote must be backslash-escaped for AppleScript"


def test_escape_for_applescript_handles_backslash_before_quote() -> None:
    """Backslashes are escaped before quotes so the quote-escaping is not corrupted.

    WHY: order matters — escaping quotes first then backslashes (or vice versa wrong)
    would double the backslash we add for a quote and corrupt the literal. This pins the
    correct order at the unit level.
    """
    escaped = deliver._escape_for_applescript('a\\b"c')
    # backslash -> \\\\ , quote -> \\"  => a\\\\b\\"c
    assert escaped == 'a\\\\b\\"c'


# --- deliver_whatsapp: skip + fail-loud -----------------------------------------


def test_deliver_whatsapp_skipped_when_target_none() -> None:
    """A null WhatsApp target is a no-op (returns False); the HTTP boundary is NOT called.

    WHY: WhatsApp is a gated stretch — opt-in like iMessage. An unset target must skip
    cleanly, never raise and never POST.
    """
    posted: list[Any] = []

    def http_post(url: str, data: Mapping[str, str], auth: tuple[str, str]) -> int:
        posted.append((url, data, auth))
        return 201

    result = deliver.deliver_whatsapp("Orbit: 3 new items", "/tmp/x.html", None, env={}, http_post=http_post)

    assert result is False
    assert posted == [], "no HTTP may fire when the WhatsApp target is unset"


def test_deliver_whatsapp_raises_when_target_set_without_credential() -> None:
    """A configured target with NO Twilio credential raises (fail loud, Rule 12).

    WHY: a silent skip would make the user believe WhatsApp delivery is on when it is
    not. Orbit must fail loud with an actionable message pointing at .env, and must NOT
    POST. The credential value is never logged (asserted indirectly: no cred exists).
    """
    posted: list[Any] = []

    def http_post(url: str, data: Mapping[str, str], auth: tuple[str, str]) -> int:
        posted.append((url, data, auth))
        return 201

    with pytest.raises(RuntimeError, match="Twilio credentials"):
        deliver.deliver_whatsapp(
            "Orbit: 3 new items",
            "/tmp/x.html",
            "whatsapp:+15551234567",
            env={},  # no Twilio creds present
            http_post=http_post,
        )

    assert posted == [], "no HTTP may fire when the credential is missing"


def test_deliver_whatsapp_posts_when_target_and_credential_present() -> None:
    """With a target + creds, it POSTs via the injected boundary and returns True on 2xx.

    WHY: confirms the stretch send path reads creds from the injected env ONLY (not the
    process env) and routes through the mockable HTTP boundary — no live Twilio call.
    """
    posted: list[Any] = []

    def http_post(url: str, data: Mapping[str, str], auth: tuple[str, str]) -> int:
        posted.append((url, data, auth))
        return 201

    env = {
        "TWILIO_ACCOUNT_SID": "AC_test_sid",
        "TWILIO_AUTH_TOKEN": "test_token_value",
        "TWILIO_WHATSAPP_FROM": "whatsapp:+15550000000",
    }

    result = deliver.deliver_whatsapp(
        "Orbit: 3 new items",
        "/tmp/x.html",
        "whatsapp:+15551234567",
        env=env,
        http_post=http_post,
    )

    assert result is True
    assert len(posted) == 1
    url, data, auth = posted[0]
    assert "AC_test_sid" in url, "the Twilio URL must carry the account SID from env"
    assert data["To"] == "whatsapp:+15551234567"
    assert auth == ("AC_test_sid", "test_token_value"), "creds must come from the injected env"


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


# --- orbit.py delivery seam (bare path no-op) -----------------------------------


def test_orbit_delivery_seam_is_noop_on_bare_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_stage7_deliver calls deliver_imessage as a no-op when imessage_to is unset.

    WHY: the bare CLI run (empty items, no configured target) must stay a clean exit-0
    no-op — never sending. This pins the orbit.py seam to the opt-in skip and confirms
    no Briefcast/WhatsApp fires without its config key.
    """
    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    imessage_calls: list[Any] = []

    def fake_deliver_imessage(summary: str, html_path: Any, imessage_to: Any, **_: Any) -> bool:
        imessage_calls.append((summary, imessage_to))
        return False

    # Patch the deliver functions the seam routes through so nothing real fires.
    monkeypatch.setattr(orbit.deliver, "deliver_imessage", fake_deliver_imessage)
    monkeypatch.setattr(
        orbit.deliver,
        "emit_briefcast_payload",
        lambda *a, **k: pytest.fail("Briefcast must not fire without delivery.briefcast_path"),
    )

    config = OrbitConfig(delivery={})  # no imessage_to, no briefcast_path

    orbit.run_stage7_deliver([], [], config, Path("/tmp/orbit-test/today.html"))

    assert len(imessage_calls) == 1, "the seam must always route through deliver_imessage"
    assert imessage_calls[0][1] is None, "the bare path passes imessage_to=None (opt-in skip)"
