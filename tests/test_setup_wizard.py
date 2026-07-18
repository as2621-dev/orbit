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

# Make ``scripts`` importable so ``from lib import ...`` resolves regardless
# of the working directory. Mirrors tests/test_config.py / test_orbit_stage0.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.bird_x import XAuthError  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.setup_wizard import _ORBIT_CRON_MARKER, generate_cron_entry, install_cron_entry, run_setup_wizard  # noqa: E402
from lib.subproc import SubprocResult  # noqa: E402
from lib.youtube_yt import Subscription  # noqa: E402


def _scripted_input(answers: list[str]) -> MagicMock:
    """Build an ``input_fn`` that returns the scripted answers in order.

    Extra prompts past the scripted list return "" (the wizard treats empty as the
    prompt's default), so a test only scripts the answers whose value it asserts on.
    """
    return MagicMock(side_effect=lambda _prompt: answers.pop(0) if answers else "")


class _FakeCrontab:
    """An in-memory ``crontab_runner`` double so no test touches the real user crontab.

    Simulates ``crontab -l`` (read) and ``crontab -`` (write) against a stored crontab
    body. The read result is scriptable to reproduce the "no crontab for user" first-run
    case (non-zero return code + that stderr) and a genuine binary failure (raising).

    Attributes:
        stored: The current crontab body returned by subsequent reads.
        writes: Every crontab body written, in order (lets a test assert idempotency).
        read_returncode: Return code the scripted ``crontab -l`` reports.
        read_stderr: Stderr the scripted ``crontab -l`` reports.
        raise_on: An exception to raise instead of running (simulates a missing binary).
    """

    def __init__(
        self,
        *,
        stored: str = "",
        read_returncode: int = 0,
        read_stderr: str = "",
        write_returncode: int = 0,
        raise_on: BaseException | None = None,
    ) -> None:
        self.stored = stored
        self.writes: list[str] = []
        self.read_returncode = read_returncode
        self.read_stderr = read_stderr
        self.write_returncode = write_returncode
        self.raise_on = raise_on

    def __call__(self, command: list[str], stdin_text: str | None) -> SubprocResult:
        if self.raise_on is not None:
            raise self.raise_on
        if command == ["crontab", "-l"]:
            return SubprocResult(
                returncode=self.read_returncode, stdout=self.stored, stderr=self.read_stderr
            )
        if command == ["crontab", "-"]:
            self.writes.append(stdin_text or "")
            self.stored = stdin_text or ""
            self.read_returncode = 0
            self.read_stderr = ""
            return SubprocResult(returncode=self.write_returncode, stdout="", stderr="")
        raise AssertionError(f"unexpected crontab command: {command!r}")


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
    #   html_path, email (blank). The schedule is NO LONGER asked (fixed 7am).
    answers = ["chrome", "y", "y", "y", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=_FakeCrontab(),
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
    assert "email_to" not in loaded.delivery  # blank answer => opt-out, no recipient stored

    # Sanity: the on-disk JSON is the api-contracts shape.
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["cookie_source"] == "chrome"
    assert raw["depth"] == "default"


def test_generate_cron_entry_contains_claude_orbit_command() -> None:
    """generate_cron_entry must emit a valid line invoking `claude -p "/orbit"` (brief §8.3).

    WHY: step 5 of setup hands the user a crontab line they paste verbatim. If the command
    isn't the brief's default scheduler invocation, their cron would run the wrong thing (or
    nothing). The ``--dangerously-skip-permissions`` flag is load-bearing: without it the
    headless cron run blocks on a permission prompt and silently produces nothing. We assert
    the schedule prefix, the skip-permissions flag, and the exact command tail.
    """
    entry = generate_cron_entry("0 7 * * *", repo_path=Path("/home/me/orbit"))
    assert entry.startswith("0 7 * * * ")
    assert 'claude -p --dangerously-skip-permissions "/orbit"' in entry
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

    # cookie, confirm cat, prioritize (no), html, email (blank). No schedule prompt.
    answers = ["chrome", "y", "n", "~/orbit/out/today.html", ""]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=llm,
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=_FakeCrontab(),
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

    answers = ["chrome", "y", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=_FakeCrontab(),
    )

    assert exit_code == 0
    loaded = load_config(config_path)
    assert loaded.interests == ["yt only"]
    # No X creator made it in (the loader raised before returning any follow).
    assert loaded.creator_weights == {}


def test_wizard_writes_email_target_when_provided(tmp_path: Path) -> None:
    """A provided email address must be persisted as delivery.email_to.

    WHY: delivery is opt-in — earlier tests prove blank => no target. The mirror case must
    also hold: when the user DOES give an address, it must reach the config so the
    email-delivery slice has a recipient. Fails if the wizard ignores the provided address.
    """
    config_path = tmp_path / "orbit.config.json"
    youtube_loader = MagicMock(
        return_value=[Subscription(channel_id="UC_yt", display_name="YT Only")]
    )
    x_loader = MagicMock(return_value=[])

    # cookie, confirm cat, prioritize (no), html, email (me@example.com). No schedule prompt.
    answers = ["chrome", "y", "n", "~/orbit/out/today.html", "me@example.com"]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=youtube_loader,
        x_loader=x_loader,
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=_FakeCrontab(),
    )

    loaded = load_config(config_path)
    assert loaded.delivery["email_to"] == "me@example.com"


