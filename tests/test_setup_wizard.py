"""DoD tests for the `/orbit --setup` wizard + cron-entry generation (Phase 6 / Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, constructed to FAIL on wrong
BUSINESS logic, not merely "returns something":

  1. The wizard, driven by MOCKED loaders + a mock LLM + scripted input, writes an
     ``orbit.config.json`` that LOADS BACK CLEANLY through ``lib.config.load_config`` and
     carries the chosen ``creator_weights``, seeded ``interests``, and ``schedule`` — fails
     if the wizard writes a shape the real loader rejects (onboarding would hand the user a
     config the pipeline then refuses), or drops the user's choices.
  2. ``generate_cron_entry`` returns a syntactically valid crontab line containing
     ``claude -p "/orbit"`` — fails if the brief's default scheduler command (§8.3 step 5)
     is wrong, which would give the user a cron line that doesn't run Orbit.
  3. ``generate_cron_entry`` rejects a malformed cron — fails if a broken schedule could
     silently reach the user's crontab (Rule 12 fail-loud).
  4. The wizard auto-classifies via the EXISTING classify path — asserted by the injected
     ``llm_classifier`` being CALLED. Fails if a separate classifier is introduced (DoD:
     "no separate classifier"), which would diverge from how the daily run judges items.
  5. An X-loader ``XAuthError`` lets the wizard continue YouTube-only and still write a
     valid config — fails if an unconfigured optional source aborts setup (X is additive).

Loaders/LLM/input/IO are all mocked — NO live calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make ``skills/orbit/scripts`` importable so ``from lib import ...`` resolves regardless
# of the working directory. Mirrors tests/test_config.py / test_orbit_stage0.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.bird_x import XAuthError  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.setup_wizard import generate_cron_entry, run_setup_wizard  # noqa: E402
from lib.youtube_yt import Subscription  # noqa: E402


def _scripted_input(answers: list[str]) -> MagicMock:
    """Build an ``input_fn`` that returns the scripted answers in order.

    Extra prompts past the scripted list return "" (the wizard treats empty as the
    prompt's default), so a test only scripts the answers whose value it asserts on.
    """
    return MagicMock(side_effect=lambda _prompt: answers.pop(0) if answers else "")


def _fake_store() -> MagicMock:
    """A store double for the classify path: no prior override, persist is a no-op.

    The classify path reads ``get_classification`` (None => no user override, so it asks
    the LLM) and writes ``set_classification``. Mocking it keeps auto-classify entirely
    offline — NO real per-user DB is touched (the directive's "IO mocked" rule).
    """
    store = MagicMock()
    store.get_classification.return_value = None
    return store


def _signal_classifier() -> MagicMock:
    """A mock LLM boundary returning a 'signal + on-topic' strict-JSON verdict.

    Returns the exact JSON shape ``lib.classify._parse_verdict`` expects, so the wizard's
    auto-classify path resolves a real category WITHOUT a live model.
    """
    return MagicMock(return_value='{"axis_a_signal": 1, "axis_b_on_topic": 1}')


def test_wizard_writes_loadable_config_with_choices(tmp_path: Path) -> None:
    """The wizard must write a config that load_config accepts AND carries the user's choices.

    WHY: the wizard's whole job is to hand a NEW user a working ``orbit.config.json``. If
    what it writes doesn't validate through the same ``load_config`` the pipeline uses, the
    very next ``/orbit`` run would fail with a ConfigError — onboarding would be broken. And
    if the chosen priority creator, seeded interests, or schedule were dropped, the digest
    would silently ignore the user's setup. So we drive it end-to-end and load the result
    back through the real loader, asserting the choices survived.
    """
    config_path = tmp_path / "orbit.config.json"
    youtube_loader = MagicMock(
        return_value=[
            Subscription(channel_id="UC_aaa", display_name="AI Lab"),
            Subscription(channel_id="UC_bbb", display_name="F1 News"),
        ]
    )
    x_loader = MagicMock(return_value=[])

    # Scripted answers, in prompt order:
    #   cookie source, confirm cat #1, confirm cat #2,
    #   prioritize #1 (yes), prioritize #2 (no),
    #   html_path, imessage (blank), schedule
    answers = ["chrome", "y", "y", "y", "n", "~/orbit/out/today.html", "", "0 7 * * *"]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
    )

    assert exit_code == 0
    assert config_path.exists()

    # WHY this assertion: the durable contract is that load_config accepts it.
    loaded = load_config(config_path)
    assert loaded.creator_weights == {"UC_aaa": 2.0}  # the one prioritized creator
    assert "UC_bbb" not in loaded.creator_weights  # the declined creator is absent
    assert loaded.schedule == "0 7 * * *"
    # Interests seeded from subscription titles (lower-cased), order-preserved.
    assert loaded.interests == ["ai lab", "f1 news"]
    assert loaded.delivery["html_path"] == "~/orbit/out/today.html"
    assert "imessage_to" not in loaded.delivery  # blank answer => opt-out, never messaged

    # Sanity: the on-disk JSON is the api-contracts shape.
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["cookie_source"] == "chrome"
    assert raw["depth"] == "default"


def test_generate_cron_entry_contains_claude_orbit_command() -> None:
    """generate_cron_entry must emit a valid line invoking `claude -p "/orbit"` (brief §8.3).

    WHY: step 5 of setup hands the user a crontab line they paste verbatim. If the command
    isn't the brief's default scheduler invocation, their cron would run the wrong thing (or
    nothing). We assert both the schedule prefix and the exact command tail.
    """
    entry = generate_cron_entry("0 7 * * *", repo_path=Path("/home/me/orbit"))
    assert entry.startswith("0 7 * * * ")
    assert 'claude -p "/orbit"' in entry
    assert "cd /home/me/orbit" in entry


def test_generate_cron_entry_rejects_malformed_cron() -> None:
    """A malformed cron must fail loud, never producing a broken crontab line (Rule 12).

    WHY: silently emitting a line built from a bad schedule would give the user a cron entry
    that never fires — a quiet failure that's hard to diagnose. The function must raise.
    """
    with pytest.raises(ValueError):
        generate_cron_entry("not a cron")
    with pytest.raises(ValueError):
        generate_cron_entry("0 7 * *")  # only 4 fields


def test_wizard_auto_classifies_via_existing_classify_path(tmp_path: Path) -> None:
    """Auto-classify must go through the existing classify path (the injected LLM is CALLED).

    WHY: the DoD requires "no separate classifier" — the setup's category judgment must be
    the SAME two-axis ``classify_item`` path the daily run uses, so a creator classified at
    setup time matches how its items are judged later. The observable proof that the wizard
    used that path is that the injected ``llm_classifier`` boundary (which classify_item
    calls) was invoked once per creator. If a parallel classifier were added, this mock
    would never be called.
    """
    config_path = tmp_path / "orbit.config.json"
    youtube_loader = MagicMock(
        return_value=[Subscription(channel_id="UC_only", display_name="Solo Channel")]
    )
    x_loader = MagicMock(return_value=[])
    llm = _signal_classifier()

    # cookie, confirm cat, prioritize (no), html, imessage (blank), schedule
    answers = ["chrome", "y", "n", "~/orbit/out/today.html", "", "0 7 * * *"]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=llm,
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
    )

    # The classify path renders a prompt then calls the LLM boundary once per creator.
    assert llm.call_count == 1


def test_wizard_continues_youtube_only_when_x_auth_fails(tmp_path: Path) -> None:
    """An X auth failure must NOT abort setup — the wizard writes a valid YouTube-only config.

    WHY: X is an ADDITIVE source (mirrors Stage 0). A user who hasn't configured X cookies
    must still complete setup and get a working config; aborting would block onboarding for
    the common YouTube-only case. We make the X loader raise and assert setup still succeeds
    with a loadable config containing the YouTube creator's seeded interest.
    """
    config_path = tmp_path / "orbit.config.json"
    youtube_loader = MagicMock(
        return_value=[Subscription(channel_id="UC_yt", display_name="YT Only")]
    )
    x_loader = MagicMock(side_effect=XAuthError("X cookies missing"))

    answers = ["chrome", "y", "n", "~/orbit/out/today.html", "", "0 7 * * *"]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
    )

    assert exit_code == 0
    loaded = load_config(config_path)
    assert loaded.interests == ["yt only"]
    # No X creator made it in (the loader raised before returning any follow).
    assert loaded.creator_weights == {}


def test_wizard_writes_imessage_target_when_provided(tmp_path: Path) -> None:
    """A provided iMessage number must be persisted as delivery.imessage_to.

    WHY: delivery is opt-in — earlier tests prove blank => no target. The mirror case must
    also hold: when the user DOES give a number, it must reach the config so Sub-phase 3's
    iMessage delivery has a target. Fails if the wizard ignores the provided number.
    """
    config_path = tmp_path / "orbit.config.json"
    youtube_loader = MagicMock(
        return_value=[Subscription(channel_id="UC_yt", display_name="YT Only")]
    )
    x_loader = MagicMock(return_value=[])

    # cookie, confirm cat, prioritize (no), html, imessage (+15551234567), schedule
    answers = ["chrome", "y", "n", "~/orbit/out/today.html", "+15551234567", "0 7 * * *"]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
    )

    loaded = load_config(config_path)
    assert loaded.delivery["imessage_to"] == "+15551234567"
