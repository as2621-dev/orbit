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
            object, or carries an out-of-range ``cookie_source`` / ``depth``.

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

    _validate_enum("cookie_source", cookie_source, ALLOWED_COOKIE_SOURCES)
    _validate_enum("depth", depth, ALLOWED_DEPTHS)

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
        creator_weights=dict(parsed.get("creator_weights", {})),
        interests=list(parsed.get("interests", [])),
        depth=depth,
        delivery=dict(parsed.get("delivery", {})),
        schedule=str(parsed.get("schedule", DEFAULT_SCHEDULE)),
    )

    log.log_info(
        "config_loaded",
        config_path=str(resolved_path),
        cookie_source=config.cookie_source,
        depth=config.depth,
        interest_count=len(config.interests),
    )
    return config
