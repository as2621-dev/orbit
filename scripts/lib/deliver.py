"""Orbit Stage-7 delivery — the email send path + the shared body helper + Briefcast emit.

The pipeline renders the digest HTML locally; :func:`deliver_email` turns it into one sent
email — the delivery TL;DR as the body, the self-contained Tiles HTML page(s) as
attachments — over Gmail SMTP with an app password (PRD M5). iMessage and WhatsApp delivery
were DELETED, not deprecated (PRD story #8: one delivery path to configure, permission, and
debug) — email is the single network delivery surface.

What lives here:

  * :func:`deliver_email` — the send path AND its failure posture (skip on missing config,
    no-retry auth rejection, one-retry transient error, 25MB cap). The SMTP transport is an
    injected boundary (a ``smtplib.SMTP_SSL`` factory) so tests fake it, never the message
    logic.
  * :func:`build_message_body` — a PURE helper composing a one-line summary+link body
    (reserved for the M7 chat-link bridge; the email body is the TL;DR summary as-is).
  * :func:`emit_briefcast_payload` — OPTIONAL / STRETCH. Writes the TL;DR + episode list
    as a JSON Briefcast payload file (integrations §6). A file, not a live integration —
    NO auth surface.

Security (hard rule, brief §4/§8.5 + CLAUDE.md): the Gmail app password is read from the
environment, passed straight to SMTP ``login``, and NEVER hardcoded, logged, echoed in an
exception, or placed in an email header.
"""

from __future__ import annotations

import functools
import json
import os
import smtplib
import sys
from collections.abc import Mapping, Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as ``lib.deliver`` or run
# from the scripts dir directly. Mirrors config.py / store.py so ``from lib import
# log`` resolves in both cases.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)

# Gmail SMTP over implicit SSL (integrations §4 / stack-notes §email). Sender + app
# password come from ``.env`` (never from the JSON config); the recipient is
# ``delivery.email_to``.
GMAIL_SMTP_HOST: str = "smtp.gmail.com"
GMAIL_SMTP_SSL_PORT: int = 465
ORBIT_EMAIL_FROM_ENV_VAR: str = "ORBIT_EMAIL_FROM"
GMAIL_APP_PASSWORD_ENV_VAR: str = "GMAIL_APP_PASSWORD"

# Gmail refuses a message whose total size exceeds 25 MB. The check measures the ENCODED
# message (``message.as_bytes()``) — base64 inflates attachment bytes ~1.35x — so the guard
# reflects what Gmail actually sees. Tiles is ~318KB today, so this is a guard against a
# runaway render (e.g. many inlined images), not a live constraint (#2).
GMAIL_ATTACHMENT_LIMIT_BYTES: int = 25 * 1024 * 1024

# Socket timeout for the real SMTP connection. Without it, a network black-hole (dropped
# SYN / stalled TLS handshake) would block an unattended cron run forever — neither loud nor
# non-fatal. A timeout turns that into a ``TimeoutError`` (an ``OSError``) the retry posture
# handles like any transient failure.
SMTP_TIMEOUT_SECONDS: int = 30

# The injected SMTP boundary: a factory mirroring ``smtplib.SMTP_SSL(host, port)`` that
# returns a context-manager connection exposing ``login`` + ``send_message``. Tests fake
# this so no test opens a real socket (stack-notes: fake the transport, not the message
# logic). One factory call == one send attempt.
SmtpTransport = Callable[[str, int], Any]

# The real default transport: ``smtplib.SMTP_SSL`` bound to a connect/read timeout so a
# stalled network surfaces as a transient error instead of hanging the pipeline.
_DEFAULT_SMTP_TRANSPORT: SmtpTransport = functools.partial(smtplib.SMTP_SSL, timeout=SMTP_TIMEOUT_SECONDS)


