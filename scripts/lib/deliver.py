"""Orbit Stage-7 delivery — iMessage (core) + optional WhatsApp / Briefcast (stretch).

The pipeline writes the digest HTML locally; this module *notifies* the user that
the digest is ready and links them to that local page (brief §3 Stage 7,
integrations §4-§6). Delivery is OPT-IN: nothing is sent unless the user has
configured a target in ``orbit.config.json``'s ``delivery`` block.

Three deterministic delivery functions live here (Rule 5 — routing is plain code,
no LLM):

  * :func:`deliver_imessage` — CORE. Sends a short message (one-line TL;DR + scoops
    + a link to the local HTML page) via macOS AppleScript (``osascript``). Uses the
    user's own logged-in Messages session — NO network credential leaves the machine
    (integrations §4). Triggered only when ``delivery.imessage_to`` is set.
  * :func:`deliver_whatsapp` — OPTIONAL/STRETCH. Twilio WhatsApp send, gated behind
    ``delivery.whatsapp_to`` + a Twilio credential read from ``.env`` (``os.environ``)
    ONLY (integrations §5). Fails LOUD (Rule 12) if a target is set but the credential
    is absent. Built minimally, not polished (phase scope: stretch, gated).
  * :func:`emit_briefcast_payload` — OPTIONAL/STRETCH. Writes the TL;DR + episode list
    as a JSON Briefcast payload file (integrations §6). No auth surface.

Security (hard rule, brief §4/§8.5 + CLAUDE.md): no credential is hardcoded, logged,
or transmitted. iMessage uses local ``osascript`` only. Twilio creds come from
``os.environ`` (``.env``) and are never logged. All external boundaries (the
``osascript`` subprocess, the WhatsApp HTTP POST) are INJECTABLE so tests mock them
and no real message / HTTP ever fires.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

# Make ``lib`` importable whether this module is imported as ``lib.deliver`` or run
# from the scripts dir directly. Mirrors config.py / store.py so ``from lib import
# log`` resolves in both cases.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)

# The Twilio env var Orbit reads for the WhatsApp stretch path. The credential VALUE
# is never read into a logged field — only its presence/absence is observed.
TWILIO_AUTH_TOKEN_ENV_VAR: str = "TWILIO_AUTH_TOKEN"
TWILIO_ACCOUNT_SID_ENV_VAR: str = "TWILIO_ACCOUNT_SID"
TWILIO_WHATSAPP_FROM_ENV_VAR: str = "TWILIO_WHATSAPP_FROM"

# How much of the message body we allow into a log field. The full TL;DR can be long;
# we log a short preview only (never the whole body) to keep logs tidy and avoid
# echoing large user content. The body itself still goes to the user via osascript.
_LOG_BODY_PREVIEW_CHARS: int = 80


# A subprocess runner is ``(argv) -> CompletedProcess-like``. The default wraps
# ``subprocess.run``; tests inject a fake so ``osascript`` never actually runs.
SubprocessRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# A WhatsApp HTTP poster is ``(url, data, auth) -> int`` returning an HTTP status code.
# The default is unset (None) — the stretch path requires the caller/test to inject a
# boundary, so this module never makes a live HTTP call by accident.
HttpPoster = Callable[[str, Mapping[str, str], tuple[str, str]], int]


def _default_subprocess_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run ``argv`` via :func:`subprocess.run`, capturing output (the real osascript boundary).

    Kept tiny and injectable so :func:`deliver_imessage` tests pass a fake runner and
    no real ``osascript`` (and thus no real iMessage) ever executes. ``check=False`` so
    a non-zero exit is surfaced by the caller as an actionable error, not a raised
    ``CalledProcessError`` that hides the fix.

    Args:
        argv: The command + args (e.g. ``["osascript", "-e", "<script>"]``).

    Returns:
        The completed process (``returncode``, ``stdout``, ``stderr``).
    """
    return subprocess.run(  # noqa: S603  (argv is built by us, not user shell input)
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )


