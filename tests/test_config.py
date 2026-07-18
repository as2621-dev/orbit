"""DoD tests for the Orbit config schema + validation (Phase 6 / Sub-phase 1).

Per Rule 9, each test encodes WHY the behavior matters, constructed to FAIL on wrong
BUSINESS logic, not merely "returns something":

  1. A valid config loads into the typed OrbitConfig with every field carried through —
     fails if a field is dropped or mistyped (the schema is the durable contract
     api-contracts.md depends on).
  2. EACH invalid field (bad cookie_source, bad depth, malformed cron, bad delivery,
     non-numeric creator_weights) raises a field-NAMED ConfigError — fails if any bad
     value silently falls back to a default (Rule 12: config typos must fail loud, never
     silently default, or the user gets a wrong digest with no signal).
  3. The shipped orbit.config.example.json parses AND validates as a complete example —
     fails if the example drifts from what load_config accepts (onboarding would break).
  4. .env.example carries only placeholders — fails if a real-token-shaped secret is ever
     committed (the security posture; AUTH_TOKEN/CT0 must be empty).

No network, no LLM, no real user DB — config loading is pure file I/O on tmp paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``scripts`` importable so ``from lib import config`` resolves
# regardless of the working directory. Mirrors tests/test_scoops_and_render.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.config import (  # noqa: E402
    ConfigError,
    OrbitConfig,
    is_valid_cron_expression,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, payload: dict) -> Path:
    """Write a config payload to a tmp orbit.config.json and return its path."""
    config_path = tmp_path / "orbit.config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


# --- DoD #1: a valid config loads into the typed OrbitConfig -----------------


def test_valid_config_loads_into_typed_orbit_config(tmp_path: Path) -> None:
    """A complete valid config loads into OrbitConfig with every field carried through (DoD #1).

    WHY: the config schema is the durable contract every stage agrees on
    (api-contracts.md). If load_config dropped or mistyped a field (e.g. lost a
    creator_weight, or failed to coerce a numeric-string weight to float), the derank
    score and delivery routing would silently use the wrong values. This pins each
    field's value AND that creator_weights values are real floats.
    """
    config_path = _write_config(
        tmp_path,
        {
            "cookie_source": "firefox",
            "creator_weights": {"UC_chan": 1.5, "x_handle": "2"},
            "interests": ["ai agents", "f1"],
            "depth": "deep",
            "delivery": {"html_path": "~/orbit/out/today.html", "email_to": "me@example.com"},
            "schedule": "0 7 * * *",
        },
    )

    config = load_config(config_path)

    assert isinstance(config, OrbitConfig)
    assert config.cookie_source == "firefox"
    assert config.depth == "deep"
    assert config.schedule == "0 7 * * *"
    assert config.interests == ["ai agents", "f1"]
    # creator_weights values must be coerced to real floats (the numeric string "2" -> 2.0).
    assert config.creator_weights == {"UC_chan": 1.5, "x_handle": 2.0}
    assert all(isinstance(weight, float) for weight in config.creator_weights.values())
    assert config.delivery["html_path"] == "~/orbit/out/today.html"
    assert config.delivery["email_to"] == "me@example.com"


def test_missing_config_file_returns_defaults(tmp_path: Path) -> None:
    """An absent config file yields all-default OrbitConfig (first-run friendly, unchanged).

    WHY: Orbit must run on a clean machine before the user writes a config. This pins the
    existing defaults-on-missing-file behavior so the new validators did not break it.
    """
    config = load_config(tmp_path / "does-not-exist.json")

    assert config.cookie_source == "chrome"
    assert config.depth == "default"
    assert config.schedule == "0 7 * * *"
    assert config.creator_weights == {}


# --- DoD #2: each invalid field raises a field-named ConfigError -------------


def test_bad_cookie_source_raises_field_named_error(tmp_path: Path) -> None:
    """An unknown cookie_source fails loud, naming the field (Rule 12, DoD #2).

    WHY: a typo'd cookie_source ('chrom') must NOT silently default to chrome — that would
    read the wrong browser's cookies (or none) and the user would never know. The error
    must name 'cookie_source' so the fix is obvious.
    """
    config_path = _write_config(tmp_path, {"cookie_source": "chrom"})

    with pytest.raises(ConfigError, match="cookie_source"):
        load_config(config_path)


def test_bad_depth_raises_field_named_error(tmp_path: Path) -> None:
    """An out-of-range depth fails loud, naming the field (Rule 12, DoD #2).

    WHY: depth is the single cost throttle. A bad value ('medium') silently defaulting
    would mis-bill the user's run; it must fail loud naming 'depth'.
    """
    config_path = _write_config(tmp_path, {"depth": "medium"})

    with pytest.raises(ConfigError, match="depth"):
        load_config(config_path)


@pytest.mark.parametrize(
    "bad_cron",
    [
        "0 7 * *",        # too few fields
        "0 7 * * * *",    # too many fields
        "",               # empty
        "99x 7 * * *",    # bad token
        "0 7 * * mon",    # day-name token (not supported by the numeric grammar)
    ],
)
def test_malformed_cron_schedule_raises_field_named_error(tmp_path: Path, bad_cron: str) -> None:
    """A malformed cron schedule fails loud, naming the field + the bad value (Rule 12, DoD #2).

    WHY: the schedule is written into the user's crontab. A malformed expression
    ('0 7 * *' — only 4 fields) that silently defaulted would produce a broken or
    surprising cron line. Each malformed shape must raise a ConfigError naming 'schedule'.
    """
    config_path = _write_config(tmp_path, {"schedule": bad_cron})

    with pytest.raises(ConfigError, match="schedule"):
        load_config(config_path)


def test_valid_cron_schedule_accepted(tmp_path: Path) -> None:
    """A range/step/list cron expression validates (the grammar is not over-strict).

    WHY: the validator must accept real crontab forms (steps, ranges, lists) — rejecting a
    valid '*/15 9-17 * * 1-5' would block a legitimate user schedule. Pins the happy path.
    """
    config_path = _write_config(tmp_path, {"schedule": "*/15 9-17 1,15 * 1-5"})

    config = load_config(config_path)
    assert config.schedule == "*/15 9-17 1,15 * 1-5"


def test_non_object_delivery_raises_field_named_error(tmp_path: Path) -> None:
    """A non-object delivery block fails loud, naming the field (Rule 12, DoD #2).

    WHY: delivery must be a JSON object holding the output targets. A string/array there
    would break the render + delivery stages downstream; it must fail loud at the boundary.
    """
    config_path = _write_config(tmp_path, {"delivery": "send it to my phone"})

    with pytest.raises(ConfigError, match="delivery"):
        load_config(config_path)


def test_empty_delivery_html_path_raises_field_named_error(tmp_path: Path) -> None:
    """An empty delivery.html_path fails loud, naming the field (Rule 12, DoD #2).

    WHY: html_path is the one required output target. An empty string would resolve to a
    nonsense path and the digest would write nowhere useful; it must fail loud.
    """
    config_path = _write_config(tmp_path, {"delivery": {"html_path": "   "}})

    with pytest.raises(ConfigError, match="delivery.html_path"):
        load_config(config_path)


def test_non_string_email_target_raises_field_named_error(tmp_path: Path) -> None:
    """A non-string delivery.email_to fails loud, naming the field (Rule 12, DoD #2).

    WHY: the delivery recipient is the email address the digest is sent to — a string. A
    number/object there would break the send; it must fail loud naming the field. (Format
    is NOT validated — only the type — to avoid over-validating email addresses.)
    """
    config_path = _write_config(tmp_path, {"delivery": {"email_to": 12345}})

    with pytest.raises(ConfigError, match="delivery.email_to"):
        load_config(config_path)


def test_legacy_imessage_whatsapp_keys_are_ignored_not_rejected(tmp_path: Path) -> None:
    """A config still carrying leftover imessage_to/whatsapp_to keys loads without crashing.

    WHY: real users upgrading from the iMessage era have an on-disk orbit.config.json whose
    delivery block still contains imessage_to (and maybe whatsapp_to). The schema no longer
    knows those keys — but loading must degrade gracefully (the key is ignored), never raise
    a ConfigError. Pinning this makes the migration deliberate: a leftover key is tolerated,
    not a hard failure that would break every returning user's next run.
    """
    config_path = _write_config(
        tmp_path,
        {"delivery": {"html_path": "~/orbit/out/today.html", "imessage_to": "+15551234567", "whatsapp_to": None}},
    )

    config = load_config(config_path)

    # It loaded (no ConfigError) and html_path survived; the unknown keys are simply not
    # consumed by any stage. The value passing through is harmless — nothing reads it.
    assert config.delivery["html_path"] == "~/orbit/out/today.html"


def test_non_numeric_creator_weight_raises_field_named_error(tmp_path: Path) -> None:
    """A non-numeric creator_weights value fails loud, naming the creator key (Rule 12, DoD #2).

    WHY: the derank score multiplies by each creator weight (a float). A non-numeric weight
    ('high') silently skipped would drop the user's priority signal for that creator; it
    must fail loud naming the offending creator key.
    """
    config_path = _write_config(tmp_path, {"creator_weights": {"UC_chan": "high"}})

    with pytest.raises(ConfigError, match="creator_weights.UC_chan"):
        load_config(config_path)


def test_boolean_creator_weight_raises_field_named_error(tmp_path: Path) -> None:
    """A boolean creator_weights value fails loud, not silently coerced to 1.0/0.0 (Rule 12).

    WHY: bool is a Python int subclass, so float(True) == 1.0 would SILENTLY accept a
    meaningless `true` weight. The user meant a number; a boolean is a mistake and must
    fail loud rather than masquerade as a neutral/zero weight.
    """
    config_path = _write_config(tmp_path, {"creator_weights": {"UC_chan": True}})

    with pytest.raises(ConfigError, match="creator_weights.UC_chan"):
        load_config(config_path)


# --- DoD #3: the shipped example config parses + validates -------------------


def test_example_config_parses_and_validates() -> None:
    """orbit.config.example.json is valid JSON AND validates through load_config (DoD #3).

    WHY: the example is the onboarding template a new user copies. If it drifted from what
    load_config accepts (a bad enum, a malformed cron, a non-numeric weight), the very
    first run from the example would fail. This proves the shipped example is a complete,
    accepted config.
    """
    example_path = REPO_ROOT / "orbit.config.example.json"
    assert example_path.exists(), "orbit.config.example.json must ship at the repo root"

    # It must be valid JSON (no // comments — standard JSON).
    json.loads(example_path.read_text(encoding="utf-8"))

    # And it must validate cleanly through the real loader.
    config = load_config(example_path)
    assert config.cookie_source in ("chrome", "firefox", "safari", "edge", "brave", "env")
    assert config.depth in ("quick", "default", "deep")
    assert is_valid_cron_expression(config.schedule)
    assert config.delivery.get("html_path")
    assert all(isinstance(weight, float) for weight in config.creator_weights.values())


# --- DoD #4: .env.example contains only placeholders -------------------------


def test_env_example_contains_only_placeholders() -> None:
    """.env.example carries empty AUTH_TOKEN/CT0 placeholders — no real-token-shaped value (DoD #4).

    WHY: the security posture is that secrets NEVER land in the repo. A committed
    .env.example with a real-token-shaped value would leak a credential. The cookie vars
    must be present (for onboarding) but EMPTY, and any WhatsApp/Twilio lines must stay
    commented (so they are documentation, not active assignments).
    """
    env_example_path = REPO_ROOT / ".env.example"
    assert env_example_path.exists(), ".env.example must ship for onboarding"

    lines = env_example_path.read_text(encoding="utf-8").splitlines()
    active_assignments: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        active_assignments[key.strip()] = value.strip()

    # The cookie credential vars must exist as placeholders and be EMPTY.
    assert "AUTH_TOKEN" in active_assignments, "AUTH_TOKEN placeholder must be present"
    assert "CT0" in active_assignments, "CT0 placeholder must be present"
    # No active assignment may carry a real-token-shaped value (every placeholder is empty).
    for key, value in active_assignments.items():
        assert value == "", f"{key} in .env.example must be an empty placeholder, not a real value"
