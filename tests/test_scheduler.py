"""DoD tests for the daily-run scheduler (``lib.scheduler``): launchd + legacy cron surface.

Two suites live here, both per Rule 9 (each test encodes WHY the behavior matters, built to
FAIL on wrong BUSINESS logic, not merely "returns something"):

  * **launchd** — the wake-proof daily-run scheduler that replaces cron. Pins the schedule
    (07:00 ``StartCalendarInterval``), the wake-catch-up invariant (no ``StartInterval`` /
    ``Disabled`` that would reintroduce cron's skip), idempotent-by-label install (bootout
    before bootstrap), the one-time cron migration (retire the orbit line, keep foreign
    jobs), the ``--dangerously-skip-permissions`` carry-over + its stated rationale, and the
    two fail-soft paths.
  * **legacy cron** — ``generate_cron_entry`` / ``install_cron_entry`` and the read helpers,
    MOVED here verbatim from ``tests/test_setup_wizard.py`` when the code moved out of the
    wizard. Their assertions are kept intact so the characterized cron surface (which the
    migration reads against) stays pinned across the move.

All subprocess boundaries (launchctl, crontab) are faked; no test runs a real ``launchctl``
or ``crontab`` or touches the real ``~/Library/LaunchAgents`` / user crontab.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

# Make ``scripts`` importable so ``from lib import ...`` resolves regardless of the working
# directory. Mirrors tests/test_setup_wizard.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import scheduler  # noqa: E402
from lib.scheduler import _ORBIT_CRON_MARKER, generate_cron_entry, install_cron_entry  # noqa: E402
from lib.subproc import SubprocResult  # noqa: E402


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


class _FakeLaunchctl:
    """An in-memory ``launchctl_runner`` double so no test runs a real ``launchctl``.

    Records every argv in order (lets a test assert bootout precedes bootstrap) and returns
    scriptable per-verb return codes. ``raise_on`` simulates a missing / sandboxed binary
    (the runner raising) — mirrors :class:`_FakeCrontab`'s scriptable-returncode design.

    Attributes:
        commands: Every launchctl argv received, in order.
        returncodes: Per-verb (e.g. ``"bootstrap"``) return-code overrides; default 0.
        raise_on: An exception to raise instead of running (missing binary / sandbox).
    """

    def __init__(
        self,
        *,
        returncodes: dict[str, int] | None = None,
        raise_on: BaseException | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.returncodes = returncodes or {}
        self.raise_on = raise_on

    def __call__(self, command: list[str]) -> SubprocResult:
        if self.raise_on is not None:
            raise self.raise_on
        self.commands.append(command)
        verb = command[1] if len(command) > 1 else ""
        return SubprocResult(returncode=self.returncodes.get(verb, 0), stdout="", stderr="")

    @property
    def verbs(self) -> list[str]:
        """The launchctl subcommand of each recorded call, in order."""
        return [command[1] for command in self.commands if len(command) > 1]


# ---------------------------------------------------------------------------
# launchd — the wake-proof daily-run scheduler.
# ---------------------------------------------------------------------------


def test_generate_launchd_plist_schedules_7am_with_skip_permissions() -> None:
    """The plist is a valid ``com.orbit.daily`` LaunchAgent: 7am schedule + headless run flag.

    WHY: setup writes this plist verbatim under ``~/Library/LaunchAgents``. If the label,
    schedule, working dir, or the headless ``--dangerously-skip-permissions`` flag were
    wrong, the daily run would not fire at 7am, would not find its config, or would silently
    block on a permission prompt and produce nothing. We parse the generated plist and assert
    each load-bearing field — not merely that a string was returned.
    """
    plist_text = scheduler.generate_launchd_plist(
        repo_path=Path("/home/me/orbit"), minute=0, hour=7, claude_executable="/opt/homebrew/bin/claude"
    )
    parsed = plistlib.loads(plist_text.encode("utf-8"))

    assert parsed["Label"] == "com.orbit.daily"
    assert parsed["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert parsed["WorkingDirectory"] == "/home/me/orbit"
    # The headless run flag must be an argv element (never shell-interpolated).
    assert parsed["ProgramArguments"] == [
        "/opt/homebrew/bin/claude",
        "-p",
        "--dangerously-skip-permissions",
        "/orbit",
    ]


def test_plist_uses_startcalendarinterval_so_a_missed_run_fires_on_wake() -> None:
    """The plist must schedule via ``StartCalendarInterval`` and set nothing that suppresses wake-catch-up.

    WHY: launchd runs a MISSED ``StartCalendarInterval`` job on next wake — that catch-up is
    the entire reason for switching off cron (which silently skips). A future edit that
    swapped to ``StartInterval`` (fires N seconds after load, no calendar catch-up) or set
    ``Disabled`` would silently reintroduce cron's skip behavior. We pin the governing keys:
    ``StartCalendarInterval`` present at 07:00, and ``StartInterval`` / ``Disabled`` absent —
    so that regression fails here.
    """
    parsed = plistlib.loads(scheduler.generate_launchd_plist(repo_path=Path("/repo"), minute=0, hour=7).encode())

    assert parsed["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}  # the wake-catch-up key
    assert "StartInterval" not in parsed  # would lose calendar catch-up
    assert parsed.get("Disabled", False) is False  # would suppress all runs, incl. catch-up


def test_plist_states_why_the_permissions_flag_is_carried() -> None:
    """The plist itself must state WHY ``--dangerously-skip-permissions`` is present.

    WHY: the flag is a deliberate carry-over of cron's headless behavior, not a silent port
    (acceptance criterion). A human opening the plist must see both the flag AND the reason,
    so a future maintainer doesn't strip it as "dangerous-looking" and break the headless run.
    We assert the flag appears and the rationale comment explains the headless-prompt reason.
    """
    plist_text = scheduler.generate_launchd_plist(repo_path=Path("/repo"))

    assert "--dangerously-skip-permissions" in plist_text
    assert "headless" in plist_text and "permission prompt" in plist_text


def test_install_daily_scheduler_is_idempotent_bootout_before_bootstrap(tmp_path: Path) -> None:
    """Installing twice leaves exactly one agent, and each install boots the old one out first.

    WHY: re-running setup must never leave two ``com.orbit.daily`` agents racing. Idempotency
    is BY LABEL: the installer boots out the existing agent BEFORE bootstrapping the new one.
    We install twice against a tmp LaunchAgents dir and assert exactly one plist file remains
    and the launchctl call order is bootout→bootstrap on BOTH runs (so nothing is duplicated).
    """
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_launchctl = _FakeLaunchctl()

    for _ in range(2):
        assert scheduler.install_daily_scheduler(
            "0 7 * * *",
            repo_path=tmp_path,
            launch_agents_dir=launch_agents_dir,
            launchctl_runner=fake_launchctl,
            crontab_runner=_FakeCrontab(),
        )

    assert list(launch_agents_dir.glob("*.plist")) == [launch_agents_dir / "com.orbit.daily.plist"]
    # Every install boots the prior agent out before bootstrapping — never a bare duplicate.
    assert fake_launchctl.verbs == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_install_daily_scheduler_migrates_orbit_cron_and_keeps_foreign_jobs(tmp_path: Path) -> None:
    """Install retires the legacy orbit crontab line but leaves unrelated cron jobs untouched.

    WHY: upgrading from the cron era must not leave two schedulers racing (the Phase-5
    double-orchestrator incident), so the orbit-tagged line is removed — but the user's OWN
    cron jobs must survive. We seed a crontab with the orbit line plus two foreign jobs and
    assert only the orbit line disappears.
    """
    orbit_line = f'0 9 * * * cd /repo && claude -p "/orbit" {_ORBIT_CRON_MARKER}'
    fake_crontab = _FakeCrontab(stored=f"{orbit_line}\n30 8 * * * my-backup-job\n15 2 * * 0 my-weekly-job\n")

    assert scheduler.install_daily_scheduler(
        "0 7 * * *",
        repo_path=tmp_path,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=fake_crontab,
    )

    assert _ORBIT_CRON_MARKER not in fake_crontab.stored  # the orbit line is gone
    assert "my-backup-job" in fake_crontab.stored  # foreign job #1 preserved
    assert "my-weekly-job" in fake_crontab.stored  # foreign job #2 preserved


def test_install_daily_scheduler_clean_install_with_no_prior_scheduling(tmp_path: Path) -> None:
    """A user with no crontab and no agent installs cleanly, touching neither on the cron side.

    WHY: a brand-new machine has no crontab (``crontab -l`` exits non-zero "no crontab for
    user") and no agent. That must be a clean install, not an error, and the absent crontab
    must NOT be rewritten (nothing to migrate). We assert the agent lands and the crontab is
    never written.
    """
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_crontab = _FakeCrontab(stored="", read_returncode=1, read_stderr="no crontab for testuser")

    assert scheduler.install_daily_scheduler(
        "0 7 * * *",
        repo_path=tmp_path,
        launch_agents_dir=launch_agents_dir,
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=fake_crontab,
    )

    assert (launch_agents_dir / "com.orbit.daily.plist").exists()
    assert fake_crontab.writes == []  # no crontab to migrate — left untouched


def test_install_daily_scheduler_fails_soft_when_launchctl_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing/sandboxed ``launchctl`` fails SOFT (return False + log) and skips cron migration.

    WHY: a sandboxed run with no ``launchctl`` must not raise — setup falls back to printed
    instructions. Critically, the legacy cron line must NOT be retired when launchd didn't
    actually install, or the user would be left with NO scheduler. We make launchctl raise and
    assert install returns False, logs the failure, and never rewrote the (orbit-tagged) crontab.
    """
    fake_crontab = _FakeCrontab(stored=f"0 9 * * * old {_ORBIT_CRON_MARKER}\n")

    installed = scheduler.install_daily_scheduler(
        "0 7 * * *",
        repo_path=tmp_path,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(raise_on=FileNotFoundError("launchctl: command not found")),
        crontab_runner=fake_crontab,
    )

    assert installed is False
    assert "setup_launchd_install_failed" in capsys.readouterr().out
    assert fake_crontab.writes == []  # cron NOT retired — launchd never came up


def test_remove_orbit_cron_entry_refuses_to_clobber_an_unreadable_crontab(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A genuine crontab read error must fail soft AND never rewrite the crontab.

    WHY: if ``crontab -l`` fails for a real reason (e.g. permissions) we do NOT know the
    current crontab, so rewriting it would clobber the user's jobs. The migration must refuse
    to write, return False, and log why — matching ``install_cron_entry``'s posture.
    """
    fake_crontab = _FakeCrontab(stored="", read_returncode=1, read_stderr="crontab: permission denied")

    migrated = scheduler._remove_orbit_cron_entry(crontab_runner=fake_crontab)

    assert migrated is False
    assert fake_crontab.writes == []  # never overwrote the crontab we couldn't read
    assert "setup_cron_migration_skipped" in capsys.readouterr().out


def test_calendar_from_schedule_rejects_a_schedule_launchd_cannot_express() -> None:
    """A schedule whose minute/hour aren't fixed integers must fail LOUD, not silently mis-schedule.

    WHY: launchd's ``StartCalendarInterval`` needs fixed clock fields. A stepped/ranged cron
    (``*/15 9-17 ...``) or a short 4-field expression can't be expressed; building a plist from
    it anyway would silently schedule the run at the wrong time (or 00:00). The helper must raise.
    """
    with pytest.raises(ValueError):
        scheduler._calendar_from_schedule("*/15 9-17 * * 1-5")
    with pytest.raises(ValueError):
        scheduler._calendar_from_schedule("0 7 * *")  # only 4 fields


# ---------------------------------------------------------------------------
# Legacy cron surface (moved verbatim from tests/test_setup_wizard.py — assertions intact).
# ---------------------------------------------------------------------------


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
