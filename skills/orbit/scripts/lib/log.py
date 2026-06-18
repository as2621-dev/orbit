"""Structured JSON logging for Orbit.

Emits one JSON object per line to stdout (an event stream a human or an LLM can
parse). Every log entry carries an ``event`` (snake_case name), a ``level``, and a
UTC ``timestamp``, plus any contextual fields passed as keyword arguments. Error
logs additionally carry a ``fix_suggestion`` to make debugging actionable.

Security (hard rule, brief §4/§8.5 + conventions.md §Logging): cookie values,
``auth_token``, ``ct0``, and any credential-shaped field MUST NEVER reach the
output. All log functions route their fields through :func:`redact` before
serializing, replacing offending values with ``"[REDACTED]"``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

# Substrings that mark a field name as credential-bearing. Any field whose key
# contains one of these (case-insensitive) has its value redacted before logging.
# Keep this list conservative-but-broad: a false redaction is harmless; a leaked
# cookie is not.
REDACT_KEY_SUBSTRINGS: tuple[str, ...] = (
    "cookie",
    "cookies",
    "auth_token",
    "ct0",
    "token",
    "secret",
    "password",
    "credential",
)

REDACTED_PLACEHOLDER: str = "[REDACTED]"


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-bearing fields so secrets never reach the log stream.

    Any key whose lower-cased name contains one of :data:`REDACT_KEY_SUBSTRINGS`
    (e.g. ``cookie``, ``auth_token``, ``ct0``) has its value replaced with
    :data:`REDACTED_PLACEHOLDER`. Nested dictionaries are redacted recursively so
    a cookie nested inside a context object is caught too.

    Args:
        fields: Arbitrary contextual fields about to be logged.

    Returns:
        A new dict with the same keys; credential values replaced by ``"[REDACTED]"``.

    Example:
        >>> redact({"source": "youtube", "auth_token": "abc123"})
        {'source': 'youtube', 'auth_token': '[REDACTED]'}
    """
    redacted_fields: dict[str, Any] = {}
    for field_name, field_value in fields.items():
        field_name_lower = field_name.lower()
        if any(marker in field_name_lower for marker in REDACT_KEY_SUBSTRINGS):
            redacted_fields[field_name] = REDACTED_PLACEHOLDER
        elif isinstance(field_value, dict):
            redacted_fields[field_name] = redact(field_value)
        else:
            redacted_fields[field_name] = field_value
    return redacted_fields


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    """Serialize a single redacted log record as one JSON line on stdout.

    Args:
        level: Severity label (``info``, ``warning``, ``error``, ``debug``).
        event: snake_case event name describing what happened.
        fields: Contextual fields; routed through :func:`redact` before serial, so
            credential values never reach stdout.
    """
    record: dict[str, Any] = {
        "event": event,
        "level": level,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **redact(fields),
    }
    # default=str keeps non-JSON-native values (datetime, Path) from crashing the
    # logger — observability must not raise.
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def log_info(event: str, **fields: Any) -> None:
    """Emit an info-level structured event.

    Args:
        event: snake_case event name (e.g. ``loading_sources``).
        **fields: Arbitrary contextual fields (redacted before output).

    Example:
        >>> log_info("loading_sources", platform="youtube", count=42)
    """
    _emit("info", event, fields)


def log_warning(event: str, **fields: Any) -> None:
    """Emit a warning-level structured event.

    Args:
        event: snake_case event name (e.g. ``sources_cache_stale``).
        **fields: Arbitrary contextual fields (redacted before output).
    """
    _emit("warning", event, fields)


def log_error(event: str, *, fix_suggestion: str, **fields: Any) -> None:
    """Emit an error-level structured event carrying an actionable fix suggestion.

    Args:
        event: snake_case event name (e.g. ``classification_failed``).
        fix_suggestion: Concrete, human-actionable remediation step. Required —
            Orbit error logs must always tell the reader what to do next.
        **fields: Arbitrary contextual fields (redacted before output).

    Example:
        >>> log_error(
        ...     "youtube_auth_failed",
        ...     fix_suggestion="Log into YouTube in your browser, then re-run.",
        ...     platform="youtube",
        ... )
    """
    _emit("error", event, {"fix_suggestion": fix_suggestion, **fields})


def log_debug(event: str, **fields: Any) -> None:
    """Emit a debug-level structured event.

    Args:
        event: snake_case event name.
        **fields: Arbitrary contextual fields (redacted before output).
    """
    _emit("debug", event, fields)