def _escape_for_applescript(text: str) -> str:
    """Escape ``text`` so it is safe inside an AppleScript double-quoted string literal.

    AppleScript string literals are double-quoted; a literal backslash or double-quote
    inside the text would otherwise terminate the literal early or inject extra
    AppleScript. Order matters: escape backslashes FIRST (so the backslashes we add for
    quotes are not themselves doubled), then double-quotes. Newlines are normalized to
    spaces so a multi-line TL;DR cannot break the single ``set messageText to "..."``
    statement (AppleScript string literals do not span raw newlines).

    Args:
        text: Arbitrary user-facing text (TL;DR / scoops / link) to embed.

    Returns:
        The text with ``\\`` -> ``\\\\``, ``"`` -> ``\\"``, and newlines collapsed —
        injection-safe for a double-quoted AppleScript literal.

    Example:
        >>> _escape_for_applescript('say "hi"') == 'say \\\\"hi\\\\"'
        True
    """
    # Reason: backslash first — otherwise the backslash we prepend to each quote below
    # would itself get doubled, corrupting the escaping.
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace('"', '\\"')
    # Reason: AppleScript "..." literals cannot contain a raw newline; collapse so a
    # multi-line body stays one valid statement (and cannot smuggle in a new line of code).
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return escaped


def _build_html_link(html_path: Path | str) -> str:
    """Build a ``file://`` link to the local HTML digest page for the message body.

    The digest is a local file; a ``file://`` URI lets the recipient (the user, on
    their own machine) tap straight into it. The path is expanded (``~``) and resolved
    to an absolute path so the link is unambiguous regardless of the run's cwd.

    Args:
        html_path: The page-1 HTML path (may be ``~``-relative or relative).

    Returns:
        A ``file://`` URI string pointing at the absolute HTML path.
    """
    absolute_path = Path(html_path).expanduser().resolve()
    return absolute_path.as_uri()


def _build_imessage_applescript(message_body: str, recipient: str) -> str:
    """Build the AppleScript that sends ``message_body`` to ``recipient`` via Messages.

    Both the body and the recipient are escaped (:func:`_escape_for_applescript`) before
    interpolation so a TL;DR or handle containing a double-quote / backslash cannot
    break out of the string literal or inject extra AppleScript (injection-safe).

    Args:
        message_body: The full one-line message (TL;DR + scoops + link).
        recipient: The iMessage target (phone / handle) from ``delivery.imessage_to``.

    Returns:
        A complete AppleScript program string suitable for ``osascript -e <script>``.
    """
    safe_body = _escape_for_applescript(message_body)
    safe_recipient = _escape_for_applescript(recipient)
    # Reason: target the default Messages service; "buddy" addressing works for both
    # iMessage handles and phone numbers on a logged-in Messages app.
    return (
        'tell application "Messages"\n'
        f'    set targetBuddy to "{safe_recipient}"\n'
        f'    set messageText to "{safe_body}"\n'
        "    set targetService to 1st account whose service type = iMessage\n"
        "    send messageText to participant targetBuddy of targetService\n"
        "end tell"
    )


def build_message_body(summary: str, html_link: str) -> str:
    """Compose the short delivery body: the TL;DR summary + a link to the local page.

    A PURE helper (deterministic, no I/O — Rule 5) shared by every delivery channel so
    the iMessage / WhatsApp / Briefcast bodies stay consistent. The ``summary`` already
    folds in the TL;DR + scoops (the caller in ``orbit.py`` builds it from the tiered
    items / scoops). Kept to one line so it reads as a notification, not a wall of text.

    Args:
        summary: The one-line TL;DR (already includes any scoops prefix).
        html_link: The ``file://`` link to the local HTML digest.

    Returns:
        The composed message body string.

    Example:
        >>> build_message_body("3 new items", "file:///tmp/today.html")
        '3 new items — file:///tmp/today.html'
    """
    summary_text = summary.strip() or "Your Orbit digest is ready."
    return f"{summary_text} — {html_link}"