def test_install_cron_entry_fresh_install_writes_marker_tagged_line() -> None:
    """A fresh install writes exactly the entry tagged with the Orbit marker.

    WHY: the marker is what makes re-runs idempotent — the installed line MUST carry it,
    or a second run can't find and replace it and would append a duplicate. On an empty
    crontab the result is the single tagged line and nothing else.
    """
    fake = _FakeCrontab(stored="")

    installed = install_cron_entry("0 7 * * * echo run", crontab_runner=fake)

    assert installed is True
    assert fake.writes == [f"0 7 * * * echo run {_ORBIT_CRON_MARKER}\n"]


def test_install_cron_entry_replaces_orbit_line_not_duplicates() -> None:
    """A second install REPLACES the marked Orbit line and leaves unrelated lines intact.

    WHY: re-running the wizard must not accumulate stale Orbit cron lines (the machine
    would then run Orbit twice, on the old and new schedule). The replacement matches on
    the marker, so exactly one marked line survives; a user's unrelated crontab job is
    never touched.
    """
    fake = _FakeCrontab(
        stored=f"0 9 * * * cd /repo && claude -p \"/orbit\" {_ORBIT_CRON_MARKER}\n30 8 * * * my-backup-job\n"
    )

    installed = install_cron_entry("0 7 * * * new-orbit-command", crontab_runner=fake)

    assert installed is True
    final_crontab = fake.stored
    assert final_crontab.count(_ORBIT_CRON_MARKER) == 1  # not duplicated
    assert "new-orbit-command" in final_crontab  # the new schedule replaced the old
    assert "0 9 * * *" not in final_crontab  # the stale Orbit line is gone
    assert "my-backup-job" in final_crontab  # the unrelated job is preserved


def test_install_cron_entry_treats_no_crontab_for_user_as_empty() -> None:
    """The "no crontab for user" first-run case must be treated as an empty crontab.

    WHY: on a machine that has never had a crontab, ``crontab -l`` exits non-zero with a
    "no crontab for <user>" message. If that were treated as a hard read failure, a brand
    new user could never get auto-installed. It must degrade to a clean fresh install.
    """
    fake = _FakeCrontab(stored="", read_returncode=1, read_stderr="no crontab for testuser")

    installed = install_cron_entry("0 7 * * * run", crontab_runner=fake)

    assert installed is True
    assert fake.stored == f"0 7 * * * run {_ORBIT_CRON_MARKER}\n"