def deliver_email(
    summary: str,
    html_pages: Sequence[Path],
    email_to: Optional[str],
    *,
    env: Mapping[str, str] = os.environ,
    transport: SmtpTransport = _DEFAULT_SMTP_TRANSPORT,
) -> bool:
    """Send the digest as ONE email: TL;DR body + the Tiles HTML page(s) as attachments.

    The morning digest lands in the user's inbox (PRD M5). The body is the delivery TL;DR
    (``summary``); each rendered page rides as a self-contained ``text/html`` attachment
    (page 1 first) so it opens in a real browser with fonts, thumbnails, and deep-links
    intact — Gmail never renders it inline. The sender + app password are read from ``env``
    (``.env``); the recipient is ``email_to`` (``delivery.email_to``).

    Failure posture (loud but never destructive — PRD story #7):

      * No recipient / sender / app password, or no rendered pages -> SKIP with a clear
        structured log, no exception, no SMTP touch. The pipeline completes normally.
      * A page that cannot be read, or an unbuildable message -> refuse with a structured
        error log rather than a crash.
      * Encoded message over :data:`GMAIL_ATTACHMENT_LIMIT_BYTES` -> refuse with an actionable
        log rather than a silent SMTP rejection.
      * Auth rejected -> log the real remedy (app password invalid / 2FA off) and do NOT
        retry (no retry storm).
      * Transient SMTP/connection error -> retry EXACTLY once, then surface.

    This never raises for a delivery failure and never touches ``seen`` state, so a bad SMTP
    day cannot crash the pipeline or re-send yesterday's items tomorrow.

    Args:
        summary: The delivery TL;DR (used as the email subject + body lead).
        html_pages: The rendered page paths in order (page 1 first); page 2 when present.
        email_to: The recipient (``config.delivery.email_to``); None/empty => skip.
        env: Environment mapping to read secrets from (defaults to ``os.environ``).
        transport: Injected ``smtplib.SMTP_SSL``-shaped factory (faked in tests).

    Returns:
        True only on a confirmed send; False on any skip / refusal / delivery failure.
    """
    email_from = (env.get(ORBIT_EMAIL_FROM_ENV_VAR) or "").strip()
    app_password = (env.get(GMAIL_APP_PASSWORD_ENV_VAR) or "").strip()
    recipient = (email_to or "").strip()

    # Reason: delivery is opt-in — an unconfigured recipient/credential is a SKIP, not an
    # error. ``missing_config`` lists field NAMES only, never the secret value.
    missing_config = [
        field_name
        for field_name, field_value in (
            ("delivery.email_to", recipient),
            (ORBIT_EMAIL_FROM_ENV_VAR, email_from),
            (GMAIL_APP_PASSWORD_ENV_VAR, app_password),
        )
        if not field_value
    ]
    if missing_config:
        log.log_info(
            "email_delivery_skipped",
            channel="email",
            reason="incomplete_delivery_config",
            missing_config=missing_config,
            detail="Set delivery.email_to plus ORBIT_EMAIL_FROM and GMAIL_APP_PASSWORD in "
            ".env to enable email delivery.",
        )
        return False

    if not html_pages:
        # Nothing rendered to attach — skip rather than send a body-only "digest attached"
        # email. Not reachable from the pipeline (render always writes page 1), but keeps
        # the module contract sound for any direct caller.
        log.log_info(
            "email_delivery_skipped",
            channel="email",
            reason="no_rendered_pages",
            detail="No rendered digest pages to attach; nothing to deliver.",
        )
        return False

    # Build inside a guard: a vanished/unreadable render file (OSError) or a malformed
    # From/To header (ValueError from EmailMessage) must be a loud, NON-FATAL failure — the
    # pipeline (and seen state) must survive it, per the module's "never raises" contract.
    try:
        message = _build_email_message(summary, html_pages, email_from=email_from, email_to=recipient)
    except (OSError, ValueError) as build_error:
        log.log_error(
            "email_delivery_build_failed",
            fix_suggestion="Could not assemble the digest email. Confirm the rendered pages "
            "still exist and ORBIT_EMAIL_FROM / delivery.email_to are valid addresses, then re-run.",
            channel="email",
            error_type=type(build_error).__name__,
        )
        return False

    # Check the ENCODED message size (what Gmail actually weighs) against the 25MB cap.
    message_bytes = len(message.as_bytes())
    if message_bytes > GMAIL_ATTACHMENT_LIMIT_BYTES:
        log.log_error(
            "email_delivery_attachment_too_large",
            fix_suggestion="The rendered digest exceeds Gmail's 25MB message cap. Lower "
            "'depth' or trim inlined images to shrink the digest, then re-run.",
            channel="email",
            message_bytes=message_bytes,
            limit_bytes=GMAIL_ATTACHMENT_LIMIT_BYTES,
            attachment_count=len(html_pages),
        )
        return False

    return _send_with_retry(
        message,
        username=email_from,
        password=app_password,
        recipient=recipient,
        attachment_count=len(html_pages),
        transport=transport,
    )


def _build_email_message(
    summary: str,
    html_pages: Sequence[Path],
    *,
    email_from: str,
    email_to: str,
) -> EmailMessage:
    """Assemble the delivery email: TL;DR body + each HTML page as a ``text/html`` attachment.

    Each page is attached as raw BYTES read from disk (not a re-serialized string) so the
    digest the user opens is byte-identical to the rendered file. The credential is never a
    header. Reads happen here (inside the caller's failure guard) so a vanished/unreadable
    page raises ``OSError`` the caller turns into a non-fatal, logged refusal.

    The ``summary`` feeds the Subject header but is derived from external creator titles, so
    its CR/LF are collapsed to spaces (:func:`_sanitize_header_value`): a title with an
    embedded newline would otherwise make ``EmailMessage`` raise mid-build — and, absent the
    stdlib guard, could smuggle a spoofed header. The full (unsanitized) summary still leads
    the body, where newlines are harmless.

    Args:
        summary: The delivery TL;DR (subject + body lead); a blank summary falls back.
        html_pages: The rendered page paths in send order (page 1 first).
        email_from: The ``From`` address (also the SMTP login username).
        email_to: The ``To`` recipient.

    Returns:
        A ready-to-send :class:`email.message.EmailMessage`.

    Raises:
        OSError: If a page file cannot be read.
        ValueError: If a header value (e.g. a malformed From/To) contains a CR/LF.
    """
    summary_line = summary.strip() or "Your Orbit digest is ready."
    message = EmailMessage()
    message["Subject"] = _sanitize_header_value(summary_line)
    message["From"] = email_from
    message["To"] = email_to
    message.set_content(f"{summary_line}\n\nYour full Orbit digest is attached — open it in any browser.")
    for page_path in html_pages:
        message.add_attachment(page_path.read_bytes(), maintype="text", subtype="html", filename=page_path.name)
    return message