def deliver_imessage(
    summary: str,
    html_path: Path | str,
    imessage_to: Optional[str],
    *,
    runner: SubprocessRunner = _default_subprocess_runner,
) -> bool:
    """Deliver the digest TL;DR via iMessage (AppleScript), opt-in on ``imessage_to``.

    CORE delivery (integrations §4). Behavior:

      * ``imessage_to`` falsy/None -> NO-OP: logs ``imessage_skipped`` and returns
        ``False``. Opt-in intent — Orbit NEVER messages without a configured target.
      * ``imessage_to`` set -> builds an injection-safe AppleScript (TL;DR + scoops +
        a ``file://`` link to the local HTML page) and runs it via ``osascript -e``.
        The subprocess boundary is the injectable ``runner`` so tests mock ``osascript``
        and no real message is sent. A non-zero ``osascript`` exit logs an error with a
        ``fix_suggestion`` (grant Automation permission) and returns ``False``.

    No network credential leaves the machine — Messages uses the user's own logged-in
    session (integrations §4). The recipient is user config (a phone/handle); logging is
    kept minimal (a short body preview only, never the full body, never a credential).

    Args:
        summary: The one-line TL;DR (already folds in scoops) the caller built.
        html_path: The page-1 HTML path (``~``/relative ok; resolved to ``file://``).
        imessage_to: The iMessage target, or None/empty to skip (opt-in).
        runner: Injectable subprocess runner; defaults to a thin ``subprocess.run``
            wrapper. Tests pass a fake so ``osascript`` never actually runs.

    Returns:
        ``True`` on a successful send; ``False`` on the skip path or an osascript failure.
    """
    if not imessage_to:
        # Reason: opt-in — never message without a configured target. Logged, not silent.
        log.log_info(
            "imessage_skipped",
            channel="imessage",
            detail="delivery.imessage_to not set; skipping iMessage delivery (opt-in).",
        )
        return False

    html_link = _build_html_link(html_path)
    message_body = build_message_body(summary, html_link)
    applescript = _build_imessage_applescript(message_body, imessage_to)

    log.log_info(
        "imessage_send_started",
        channel="imessage",
        html_link=html_link,
        body_preview=message_body[:_LOG_BODY_PREVIEW_CHARS],
    )

    try:
        completed = runner(["osascript", "-e", applescript])
    except OSError as exc:
        # Reason: osascript missing (non-macOS) or not executable — fail loud, actionable.
        log.log_error(
            "imessage_send_failed",
            fix_suggestion=(
                "Could not invoke 'osascript'. iMessage delivery requires macOS. Run Orbit "
                "on a Mac with the Messages app signed in, or unset delivery.imessage_to."
            ),
            channel="imessage",
            error_type=type(exc).__name__,
        )
        return False

    if completed.returncode != 0:
        log.log_error(
            "imessage_send_failed",
            fix_suggestion=(
                "osascript exited non-zero. Grant Automation permission to the controlling "
                "app in System Settings > Privacy & Security > Automation (allow it to control "
                "Messages), and confirm the Messages app is signed in."
            ),
            channel="imessage",
            return_code=completed.returncode,
            stderr=(completed.stderr or "")[:_LOG_BODY_PREVIEW_CHARS],
        )
        return False

    log.log_info("imessage_send_completed", channel="imessage")
    return True


