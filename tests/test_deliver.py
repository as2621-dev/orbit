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
import smtplib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

    WHY: with no email recipient and no briefcast_path configured, the delivery stage is a
    clean no-op that still lets the pipeline finish. This pins that: a bare config must NOT
    fire Briefcast or an email send, and run_stage7_deliver must return cleanly, so
    run_pipeline stays green end-to-end.
    """
    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    monkeypatch.setattr(
        orbit.deliver,
        "emit_briefcast_payload",
        lambda *a, **k: pytest.fail("Briefcast must not fire without delivery.briefcast_path"),
    )

    config = OrbitConfig(delivery={})  # no briefcast_path, no email_to — the bare path

    result = orbit.run_stage7_deliver([], [], [], config)

    assert result is None, "the delivery seam is a clean no-op on the bare path"


# --- deliver_email (M5: the email send path + its failure posture) --------------
#
# The SMTP boundary is injected exactly like the pipeline's other side-effectful seams
# (crontab_runner, llm_classifier, the HTML writer): a keyword-only ``transport`` with a
# real ``smtplib.SMTP_SSL`` default, so these tests fake the transport and NEVER the
# message-building logic (stack-notes §email). No test here opens a socket.


class _RecordingSmtp:
    """Fake ``smtplib.SMTP_SSL`` factory + connection — records attempts, logins, sends.

    Each factory call ``(host, port)`` is ONE send attempt and returns a context-manager
    connection (itself). Configurable to raise on ``login`` (auth rejection) or on
    ``send_message`` (transient failure), so the retry / no-retry posture is pinned by the
    ``attempts`` count rather than by faking any message logic.
    """

    def __init__(
        self,
        *,
        login_error: Exception | None = None,
        send_error: Exception | None = None,
        send_fail_times: int = -1,
    ) -> None:
        self.attempts = 0
        self.logins: list[tuple[str, str]] = []
        self.sent_messages: list[Any] = []
        self._login_error = login_error
        self._send_error = send_error
        # -1 => every send raises ``send_error``; N => the first N sends raise, then succeed.
        self._send_fail_times = send_fail_times
        self._send_failures = 0

    def __call__(self, host: str, port: int) -> "_RecordingSmtp":
        self.attempts += 1
        return self

    def __enter__(self) -> "_RecordingSmtp":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def login(self, username: str, password: str) -> None:
        self.logins.append((username, password))
        if self._login_error is not None:
            raise self._login_error

    def send_message(self, message: Any) -> None:
        should_fail = self._send_error is not None and (
            self._send_fail_times < 0 or self._send_failures < self._send_fail_times
        )
        if should_fail:
            self._send_failures += 1
            raise self._send_error  # type: ignore[misc]  (guarded non-None above)
        self.sent_messages.append(message)


_APP_PASSWORD = "topsecretapppw1234"
_ENV_OK: dict[str, str] = {"ORBIT_EMAIL_FROM": "sender@gmail.com", "GMAIL_APP_PASSWORD": _APP_PASSWORD}


def _write_page(path: Path, html: str) -> Path:
    """Write an HTML page to disk (bytes) so the attachment test can compare on-disk bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(html.encode("utf-8"))
    return path


def test_deliver_email_sends_one_message_with_body_and_attachment(tmp_path: Path) -> None:
    """Happy path: one message, right envelope, TL;DR body, page 1 attached VERBATIM.

    WHY: this is the product claim (PRD stories #1-3) — a single email, addressed from
    ORBIT_EMAIL_FROM to delivery.email_to, whose body is the delivery TL;DR and whose lone
    attachment is byte-identical to the rendered file on disk (so it opens in a browser
    exactly as rendered). Asserting the attachment BYTES, not just its presence, is the
    point: a re-serialized/HTML-mangled attachment would silently break rendering.
    """
    page_1 = _write_page(tmp_path / "today.html", "<!DOCTYPE html><html>page one ☕</html>")
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email(
        "Orbit: 2 new items — top: Big News",
        [page_1],
        "you@example.com",
        env=_ENV_OK,
        transport=smtp,
    )

    assert sent is True
    assert smtp.attempts == 1, "exactly one send on the happy path"
    assert len(smtp.sent_messages) == 1
    message = smtp.sent_messages[0]
    assert message["To"] == "you@example.com"
    assert message["From"] == "sender@gmail.com"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Orbit: 2 new items — top: Big News" in body, "the delivery TL;DR is the body"
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1, "single-page digest attaches exactly one file"
    assert attachments[0].get_filename() == "today.html"
    assert attachments[0].get_payload(decode=True) == page_1.read_bytes(), "attachment bytes match disk"
    assert smtp.logins == [("sender@gmail.com", _APP_PASSWORD)], "login uses ORBIT_EMAIL_FROM + app password"