def _sanitize_header_value(value: str) -> str:
    """Collapse CR/LF in a header value to single spaces (header-injection + crash guard).

    ``EmailMessage`` raises ``ValueError`` on a linefeed/carriage-return in a header value —
    which both blocks header injection and would crash the build if an external title
    carried a newline. Replacing CR/LF with spaces keeps the send resilient AND is
    defense-in-depth against a spoofed header slipping into the Subject.

    Args:
        value: The raw header value (may contain CR/LF from external content).

    Returns:
        The value with every ``\\r`` and ``\\n`` replaced by a single space.
    """
    return value.replace("\r", " ").replace("\n", " ")


def _send_once(
    message: EmailMessage,
    *,
    username: str,
    password: str,
    transport: SmtpTransport,
) -> None:
    """Perform ONE SMTP-SSL send attempt over the injected transport (one call == one attempt).

    Opens the transport (``smtplib.SMTP_SSL(host, port)`` by default), authenticates with the
    app password, and sends. The password is passed straight to ``login`` and never logged or
    stored. Raises on failure so :func:`_send_with_retry` owns the retry/no-retry policy.
    """
    with transport(GMAIL_SMTP_HOST, GMAIL_SMTP_SSL_PORT) as smtp_connection:
        smtp_connection.login(username, password)
        smtp_connection.send_message(message)


def _send_with_retry(
    message: EmailMessage,
    *,
    username: str,
    password: str,
    recipient: str,
    attachment_count: int,
    transport: SmtpTransport,
) -> bool:
    """Send ``message`` over the injected transport with Orbit's delivery failure posture.

    An auth rejection is terminal — surface the real remedy (app password invalid / 2FA off)
    and do NOT retry. A transient SMTP/connection error retries EXACTLY once, then surfaces.
    Delivery failure is loud (structured error log + ``fix_suggestion``) but never raises, so
    the caller's pipeline finishes and ``seen`` state stands. Returns True only on a send.

    Args:
        message: The assembled email to send.
        username: SMTP login username (the ``ORBIT_EMAIL_FROM`` sender).
        password: The Gmail app password (never logged).
        recipient: The recipient, for the success log only.
        attachment_count: Number of attached pages, for the success log only.
        transport: The injected SMTP factory.

    Returns:
        True on a confirmed send; False on auth rejection or a twice-failed transient send.
    """
    try:
        _send_once(message, username=username, password=password, transport=transport)
    except smtplib.SMTPAuthenticationError as auth_error:
        # Terminal — a wrong/absent app password will not fix itself on a retry.
        log.log_error(
            "email_delivery_auth_failed",
            fix_suggestion="Gmail rejected the app password. Confirm 2-Step Verification "
            "(2FA) is ON for the sender account, generate a fresh app password at "
            "https://myaccount.google.com/apppasswords, and set GMAIL_APP_PASSWORD in .env.",
            channel="email",
            smtp_code=auth_error.smtp_code,
            attempts=1,
        )
        return False
    except (smtplib.SMTPException, OSError) as transient_error:
        log.log_warning(
            "email_delivery_retrying",
            channel="email",
            error_type=type(transient_error).__name__,
            attempt=1,
        )
        try:
            _send_once(message, username=username, password=password, transport=transport)
        except (smtplib.SMTPException, OSError) as retry_error:
            log.log_error(
                "email_delivery_failed",
                fix_suggestion="The Gmail SMTP send failed twice. Check your network and "
                "https://www.google.com/appsstatus, then re-run — today's items stay marked "
                "seen, so nothing re-sends.",
                channel="email",
                error_type=type(retry_error).__name__,
                attempts=2,
            )
            return False

    log.log_info(
        "email_delivery_sent",
        channel="email",
        recipient=recipient,
        attachment_count=attachment_count,
    )
    return True


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
        return {
            "title": str(rankable.get("title", "")),
            "url": str(rankable.get("url") or rankable.get("card_url", "")),
        }

    title = getattr(rankable, "title", None)
    if title is None:
        return {"title": str(rankable), "url": ""}
    return {"title": str(title), "url": str(getattr(rankable, "card_url", "") or "")}