def deliver_whatsapp(
    summary: str,
    html_path: Path | str,
    whatsapp_to: Optional[str],
    *,
    env: Mapping[str, str] = os.environ,
    http_post: Optional[HttpPoster] = None,
) -> bool:
    """Deliver the digest TL;DR via WhatsApp (Twilio) — OPTIONAL / STRETCH, gated.

    Stretch path (integrations §5; built minimally, not polished). Behavior:

      * ``whatsapp_to`` None/empty -> NO-OP: logs ``whatsapp_skipped`` and returns
        ``False`` (opt-in, mirrors iMessage).
      * ``whatsapp_to`` set but the Twilio credential is ABSENT from ``env`` -> raises
        :class:`RuntimeError` (fail loud, Rule 12) with a ``fix_suggestion`` telling the
        user to set the cred in ``.env``. The credential VALUE is never logged.
      * ``whatsapp_to`` set + cred present -> POSTs via the INJECTABLE ``http_post``
        boundary (tests mock it; no live HTTP). Credentials are read from ``env`` ONLY.

    Security: the Twilio auth token / account SID are read from ``env`` (``.env``) only
    and are NEVER logged or hardcoded. ``http_post`` receives the auth tuple but this
    module logs only the presence of the cred, never its value.

    Args:
        summary: The one-line TL;DR (already folds in scoops).
        html_path: The page-1 HTML path (resolved to a ``file://`` link in the body).
        whatsapp_to: The WhatsApp target (e.g. ``"whatsapp:+15551234567"``), or None to skip.
        env: The environment mapping to read Twilio creds from; defaults to
            ``os.environ``. Injectable so tests set/unset creds without touching the process env.
        http_post: Injectable HTTP poster ``(url, data, auth) -> status_code``. Required
            when actually sending; tests mock it so no real HTTP fires.

    Returns:
        ``True`` on a 2xx send; ``False`` on the skip path or a non-2xx response.

    Raises:
        RuntimeError: When ``whatsapp_to`` is set but the Twilio credential is absent
            from ``env`` (fail loud), or when ``http_post`` was not injected.
    """
    if not whatsapp_to:
        log.log_info(
            "whatsapp_skipped",
            channel="whatsapp",
            detail="delivery.whatsapp_to not set; skipping WhatsApp delivery (opt-in).",
        )
        return False

    account_sid = env.get(TWILIO_ACCOUNT_SID_ENV_VAR)
    auth_token = env.get(TWILIO_AUTH_TOKEN_ENV_VAR)
    whatsapp_from = env.get(TWILIO_WHATSAPP_FROM_ENV_VAR)

    # Reason: fail loud (Rule 12) — a configured WhatsApp target with no credential is a
    # misconfiguration, not a silent skip. Never log the credential VALUE (only presence).
    if not account_sid or not auth_token or not whatsapp_from:
        fix_suggestion = (
            f"WhatsApp delivery needs Twilio credentials in .env: {TWILIO_ACCOUNT_SID_ENV_VAR}, "
            f"{TWILIO_AUTH_TOKEN_ENV_VAR}, and {TWILIO_WHATSAPP_FROM_ENV_VAR}. Set them in .env "
            "(never commit real values), or remove delivery.whatsapp_to to disable WhatsApp."
        )
        log.log_error(
            "whatsapp_credential_missing",
            fix_suggestion=fix_suggestion,
            channel="whatsapp",
            has_account_sid=bool(account_sid),
            has_auth_token=bool(auth_token),
            has_whatsapp_from=bool(whatsapp_from),
        )
        raise RuntimeError(
            "WhatsApp delivery requested (delivery.whatsapp_to set) but Twilio credentials "
            "are missing from .env. " + fix_suggestion
        )

    if http_post is None:
        # Reason: this module must never make a live HTTP call by accident — the caller/test
        # injects the boundary. Fail loud rather than silently no-op a configured target.
        raise RuntimeError(
            "deliver_whatsapp requires an injected http_post boundary to send. "
            "Pass http_post=... (the host wires the real HTTP client; tests mock it)."
        )

    html_link = _build_html_link(html_path)
    message_body = build_message_body(summary, html_link)
    twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    post_data: dict[str, str] = {
        "From": whatsapp_from,
        "To": whatsapp_to,
        "Body": message_body,
    }

    log.log_info(
        "whatsapp_send_started",
        channel="whatsapp",
        html_link=html_link,
        body_preview=message_body[:_LOG_BODY_PREVIEW_CHARS],
    )

    status_code = http_post(twilio_url, post_data, (account_sid, auth_token))
    if not 200 <= status_code < 300:
        log.log_error(
            "whatsapp_send_failed",
            fix_suggestion=(
                "Twilio returned a non-2xx status. Verify the Twilio credentials, the "
                "WhatsApp 'From' sender, and the 'To' number format (whatsapp:+<E164>)."
            ),
            channel="whatsapp",
            status_code=status_code,
        )
        return False

    log.log_info("whatsapp_send_completed", channel="whatsapp", status_code=status_code)
    return True


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