def test_deliver_email_attaches_page_two_when_present(tmp_path: Path) -> None:
    """Both rendered pages ride one email, page 1 first, each byte-identical to disk.

    WHY: PRD story #4 — when the render spilled to a second page, the overflow must not be
    stranded on the origin machine. Pin that both pages attach to the SAME email in order.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>one</html>")
    page_2 = _write_page(tmp_path / "today-page2.html", "<html>two</html>")
    smtp = _RecordingSmtp()

    deliver.deliver_email("Orbit: many items", [page_1, page_2], "you@example.com", env=_ENV_OK, transport=smtp)

    attachments = list(smtp.sent_messages[0].iter_attachments())
    assert [a.get_filename() for a in attachments] == ["today.html", "today-page2.html"], "page 1 first"
    assert attachments[0].get_payload(decode=True) == page_1.read_bytes()
    assert attachments[1].get_payload(decode=True) == page_2.read_bytes()


@pytest.mark.parametrize(
    ("env", "email_to"),
    [
        ({"ORBIT_EMAIL_FROM": "sender@gmail.com", "GMAIL_APP_PASSWORD": _APP_PASSWORD}, ""),  # no recipient
        ({"ORBIT_EMAIL_FROM": "sender@gmail.com"}, "you@example.com"),  # no app password
        ({"GMAIL_APP_PASSWORD": _APP_PASSWORD}, "you@example.com"),  # no sender
    ],
)
def test_deliver_email_skips_when_config_incomplete(
    env: dict[str, str], email_to: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing recipient/sender/app-password => a clear structured SKIP, never a crash.

    WHY: delivery is opt-in (PRD story #5/#6). An unconfigured recipient or credential must
    skip with a clear log so the pipeline completes normally — not raise, not touch SMTP.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email("Orbit: 1 new item", [page_1], email_to, env=env, transport=smtp)

    assert sent is False, "incomplete config skips"
    assert smtp.attempts == 0, "no SMTP attempt when config is incomplete"
    assert "email_delivery_skipped" in capsys.readouterr().out, "the skip is a clear structured log"


def test_deliver_email_auth_rejection_does_not_retry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An auth rejection surfaces the real remedy and does NOT retry (PRD story #6).

    WHY: a wrong/absent app password will fail identically on every attempt — retrying
    hammers Gmail and can trip account protections. Pin ONE attempt and a log that names
    the actual fix (app password / 2FA), not a generic error.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    auth_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    smtp = _RecordingSmtp(login_error=auth_error)

    sent = deliver.deliver_email("Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is False
    assert smtp.attempts == 1, "auth rejection must NOT trigger a retry storm"
    out = capsys.readouterr().out
    assert "email_delivery_auth_failed" in out
    assert "app password" in out.lower(), "the log names the app-password remedy"
    assert "2fa" in out.lower() or "2-step" in out.lower(), "the log names the 2FA prerequisite"
    assert "fix_suggestion" in out


def test_deliver_email_transient_failure_retries_exactly_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A transient send failure retries EXACTLY once, then surfaces (PRD story #7).

    WHY: one retry rides out a blip without becoming a retry storm. Pin the count at 2 — a
    regression to 1 (no resilience) or 3+ (storm) both fail here.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    transient = smtplib.SMTPServerDisconnected("connection dropped mid-send")
    smtp = _RecordingSmtp(send_error=transient)  # every send fails

    sent = deliver.deliver_email("Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is False
    assert smtp.attempts == 2, "a transient failure retries exactly once — 2 attempts, not 1 or 3"
    assert "email_delivery_failed" in capsys.readouterr().out


def test_deliver_email_retry_succeeds_on_second_attempt(tmp_path: Path) -> None:
    """The single retry can recover: first send fails transiently, the second succeeds.

    WHY: the retry is not cosmetic — a real blip that clears on the second try must deliver.
    Pin that a first-attempt transient failure followed by success sends exactly once.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    transient = smtplib.SMTPServerDisconnected("blip")
    smtp = _RecordingSmtp(send_error=transient, send_fail_times=1)  # fail once, then succeed

    sent = deliver.deliver_email("Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is True
    assert smtp.attempts == 2
    assert len(smtp.sent_messages) == 1


def test_deliver_email_refuses_oversized_attachments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attachments over Gmail's 25MB cap are refused BEFORE any send, with an actionable log.

    WHY: an over-cap message is rejected by Gmail with an opaque SMTP error. Guard it at the
    source (a clear log) rather than surfacing a silent SMTP rejection. Shrink the cap so
    the test needn't write 25MB.
    """
    monkeypatch.setattr(deliver, "GMAIL_ATTACHMENT_LIMIT_BYTES", 1024)
    page_1 = _write_page(tmp_path / "today.html", "x" * 5000)  # 5000 bytes > 1024 cap
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email("Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is False
    assert smtp.attempts == 0, "over-cap attachments are refused before any SMTP send"
    out = capsys.readouterr().out
    assert "email_delivery_attachment_too_large" in out
    assert "fix_suggestion" in out


def test_deliver_email_never_leaks_credential_in_logs_or_headers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app password never appears in a log line, an exception path, or an email header.

    WHY: this is a hard security rule (brief §4/§8.5) and an acceptance criterion. Drive
    BOTH the success path and the auth-failure path (where a naive impl would echo the
    credential) and assert the secret string is absent from all captured log output and
    from the sent message.
    """
    secret = _APP_PASSWORD  # the fake credential the send must never echo
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")

    ok_smtp = _RecordingSmtp()
    deliver.deliver_email("Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=ok_smtp)

    auth_error = smtplib.SMTPAuthenticationError(535, b"bad creds")
    deliver.deliver_email(
        "Orbit: 1 new item", [page_1], "you@example.com", env=_ENV_OK, transport=_RecordingSmtp(login_error=auth_error)
    )

    out = capsys.readouterr().out
    assert secret not in out, "the app password must never appear in any log line"
    assert secret not in ok_smtp.sent_messages[0].as_string(), "the app password must never appear in the email"


def test_deliver_email_sanitizes_crlf_in_subject(tmp_path: Path) -> None:
    """A summary with an embedded CR/LF (from an external title) still sends, no injection.

    WHY: the Subject is derived from creator titles; ``EmailMessage`` raises on a CR/LF in a
    header, which would crash the pipeline mid-build AND, without that guard, let a crafted
    title smuggle a spoofed header. Pin that a newline-bearing summary sends exactly one
    message with a single-line Subject and no injected header.
    """
    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email(
        "Orbit: 1 new item\r\nBcc: attacker@evil.com",
        [page_1],
        "you@example.com",
        env=_ENV_OK,
        transport=smtp,
    )

    assert sent is True, "a newline in the title must not crash the send"
    message = smtp.sent_messages[0]
    assert "\n" not in message["Subject"] and "\r" not in message["Subject"], "Subject is a single line"
    # The payload text may echo "Bcc:" harmlessly in the plain-text body; what must NOT exist
    # is an actual injected Bcc *header*.
    assert message["Bcc"] is None, "no header smuggled in via the title"


def test_deliver_email_unreadable_page_is_nonfatal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A page that cannot be read is a loud, NON-FATAL refusal — never an uncaught crash.

    WHY (acceptance criterion #9): reading the attachment bytes happens before the send, so
    a vanished/unreadable render file (an ``OSError``) must not escape ``deliver_email`` and
    crash the pipeline. Pin that a missing page returns False with a structured error and no
    SMTP attempt.
    """
    missing_page = tmp_path / "gone.html"  # deliberately never written
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email("Orbit: 1 new item", [missing_page], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is False
    assert smtp.attempts == 0, "an unreadable page must not reach the SMTP send"
    assert "email_delivery_build_failed" in capsys.readouterr().out


def test_deliver_email_skips_when_no_rendered_pages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No rendered pages => skip, not a contentless 'digest attached' email.

    WHY: the body promises an attached digest; sending it with zero attachments would be a
    self-contradicting email. Pin that an empty page list skips cleanly with no SMTP touch.
    """
    smtp = _RecordingSmtp()

    sent = deliver.deliver_email("Orbit: 1 new item", [], "you@example.com", env=_ENV_OK, transport=smtp)

    assert sent is False
    assert smtp.attempts == 0
    assert "email_delivery_skipped" in capsys.readouterr().out


def test_default_transport_carries_a_socket_timeout() -> None:
    """The real default SMTP transport is bound to a timeout so an unattended run can't hang.

    WHY: with no timeout, ``smtplib.SMTP_SSL`` blocks forever on a network black-hole —
    neither loud nor non-fatal, and invisible to the faked-transport tests. Pin that the
    default carries a finite timeout so a stall surfaces as a transient error.
    """
    assert deliver._DEFAULT_SMTP_TRANSPORT.func is smtplib.SMTP_SSL
    assert deliver._DEFAULT_SMTP_TRANSPORT.keywords["timeout"] == deliver.SMTP_TIMEOUT_SECONDS
    assert deliver.SMTP_TIMEOUT_SECONDS > 0


# --- run_stage7_deliver wiring (the pipeline seam calls deliver_email) -----------


def test_stage7_deliver_emails_both_rendered_pages(tmp_path: Path) -> None:
    """run_stage7_deliver hands the FULL written_paths list to deliver_email.

    WHY: the render stage returns [page1, page2]; the deliver stage must forward the whole
    list so page 2 is not stranded. This pins the wiring end-to-end through the real stage
    over a faked transport, asserting both rendered pages reach the one email in order.
    """
    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    page_1 = _write_page(tmp_path / "today.html", "<html>one</html>")
    page_2 = _write_page(tmp_path / "today-page2.html", "<html>two</html>")
    config = OrbitConfig(delivery={"email_to": "you@example.com"})
    smtp = _RecordingSmtp()

    orbit.run_stage7_deliver([], [], [page_1, page_2], config, transport=smtp, env=_ENV_OK)

    assert len(smtp.sent_messages) == 1, "exactly one email carries both pages"
    filenames = [a.get_filename() for a in smtp.sent_messages[0].iter_attachments()]
    assert filenames == ["today.html", "today-page2.html"], "both pages attached, page 1 first"


def test_stage7_deliver_failure_is_nonfatal_and_leaves_seen_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing transport does not crash the delivery stage and never touches the store.

    WHY (acceptance criterion): a bad SMTP day must not un-mark items or crash the pipeline,
    else yesterday's items re-send tomorrow. seen is written in Stage 1, long before delivery.
    We spy on ``orbit.store`` and drive a persistently-failing transport: the stage must (a)
    not raise (loud but non-fatal), and (b) make ZERO store calls — so a send failure can
    never revise seen state. A regression that wrapped delivery around a store rollback fails
    here.
    """
    from unittest.mock import MagicMock

    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    store_spy = MagicMock()
    monkeypatch.setattr(orbit, "store", store_spy)

    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    config = OrbitConfig(delivery={"email_to": "you@example.com"})
    failing_smtp = _RecordingSmtp(send_error=smtplib.SMTPServerDisconnected("down all day"))

    # Must not raise — delivery failure is loud but non-fatal.
    result = orbit.run_stage7_deliver([], [], [page_1], config, transport=failing_smtp, env=_ENV_OK)

    assert result is None
    assert failing_smtp.attempts == 2, "the transient failure retried once, then surfaced"
    assert store_spy.mock_calls == [], "delivery must make no store calls — seen state stands on failure"


def test_stage7_deliver_skips_cleanly_without_recipient(tmp_path: Path) -> None:
    """With no delivery.email_to, run_stage7_deliver skips the send and returns cleanly.

    WHY: delivery is opt-in; a YouTube-only/unconfigured user's pipeline must finish without
    an email and without touching SMTP, even with sender+password present in the env.
    """
    import orbit  # imported here so the sys.path insert above is in effect
    from lib.config import OrbitConfig

    page_1 = _write_page(tmp_path / "today.html", "<html>x</html>")
    config = OrbitConfig(delivery={})  # no email_to
    smtp = _RecordingSmtp()

    result = orbit.run_stage7_deliver([], [], [page_1], config, transport=smtp, env=_ENV_OK)

    assert result is None
    assert smtp.attempts == 0, "no recipient => no SMTP send"