def test_install_cron_entry_fails_soft_when_crontab_binary_missing(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing/unspawnable ``crontab`` binary must fail SOFT: return False + log the failure.

    WHY: a sandboxed or CI environment may have no ``crontab`` at all. Setup must still
    complete (the caller falls back to printing the entry), so install returns False rather
    than raising, and logs an actionable ``setup_cron_install_failed`` for the user.
    """
    fake = _FakeCrontab(raise_on=FileNotFoundError("crontab: command not found"))

    installed = install_cron_entry("0 7 * * * run", crontab_runner=fake)

    assert installed is False
    assert "setup_cron_install_failed" in capsys.readouterr().out


def test_install_cron_entry_fails_soft_on_unreadable_crontab_without_clobbering(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A genuine (non "no crontab") read error must fail soft AND never write a new crontab.

    WHY: if ``crontab -l`` fails for a real reason (e.g. permissions) we do NOT know the
    current crontab, so piping a fresh one would clobber the user's existing jobs. The
    function must refuse to write and fall back — return False, write nothing, log why.
    """
    fake = _FakeCrontab(stored="", read_returncode=1, read_stderr="crontab: permission denied")

    installed = install_cron_entry("0 7 * * * run", crontab_runner=fake)

    assert installed is False
    assert fake.writes == []  # never overwrote the crontab we couldn't read
    assert "setup_cron_install_failed" in capsys.readouterr().out


def test_wizard_installs_single_marker_line_at_fixed_schedule(tmp_path: Path) -> None:
    """End-to-end: the wizard installs exactly one marker-tagged 7am Orbit crontab line.

    WHY: this is the phase DoD — a scripted wizard run must auto-install (via the injected
    runner) exactly one ``0 7 * * *`` marker-tagged line, and the written config must carry
    that same fixed schedule. Proves the install path is wired into step 5 and the schedule
    is the fixed default, not something the user typed.
    """
    config_path = tmp_path / "orbit.config.json"
    fake = _FakeCrontab(stored="")

    # cookie, confirm cat, prioritize (no), html, email (blank) — no schedule prompt.
    answers = ["chrome", "y", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_x", display_name="X Chan")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=fake,
    )

    assert exit_code == 0
    assert fake.stored.count(_ORBIT_CRON_MARKER) == 1  # exactly one Orbit line installed
    assert fake.stored.startswith("0 7 * * * ")  # the fixed 7am schedule
    assert load_config(config_path).schedule == "0 7 * * *"  # config carries it too


def test_wizard_no_longer_prompts_for_a_schedule(tmp_path: Path) -> None:
    """The wizard must NOT ask the user for a schedule anymore (fixed 7am decision).

    WHY: the 2026-07-06 ruling made the schedule a fixed local-auto-cron default, not a
    config knob. If a schedule prompt survived, a user could still set a non-7am schedule,
    contradicting the decision. We assert no prompt mentions schedule/cron, and the config
    still carries the fixed default.
    """
    config_path = tmp_path / "orbit.config.json"
    input_mock = _scripted_input(["chrome", "y", "n", "~/orbit/out/today.html", ""])

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_x", display_name="X Chan")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=input_mock,
        store_module=_fake_store(),
        crontab_runner=_FakeCrontab(),
    )

    prompt_texts = [call.args[0].lower() for call in input_mock.call_args_list]
    assert not any("schedule" in text or "cron" in text for text in prompt_texts), (
        f"the wizard must not prompt for a schedule anymore; prompts were: {prompt_texts}"
    )
    assert load_config(config_path).schedule == "0 7 * * *"


def test_wizard_falls_back_to_printed_entry_when_crontab_write_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing ``crontab`` write must degrade to printing the entry — setup still succeeds.

    WHY: a sandboxed/CI run where the crontab write fails must not abort onboarding. The
    wizard completes (exit 0), logs the failure, and prints the manual `crontab -e` fallback
    with the exact 7am line so the user can paste it themselves.
    """
    config_path = tmp_path / "orbit.config.json"
    fake = _FakeCrontab(write_returncode=1)

    answers = ["chrome", "y", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_x", display_name="X Chan")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        crontab_runner=fake,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "setup_cron_install_failed" in output  # the failure was surfaced, not hidden
    assert "crontab -e" in output  # the manual fallback instruction
    assert "0 7 * * *" in output  # the exact entry to paste
