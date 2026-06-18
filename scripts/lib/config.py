"""Orbit configuration loader — `orbit.config.json` into a typed `OrbitConfig`.

Non-secret per-user config lives in ``orbit.config.json`` (brief §6,
``reference/api-contracts.md``): which browser cookies come from, creator priority
weights, the interest profile, the depth throttle, delivery targets, and the cron
schedule. Secrets (explicit cookies, Twilio creds) live in ``.env`` and are NEVER
read here — when ``cookie_source == "env"`` this module only notes that the source
loader will read ``AUTH_TOKEN``/``CT0`` from ``.env`` at runtime; it does not touch
them.

This module is stdlib-only (pydantic is NOT an Orbit dependency — see
``conventions.md`` / ``pyproject.toml`` ``dependencies = []``). Validation fails
LOUD (Rule 12): an out-of-range ``cookie_source`` or ``depth`` raises
:class:`ConfigError` naming the bad field, the bad value, and the allowed set, so a
typo in the config never silently defaults.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.config`` or run from the scripts dir directly. Mirrors store.py /
# youtube_yt.py so ``from lib import log`` resolves in both cases.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)

# Allowed enums, lifted from reference/api-contracts.md. Kept as constants so the
# validation error message can name the allowed set verbatim.
ALLOWED_COOKIE_SOURCES: tuple[str, ...] = ("chrome", "firefox", "safari", "edge", "brave", "env")
ALLOWED_DEPTHS: tuple[str, ...] = ("quick", "default", "deep")

# A standard 5-field cron expression has exactly this many whitespace-separated fields:
# minute hour day-of-month month day-of-week. Orbit's scheduler (README §8.3 / the
# --setup wizard) emits this classic 5-field form, so the validator rejects any other
# field count loud rather than letting a malformed schedule reach the user's crontab.
_CRON_FIELD_COUNT: int = 5

# Per-field cron token grammar (standard crontab tokens): a single ``*``; an integer;
# an inclusive range ``a-b``; a comma-list of either; and an optional step ``/n`` on a
# ``*`` or a range. This is a SYNTAX check (token shape), not a semantic range check —
# the wizard and OS cron own value-range semantics. Kept deliberately permissive on
# integer ranges so it never rejects a valid crontab line.
_CRON_TOKEN_PATTERN: re.Pattern[str] = re.compile(
    r"""
    ^(
        \*                       # every value
      | \*/\d+                   # every n-th value
      | \d+                      # a single value
      | \d+-\d+                  # an inclusive range
      | \d+-\d+/\d+              # a stepped range
    )
    (,
        (
            \*/\d+
          | \d+
          | \d+-\d+
          | \d+-\d+/\d+
        )
    )*$
    """,
    re.VERBOSE,
)

# Default config file, resolved relative to the current working directory. A run
# may pass an explicit path (tests pass a tmp path); absence is first-run-friendly.
DEFAULT_CONFIG_FILENAME: str = "orbit.config.json"

# Defaults matching reference/api-contracts.md so a missing file or missing key
# yields a sane first-run config.
DEFAULT_COOKIE_SOURCE: str = "chrome"
DEFAULT_DEPTH: str = "default"
DEFAULT_SCHEDULE: str = "0 7 * * *"


class ConfigError(Exception):
    """Raised when ``orbit.config.json`` carries an invalid value.

    Carries a clear message naming the offending field, its bad value, and the
    allowed set — a config typo must fail loud at the boundary, never silently
    fall back to a default.
    """


@dataclass
class OrbitConfig:
    """Typed, validated view of ``orbit.config.json``.

    Field defaults match ``reference/api-contracts.md`` so an absent file or absent
    key is first-run friendly. Secrets are NOT represented here — for
    ``cookie_source == "env"`` the source loader reads ``AUTH_TOKEN``/``CT0`` from
    ``.env`` at runtime.

    Attributes:
        cookie_source: Browser to read cookies from, or ``"env"`` to defer to
            ``.env``. One of :data:`ALLOWED_COOKIE_SOURCES`.
        creator_weights: Map of ``channel_id`` / ``creator_handle`` -> priority
            weight (float), the thumb on the ranking scale.
        interests: Topic keywords driving Axis-B classification.
        depth: The cost/time throttle — one of :data:`ALLOWED_DEPTHS`.
        delivery: Output targets (``html_path`` / ``imessage_to`` / ``whatsapp_to``).
        schedule: Cron expression used by the README setup step / ``--setup`` wizard.
    """

    cookie_source: str = DEFAULT_COOKIE_SOURCE
    creator_weights: dict[str, float] = field(default_factory=dict)
    interests: list[str] = field(default_factory=list)
    depth: str = DEFAULT_DEPTH
    delivery: dict[str, Any] = field(default_factory=dict)
    schedule: str = DEFAULT_SCHEDULE


def _resolve_config_path(config_path: Optional[Path]) -> Path:
    """Resolve the config path: an explicit path, else ``./orbit.config.json``.

    Args:
        config_path: An explicit path (tests pass a tmp path), or None.

    Returns:
        The Path to read the config from (may not exist — caller handles absence).
    """
    if config_path is not None:
        return config_path
    return Path.cwd() / DEFAULT_CONFIG_FILENAME


def _validate_enum(field_name: str, value: str, allowed: tuple[str, ...]) -> None:
    """Raise :class:`ConfigError` if ``value`` is not in ``allowed``.

    Args:
        field_name: The config field being validated (for the error message).
        value: The value read from the config.
        allowed: The allowed set for this field.

    Raises:
        ConfigError: If ``value`` is not one of ``allowed``.
    """
    if value not in allowed:
        allowed_display = ", ".join(allowed)
        fix_suggestion = (
            f"Set '{field_name}' in orbit.config.json to one of: {allowed_display}."
        )
        log.log_error(
            "config_invalid_value",
            fix_suggestion=fix_suggestion,
            config_field=field_name,
            invalid_value=value,
            allowed_values=list(allowed),
        )
        raise ConfigError(
            f"Invalid '{field_name}' in orbit.config.json: {value!r}. "
            f"Allowed values: {allowed_display}."
        )


def is_valid_cron_expression(expr: str) -> bool:
    """Return True if ``expr`` is a syntactically valid 5-field cron expression.

    A PURE helper (no I/O, no logging) so the ``--setup`` wizard (sub-phase 2) can reuse
    it to validate a schedule the user types BEFORE writing the config. This checks
    SYNTAX only — exactly five whitespace-separated fields, each matching the standard
    crontab token grammar (``*``, integers, inclusive ranges ``a-b``, comma-lists, and
    steps ``*/n`` or ``a-b/n``). It does NOT range-check values (e.g. minute 0-59); OS
    cron owns value semantics. An empty string, the wrong field count, or any field with
    a bad token returns False.

    Args:
        expr: The candidate cron expression (e.g. ``"0 7 * * *"``).

    Returns:
        True when ``expr`` is a syntactically valid 5-field cron expression, else False.

    Example:
        >>> is_valid_cron_expression("0 7 * * *")
        True
        >>> is_valid_cron_expression("*/15 9-17 * * 1-5")
        True
        >>> is_valid_cron_expression("0 7 * *")
        False
        >>> is_valid_cron_expression("")
        False
        >>> is_valid_cron_expression("99x 7 * * *")
        False
    """
    if not isinstance(expr, str):
        return False
    fields = expr.split()
    if len(fields) != _CRON_FIELD_COUNT:
        return False
    return all(_CRON_TOKEN_PATTERN.match(field_token) is not None for field_token in fields)


def _validate_schedule(schedule: str) -> None:
    """Raise :class:`ConfigError` if ``schedule`` is not a valid 5-field cron expression.

    Fail loud (Rule 12): a malformed ``schedule`` would otherwise silently produce a
    broken crontab line at setup time. The error names the field, the bad value, and a
    ``fix_suggestion`` with the canonical 5-field form.

    Args:
        schedule: The ``schedule`` value read from the config.

    Raises:
        ConfigError: When ``schedule`` is not a syntactically valid cron expression.
    """
    if is_valid_cron_expression(schedule):
        return
    fix_suggestion = (
        "Set 'schedule' in orbit.config.json to a 5-field cron expression "
        "(minute hour day-of-month month day-of-week), e.g. '0 7 * * *' for 7am daily."
    )
    log.log_error(
        "config_invalid_schedule",
        fix_suggestion=fix_suggestion,
        config_field="schedule",
        invalid_value=schedule,
    )
    raise ConfigError(
        f"Invalid 'schedule' in orbit.config.json: {schedule!r}. "
        "Expected a 5-field cron expression like '0 7 * * *'."
    )


def _validate_delivery(delivery: Any) -> None:
    """Validate the ``delivery`` block — a dict with light, intentional field checks.

    Fail loud (Rule 12) on a structurally wrong delivery block: ``delivery`` must be a
    JSON object; ``html_path`` (if present) must be a non-empty string (it is the only
    required output target); ``imessage_to`` / ``whatsapp_to`` (if present and non-null)
    must be strings. Phone-number FORMAT is intentionally NOT validated here — that is
    over-validation; the delivery module + the user own the exact target string.

    Args:
        delivery: The ``delivery`` value read from the config.

    Raises:
        ConfigError: When ``delivery`` is not a dict, or a present field has a bad type.
    """
    if not isinstance(delivery, dict):
        log.log_error(
            "config_invalid_delivery",
            fix_suggestion="Set 'delivery' in orbit.config.json to a JSON object, e.g. {\"html_path\": \"~/orbit/out/today.html\"}.",
            config_field="delivery",
            invalid_value=str(type(delivery).__name__),
        )
        raise ConfigError(
            f"Invalid 'delivery' in orbit.config.json: must be a JSON object, "
            f"got {type(delivery).__name__}."
        )

    if "html_path" in delivery:
        html_path = delivery["html_path"]
        if not isinstance(html_path, str) or not html_path.strip():
            log.log_error(
                "config_invalid_delivery_html_path",
                fix_suggestion="Set 'delivery.html_path' to a non-empty path string, e.g. '~/orbit/out/today.html'.",
                config_field="delivery.html_path",
                invalid_value=repr(html_path),
            )
            raise ConfigError(
                "Invalid 'delivery.html_path' in orbit.config.json: must be a non-empty string, "
                f"got {html_path!r}."
            )

    for target_field in ("imessage_to", "whatsapp_to"):
        if target_field in delivery and delivery[target_field] is not None:
            value = delivery[target_field]
            if not isinstance(value, str):
                log.log_error(
                    "config_invalid_delivery_target",
                    fix_suggestion=f"Set 'delivery.{target_field}' to a string target (or null to disable it).",
                    config_field=f"delivery.{target_field}",
                    invalid_value=repr(value),
                )
                raise ConfigError(
                    f"Invalid 'delivery.{target_field}' in orbit.config.json: must be a string "
                    f"(or null), got {type(value).__name__}."
                )


def _coerce_creator_weights(raw_weights: Any) -> dict[str, float]:
    """Coerce ``creator_weights`` values to float, failing loud on a non-numeric value.

    The derank score multiplies by each creator's weight (a float), so a non-numeric
    weight is a hard error (Rule 12) — not a silent skip. ``creator_weights`` must be a
    JSON object; each value must coerce to float (an int or a numeric string is
    accepted; a non-numeric string or a list/dict is rejected with a field-named error).

    Args:
        raw_weights: The ``creator_weights`` value read from the config.

    Returns:
        A ``creator_external_id`` -> float weight map.

    Raises:
        ConfigError: When ``creator_weights`` is not a dict, or any value is non-numeric.
    """
    if not isinstance(raw_weights, dict):
        log.log_error(
            "config_invalid_creator_weights",
            fix_suggestion="Set 'creator_weights' in orbit.config.json to a JSON object mapping creator id -> float weight.",
            config_field="creator_weights",
            invalid_value=str(type(raw_weights).__name__),
        )
        raise ConfigError(
            f"Invalid 'creator_weights' in orbit.config.json: must be a JSON object, "
            f"got {type(raw_weights).__name__}."
        )

    coerced: dict[str, float] = {}
    for creator_id, raw_value in raw_weights.items():
        # Reason: bool is a subclass of int — reject it explicitly so a stray `true`
        # weight fails loud rather than silently becoming 1.0.
        if isinstance(raw_value, bool):
            _raise_creator_weight_error(creator_id, raw_value)
        try:
            coerced[str(creator_id)] = float(raw_value)
        except (TypeError, ValueError):
            _raise_creator_weight_error(creator_id, raw_value)
    return coerced


def _raise_creator_weight_error(creator_id: Any, raw_value: Any) -> None:
    """Log + raise a field-named :class:`ConfigError` for a non-numeric creator weight.

    Args:
        creator_id: The offending creator key (for the message).
        raw_value: The non-numeric value that could not coerce to float.

    Raises:
        ConfigError: Always — this is the failure path for a bad ``creator_weights`` value.
    """
    log.log_error(
        "config_invalid_creator_weight_value",
        fix_suggestion="Each 'creator_weights' value must be a number (float), e.g. \"UC123\": 1.5.",
        config_field=f"creator_weights.{creator_id}",
        invalid_value=repr(raw_value),
    )
    raise ConfigError(
        f"Invalid 'creator_weights.{creator_id}' in orbit.config.json: weight must be numeric, "
        f"got {raw_value!r}."
    )


def load_config(config_path: Optional[Path] = None) -> OrbitConfig:
    """Load and validate ``orbit.config.json`` into a typed :class:`OrbitConfig`.

    If the file is absent, returns :class:`OrbitConfig` with all defaults
    (first-run friendly) and logs ``config_defaults_used`` — Orbit runs on a clean
    machine before the user has written a config. If present, the JSON is parsed,
    fields are mapped with per-key defaults, and ``cookie_source`` / ``depth`` are
    validated against their allowed sets (a bad value raises :class:`ConfigError`).

    Secrets are never read here: for ``cookie_source == "env"`` the source loader
    reads ``AUTH_TOKEN``/``CT0`` from ``.env`` at runtime — this loader only records
    the intent.

    Args:
        config_path: Explicit path to the config file. Defaults to
            ``./orbit.config.json`` in the current working directory.

    Returns:
        A validated :class:`OrbitConfig`.

    Raises:
        ConfigError: If the file exists but contains invalid JSON, is not a JSON
            object, carries an out-of-range ``cookie_source`` / ``depth``, a malformed
            ``schedule`` cron expression, a non-object ``delivery`` (or a bad
            ``html_path`` / ``imessage_to`` / ``whatsapp_to`` type), or a non-numeric
            ``creator_weights`` value.

    Example:
        >>> config = load_config()  # doctest: +SKIP
        >>> config.depth  # doctest: +SKIP
        'default'
    """
    resolved_path = _resolve_config_path(config_path)

    if not resolved_path.exists():
        # Reason: first run — no config yet. Defaults are sane and the pipeline must
        # not refuse to start just because the user hasn't written a config file.
        log.log_info(
            "config_defaults_used",
            config_path=str(resolved_path),
            detail="orbit.config.json not found; using built-in defaults.",
        )
        return OrbitConfig()

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
        parsed: Any = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        fix_suggestion = (
            "Ensure orbit.config.json exists and is valid JSON (no trailing commas, "
            "double-quoted keys)."
        )
        log.log_error(
            "config_parse_failed",
            fix_suggestion=fix_suggestion,
            config_path=str(resolved_path),
            error_type=type(exc).__name__,
        )
        raise ConfigError(
            f"Could not read orbit.config.json at {resolved_path}: {type(exc).__name__}. "
            "Ensure it exists and is valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        log.log_error(
            "config_not_an_object",
            fix_suggestion="orbit.config.json must be a JSON object (a {...} map).",
            config_path=str(resolved_path),
        )
        raise ConfigError(
            f"orbit.config.json at {resolved_path} must be a JSON object, "
            f"got {type(parsed).__name__}."
        )

    cookie_source = str(parsed.get("cookie_source", DEFAULT_COOKIE_SOURCE))
    depth = str(parsed.get("depth", DEFAULT_DEPTH))
    schedule = str(parsed.get("schedule", DEFAULT_SCHEDULE))
    delivery = parsed.get("delivery", {})
    raw_creator_weights = parsed.get("creator_weights", {})

    _validate_enum("cookie_source", cookie_source, ALLOWED_COOKIE_SOURCES)
    _validate_enum("depth", depth, ALLOWED_DEPTHS)
    _validate_schedule(schedule)
    _validate_delivery(delivery)
    creator_weights = _coerce_creator_weights(raw_creator_weights)

    if cookie_source == "env":
        # Reason: secrets are deferred. The source loader reads AUTH_TOKEN/CT0 from
        # .env at runtime; this loader must NOT touch those values. Note the intent
        # only — no secret is read or logged here.
        log.log_info(
            "config_cookie_source_env",
            detail="cookie_source='env'; cookies will be read from .env (AUTH_TOKEN/CT0) "
            "by the source loader at runtime.",
        )

    config = OrbitConfig(
        cookie_source=cookie_source,
        creator_weights=creator_weights,
        interests=list(parsed.get("interests", [])),
        depth=depth,
        delivery=dict(delivery),
        schedule=schedule,
    )

    log.log_info(
        "config_loaded",
        config_path=str(resolved_path),
        cookie_source=config.cookie_source,
        depth=config.depth,
        interest_count=len(config.interests),
    )
    return config
