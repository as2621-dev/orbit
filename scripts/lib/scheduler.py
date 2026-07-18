"""Daily-run scheduler install: a launchd LaunchAgent (wake-proof) that replaces cron.

The daily 7am run is scheduled as a launchd LaunchAgent (``com.orbit.daily``) under
``~/Library/LaunchAgents`` with a ``StartCalendarInterval`` at 07:00. launchd runs a job
that was MISSED while the Mac slept on next wake; cron silently skips it — that catch-up is
the whole reason for the switch (PRD stories #9-#12, stack-notes.md M5-M7).

Install mirrors the legacy cron installer's shape exactly — generate -> idempotent install
-> fail-soft:

  * :func:`generate_launchd_plist` builds the plist deterministically (PURE).
  * :func:`install_daily_scheduler` writes it, boots out any existing agent before
    bootstrapping the new one (idempotent by label), and — only AFTER launchd is confirmed
    live — retires the legacy ``# orbit-daily-digest`` crontab line so no user ends up with
    two schedulers racing (the Phase-5 double-orchestrator incident).
  * A sandboxed / ``launchctl``-less environment fails SOFT: install returns ``False`` and
    the caller prints :func:`manual_setup_instructions`, so setup still completes.

The legacy cron surface was moved here verbatim from ``setup_wizard.py`` (per issue #3's
extraction mandate) and splits into two roles. The migration actively REUSES
:data:`_ORBIT_CRON_MARKER`, :func:`_existing_crontab_lines`, and :func:`_default_crontab_runner`
to read/match the crontab the same way the old installer did. :func:`generate_cron_entry` and
:func:`install_cron_entry` no longer have a production caller (new installs use launchd) but
are RETAINED as the characterized legacy cron surface, kept green by their tests (now in
``tests/test_scheduler.py``) so the format the migration matches against stays pinned.

Injection discipline (so tests never touch the real crontab / ``~/Library/LaunchAgents`` /
``launchctl``): the crontab and launchctl subprocess boundaries are injectable ``Callable``
aliases with real defaults, faked in tests. Never ``shell=True``. Stdlib-only.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.scheduler`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors config.py / setup_wizard.py's sys.path pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log, subproc  # noqa: E402
from lib.config import DEFAULT_SCHEDULE, is_valid_cron_expression  # noqa: E402

# ---------------------------------------------------------------------------
# Legacy cron surface (moved verbatim from setup_wizard.py). Retained because the
# launchd migration reads/matches the crontab against this exact marker + interpretation.
# ---------------------------------------------------------------------------

# The default scheduler command, per brief §8.3 step 5 / §2 (OS cron -> claude -p "/orbit").
# ``{repo}`` is the directory the user runs Orbit from; the cron line cds there first so
# the relative ``orbit.config.json`` resolves the same way a manual run would.
_DEFAULT_CRON_COMMAND_TEMPLATE: str = 'cd {repo} && claude -p --dangerously-skip-permissions "/orbit"'

# Trailing comment tag on Orbit's crontab line. ``install_cron_entry`` matches any existing
# line CONTAINING this marker for replacement, and the launchd migration matches it for
# removal, so the single Orbit line is always found in place instead of duplicated/orphaned.
_ORBIT_CRON_MARKER: str = "# orbit-daily-digest"

# Timeout (seconds) for a single ``crontab`` subprocess. Reading/writing a crontab is a
# fast local op; a small ceiling keeps a hung ``crontab`` binary from stalling setup.
_CRONTAB_TIMEOUT_SECONDS: int = 15

# The injectable subprocess boundary for crontab I/O: called as
# ``crontab_runner(command, stdin_text)`` and returns a :class:`lib.subproc.SubprocResult`.
# Production wires :func:`_default_crontab_runner`; tests inject a scripted fake so no test
# ever touches the real user crontab (mirrors bird_x.py's ``subproc`` injection posture).
CrontabRunner = Callable[[list[str], Optional[str]], subproc.SubprocResult]


def generate_cron_entry(schedule: str, command: Optional[str] = None, *, repo_path: Optional[Path] = None) -> str:
    """Build a syntactically valid crontab line ``"<cron_expr> <command>"`` (PURE, Rule 5).

    Deterministic string assembly — no I/O, no model. Validates ``schedule`` via
    :func:`lib.config.is_valid_cron_expression` and FAILS LOUD (Rule 12) with a clear
    :class:`ValueError` if it is malformed, so a broken schedule never reaches the user's
    crontab. The default ``command`` reflects brief §8.3 step 5 / §2 — it ``cd``s into the
    repo and invokes ``claude -p "/orbit"`` — with the repo directory injectable
    (``repo_path``) so the line is testable; an explicit ``command`` overrides it entirely.

    Args:
        schedule: A 5-field cron expression (e.g. ``"0 7 * * *"`` for 7am daily).
        command: An explicit command to run; when None, the default
            ``cd <repo> && claude -p "/orbit"`` is built from ``repo_path``.
        repo_path: The directory the cron command ``cd``s into; defaults to the current
            working directory. Ignored when an explicit ``command`` is given.

    Returns:
        A single crontab line: the validated cron expression, a space, then the command.

    Raises:
        ValueError: If ``schedule`` is not a syntactically valid 5-field cron expression.

    Example:
        >>> generate_cron_entry("0 7 * * *", repo_path=Path("/home/me/orbit"))
        '0 7 * * * cd /home/me/orbit && claude -p --dangerously-skip-permissions "/orbit"'
    """
    if not is_valid_cron_expression(schedule):
        log.log_error(
            "setup_invalid_cron_entry_schedule",
            fix_suggestion=(
                "Pass a 5-field cron expression (minute hour day-of-month month day-of-week), "
                "e.g. '0 7 * * *' for 7am daily."
            ),
            invalid_schedule=schedule,
        )
        raise ValueError(
            f"Cannot build a cron entry from {schedule!r}: not a valid 5-field cron "
            "expression. Expected e.g. '0 7 * * *'."
        )

    if command is None:
        resolved_repo = repo_path if repo_path is not None else Path.cwd()
        command = _DEFAULT_CRON_COMMAND_TEMPLATE.format(repo=resolved_repo)

    return f"{schedule} {command}"


def _default_crontab_runner(command: list[str], stdin_text: Optional[str]) -> subproc.SubprocResult:
    """Run one ``crontab`` command via subprocess (the production :data:`CrontabRunner`).

    Reads (``crontab -l``, ``stdin_text=None``) or writes (``crontab -`` with the new
    crontab text piped on stdin). Returns a :class:`lib.subproc.SubprocResult` so
    :func:`install_cron_entry` inspects ``returncode``/``stdout``/``stderr`` uniformly.
    Argument-list form (never ``shell=True``) so nothing is shell-interpolated.

    Args:
        command: The crontab argv, e.g. ``["crontab", "-l"]`` or ``["crontab", "-"]``.
        stdin_text: Text piped to the process stdin (the new crontab body) on a write,
            or None on a read.

    Returns:
        A :class:`lib.subproc.SubprocResult` with the process return code and captured
        stdout/stderr.

    Raises:
        OSError: If the ``crontab`` binary cannot be spawned (missing/not on PATH). The
            caller catches this and fails soft.
        subprocess.SubprocessError: On a subprocess-level failure (e.g. timeout).
    """
    completed = subprocess.run(
        command,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=_CRONTAB_TIMEOUT_SECONDS,
        check=False,
    )
    return subproc.SubprocResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _existing_crontab_lines(read_result: subproc.SubprocResult) -> Optional[list[str]]:
    """Interpret a ``crontab -l`` result into the current crontab lines (or a read failure).

    A zero return code yields the crontab's lines verbatim. A non-zero return code is
    ambiguous: ``crontab -l`` exits non-zero both for the benign "no crontab for user"
    first-run case AND for a genuine error. We treat an empty stderr or a "no crontab"
    stderr as an EMPTY crontab (a safe fresh start), but signal a genuine read failure
    (``None``) for anything else — so callers fail soft rather than piping a fresh crontab
    that could clobber an existing one they merely failed to read.

    Args:
        read_result: The result of the ``crontab -l`` invocation.

    Returns:
        The existing crontab lines (possibly empty) on a readable crontab, or ``None``
        when the read genuinely failed and the current crontab is unknown.
    """
    if read_result.returncode == 0:
        return read_result.stdout.splitlines()
    stderr_lower = read_result.stderr.lower()
    if not read_result.stderr.strip() or "no crontab" in stderr_lower:
        return []
    return None


def install_cron_entry(cron_entry: str, *, crontab_runner: CrontabRunner = _default_crontab_runner) -> bool:
    """Install ``cron_entry`` into the user's crontab idempotently, failing SOFT on error.

    Reads the current crontab via ``crontab_runner``, DROPS any existing line containing
    :data:`_ORBIT_CRON_MARKER` (so a re-run replaces the single Orbit line rather than
    appending a duplicate), appends ``cron_entry`` tagged with the trailing marker, and
    pipes the result back via ``crontab -``. All crontab I/O goes through the injected
    ``crontab_runner`` so tests never touch the real crontab.

    Fail-soft posture (Rule 12 surfaced, not swallowed): a missing ``crontab`` binary, a
    genuinely unreadable crontab, or a non-zero write all log
    ``setup_cron_install_failed`` with a ``fix_suggestion`` and return ``False`` — the
    caller then falls back to printing the entry for manual pasting, so a sandboxed / CI
    run still completes setup.

    Args:
        cron_entry: The crontab line to install (untagged; the marker is appended here),
            e.g. ``'0 7 * * * cd /repo && claude -p "/orbit"'``.
        crontab_runner: The injectable crontab subprocess boundary; defaults to
            :func:`_default_crontab_runner`. Tests inject a scripted fake.

    Returns:
        ``True`` when the crontab was updated, ``False`` on any failure (caller degrades
        to the print-and-paste fallback).

    Example:
        >>> install_cron_entry('0 7 * * * echo hi', crontab_runner=my_fake)  # doctest: +SKIP
        True
    """
    try:
        read_result = crontab_runner(["crontab", "-l"], None)
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_error(
            "setup_cron_install_failed",
            fix_suggestion=(
                "Could not read the current crontab. Ensure the 'crontab' binary is installed "
                "and on PATH, then add the printed cron line manually via `crontab -e`."
            ),
            phase="read",
            error_message=str(exc),
        )
        return False

    existing_lines = _existing_crontab_lines(read_result)
    if existing_lines is None:
        log.log_error(
            "setup_cron_install_failed",
            fix_suggestion=(
                "Reading the current crontab failed unexpectedly; refusing to overwrite it. "
                "Add the printed cron line manually via `crontab -e`."
            ),
            phase="read",
            returncode=read_result.returncode,
            stderr=read_result.stderr.strip(),
        )
        return False

    tagged_entry = f"{cron_entry} {_ORBIT_CRON_MARKER}"
    kept_lines = [line for line in existing_lines if _ORBIT_CRON_MARKER not in line]
    kept_lines.append(tagged_entry)
    new_crontab = "\n".join(kept_lines) + "\n"

    try:
        write_result = crontab_runner(["crontab", "-"], new_crontab)
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_error(
            "setup_cron_install_failed",
            fix_suggestion=(
                "Could not write the updated crontab. Ensure the 'crontab' binary is installed "
                "and on PATH, then add the printed cron line manually via `crontab -e`."
            ),
            phase="write",
            error_message=str(exc),
        )
        return False

    if write_result.returncode != 0:
        log.log_error(
            "setup_cron_install_failed",
            fix_suggestion=(
                "The 'crontab -' write returned non-zero; the schedule was not installed. "
                "Add the printed cron line manually via `crontab -e`."
            ),
            phase="write",
            returncode=write_result.returncode,
            stderr=write_result.stderr.strip(),
        )
        return False

    log.log_info("setup_cron_installed", cron_marker=_ORBIT_CRON_MARKER)
    return True


# ---------------------------------------------------------------------------
# launchd LaunchAgent surface (the wake-proof daily-run scheduler).
# ---------------------------------------------------------------------------

# Reverse-DNS launchd label. Install is idempotent BY THIS LABEL: a re-install boots the
# existing agent out (by this label) before bootstrapping the new one, so exactly one
# ``com.orbit.daily`` agent ever exists.
_ORBIT_LAUNCHD_LABEL: str = "com.orbit.daily"

# Where user LaunchAgents live. Injectable in ``install_daily_scheduler`` so no test ever
# writes into the real directory.
DEFAULT_LAUNCH_AGENTS_DIR: Path = Path.home() / "Library" / "LaunchAgents"

# A sane PATH for the headless agent: launchd hands a job a minimal environment, so the
# downstream ``claude`` / ``yt-dlp`` / ``node`` subprocesses need common bin dirs on PATH
# to resolve. Homebrew (Apple-silicon + Intel) first, then the system defaults.
_LAUNCHD_ENV_PATH: str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Timeout (seconds) for a single ``launchctl`` subprocess. bootout/bootstrap are fast local
# ops; a small ceiling keeps a hung ``launchctl`` from stalling setup.
_LAUNCHCTL_TIMEOUT_SECONDS: int = 15

# Why the plist carries ``--dangerously-skip-permissions`` — stated in BOTH the generated
# plist (as an XML comment) and the ``setup_launchd_installed`` log, per the acceptance
# criterion that this deliberate carry-over is not a silent port.
_PERMISSIONS_NOTE: str = (
    "--dangerously-skip-permissions is carried in the plist ProgramArguments because the "
    "07:00 run is headless (no TTY); without it, `claude -p` blocks on an interactive "
    "permission prompt and the digest never sends. Deliberate carry-over of the prior cron "
    "behavior."
)
# The plist comment can't contain a literal "--" (illegal inside an XML comment), so it
# refers to the flag by name without the leading dashes; the flag itself appears verbatim in
# the ProgramArguments strings right below the comment.
_PLIST_PERMISSIONS_COMMENT: str = (
    "<!-- The ProgramArguments carry the dangerously-skip-permissions flag because the "
    "07:00 run is headless (no TTY); without it, `claude -p` blocks on an interactive "
    "permission prompt and the digest never sends. Deliberate carry-over of prior cron "
    "behavior. -->"
)

# The injectable subprocess boundary for launchctl I/O: called as
# ``launchctl_runner(command)`` and returns a :class:`lib.subproc.SubprocResult`. launchctl
# takes only argv (no stdin), so this alias is narrower than :data:`CrontabRunner`.
# Production wires :func:`_default_launchctl_runner`; tests inject a scripted fake so no test
# ever runs a real ``launchctl`` or touches the real launchd domain.
LaunchctlRunner = Callable[[list[str]], subproc.SubprocResult]


def _default_launchctl_runner(command: list[str]) -> subproc.SubprocResult:
    """Run one ``launchctl`` command via subprocess (the production :data:`LaunchctlRunner`).

    Argument-list form (never ``shell=True``) so nothing is shell-interpolated. Returns a
    :class:`lib.subproc.SubprocResult` so :func:`_install_launchd_agent` inspects the
    return code uniformly.

    Args:
        command: The launchctl argv, e.g. ``["launchctl", "bootstrap", "gui/501", "<plist>"]``.

    Returns:
        A :class:`lib.subproc.SubprocResult` with the process return code and captured
        stdout/stderr.

    Raises:
        OSError: If the ``launchctl`` binary cannot be spawned (missing / sandboxed). The
            caller catches this and fails soft.
        subprocess.SubprocessError: On a subprocess-level failure (e.g. timeout).
    """
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=_LAUNCHCTL_TIMEOUT_SECONDS,
        check=False,
    )
    return subproc.SubprocResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _calendar_from_schedule(schedule: str) -> tuple[int, int]:
    """Extract the fixed ``(minute, hour)`` a launchd ``StartCalendarInterval`` needs.

    launchd's calendar interval schedules on fixed clock fields, so the cron ``schedule``
    must carry an integer minute and hour (the fixed-7am default ``"0 7 * * *"`` does).
    Fails LOUD (Rule 12) if the schedule is malformed or uses ranges/steps/``*`` in the
    minute or hour that a calendar interval cannot express.

    Args:
        schedule: A 5-field cron expression whose minute + hour are plain integers.

    Returns:
        ``(minute, hour)`` as integers.

    Raises:
        ValueError: If ``schedule`` is not a valid cron expression, or its minute/hour are
            not fixed integers.
    """
    if not is_valid_cron_expression(schedule):
        log.log_error(
            "setup_invalid_launchd_schedule",
            fix_suggestion="Pass a 5-field cron expression with a fixed minute and hour, e.g. '0 7 * * *'.",
            invalid_schedule=schedule,
        )
        raise ValueError(f"Cannot build a launchd calendar interval from {schedule!r}: not a valid cron expression.")

    fields = schedule.split()
    try:
        return int(fields[0]), int(fields[1])
    except ValueError as exc:
        log.log_error(
            "setup_invalid_launchd_schedule",
            fix_suggestion=(
                "launchd's StartCalendarInterval needs a fixed minute and hour; ranges, steps, "
                "and '*' can't be expressed. Use e.g. '0 7 * * *'."
            ),
            invalid_schedule=schedule,
        )
        raise ValueError(
            f"Cannot build a launchd calendar interval from {schedule!r}: minute and hour "
            "must be fixed integers."
        ) from exc


def _orbit_program_arguments(*, claude_executable: Optional[str] = None) -> list[str]:
    """Build the plist ``ProgramArguments`` argv (no shell — an argument list).

    Resolves ``claude`` to an absolute path via ``shutil.which`` at generation time so the
    headless agent doesn't depend on launchd's minimal PATH to find it (falls back to the
    bare name when unresolved, e.g. in a sandbox). Carries
    ``--dangerously-skip-permissions`` deliberately — see :data:`_PERMISSIONS_NOTE`.

    Args:
        claude_executable: Explicit path to the ``claude`` binary; when None it is resolved
            via ``shutil.which`` (injectable so tests are deterministic).

    Returns:
        The ProgramArguments argv, e.g. ``["/opt/homebrew/bin/claude", "-p",
        "--dangerously-skip-permissions", "/orbit"]``.
    """
    resolved_claude = claude_executable or shutil.which("claude") or "claude"
    return [resolved_claude, "-p", "--dangerously-skip-permissions", "/orbit"]


def _annotate_plist_with_permissions_note(plist_body: str) -> str:
    """Insert the ``--dangerously-skip-permissions`` rationale as an XML comment in the plist.

    ``plistlib`` can't emit comments, so we splice one in after the ``<plist>`` open. XML
    parsers (including ``plistlib.loads``) ignore comments, so this is human-only annotation
    that leaves the parsed plist unchanged.

    Args:
        plist_body: The serialized plist XML from ``plistlib.dumps``.

    Returns:
        The plist XML with the rationale comment inserted (unchanged if the marker is absent).
    """
    marker = '<plist version="1.0">\n'
    if marker in plist_body:
        return plist_body.replace(marker, marker + _PLIST_PERMISSIONS_COMMENT + "\n", 1)
    return plist_body


def generate_launchd_plist(
    *, repo_path: Optional[Path] = None, minute: int = 0, hour: int = 7, claude_executable: Optional[str] = None
) -> str:
    """Build the ``com.orbit.daily`` LaunchAgent plist XML (PURE — no I/O beyond which).

    The plist schedules via ``StartCalendarInterval`` at ``hour``:``minute`` — the key that
    gives launchd its wake-catch-up: a run missed while the Mac slept fires on next wake
    (cron silently skips). It deliberately does NOT set ``StartInterval`` or ``Disabled`` —
    either would reintroduce cron's skip behavior; that invariant is pinned in
    ``tests/test_scheduler.py``. ``RunAtLoad`` is left unset so bootstrapping the agent at
    setup time does not immediately fire a digest.

    Args:
        repo_path: The directory the agent runs in (``WorkingDirectory``), so ``/orbit``
            resolves ``orbit.config.json`` the same way a manual run does; defaults to cwd.
        minute: The ``StartCalendarInterval`` minute (0 for the fixed 7am default).
        hour: The ``StartCalendarInterval`` hour (7 for the fixed 7am default).
        claude_executable: Explicit ``claude`` path (injectable for deterministic tests).

    Returns:
        The plist XML as a UTF-8 string, with the permissions-flag rationale as a comment.

    Example:
        >>> "com.orbit.daily" in generate_launchd_plist(repo_path=Path("/home/me/orbit"))
        True
    """
    resolved_repo = repo_path if repo_path is not None else Path.cwd()
    plist_dict: dict[str, object] = {
        "Label": _ORBIT_LAUNCHD_LABEL,
        "ProgramArguments": _orbit_program_arguments(claude_executable=claude_executable),
        "WorkingDirectory": str(resolved_repo),
        "EnvironmentVariables": {"PATH": _LAUNCHD_ENV_PATH},
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
    }
    plist_body = plistlib.dumps(plist_dict).decode("utf-8")
    return _annotate_plist_with_permissions_note(plist_body)


def default_plist_path(launch_agents_dir: Optional[Path] = None) -> Path:
    """Resolve the ``com.orbit.daily.plist`` path under the LaunchAgents dir.

    Args:
        launch_agents_dir: The LaunchAgents directory; defaults to
            :data:`DEFAULT_LAUNCH_AGENTS_DIR` (``~/Library/LaunchAgents``).

    Returns:
        The absolute path to ``com.orbit.daily.plist``.
    """
    resolved_dir = launch_agents_dir if launch_agents_dir is not None else DEFAULT_LAUNCH_AGENTS_DIR
    return resolved_dir / f"{_ORBIT_LAUNCHD_LABEL}.plist"


def _install_launchd_agent(
    plist_contents: str, *, launch_agents_dir: Path, launchctl_runner: LaunchctlRunner
) -> bool:
    """Write the plist and load it idempotently (bootout before bootstrap), failing SOFT.

    Writes ``com.orbit.daily.plist`` into ``launch_agents_dir``, then BOOTS OUT any
    already-loaded ``com.orbit.daily`` agent BEFORE bootstrapping the freshly written one —
    so re-running setup leaves exactly one agent (idempotent by label), never two. A
    non-zero bootout is expected on a fresh install (nothing loaded) and is NOT treated as
    an error; only a bootstrap failure is.

    Fail-soft (Rule 12 surfaced, not swallowed): a missing/sandboxed ``launchctl`` (the
    runner raises), an unwritable LaunchAgents dir, or a non-zero bootstrap all log
    ``setup_launchd_install_failed`` with a ``fix_suggestion`` and return ``False`` — the
    caller then prints :func:`manual_setup_instructions` so setup still completes.

    Args:
        plist_contents: The plist XML to write (from :func:`generate_launchd_plist`).
        launch_agents_dir: The directory to write the plist into (tmp path in tests).
        launchctl_runner: The injectable launchctl boundary; tests inject a scripted fake.

    Returns:
        ``True`` when the agent was written and bootstrapped, ``False`` on any failure.
    """
    plist_path = default_plist_path(launch_agents_dir)
    try:
        launch_agents_dir.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_contents, encoding="utf-8")
    except OSError as exc:
        log.log_error(
            "setup_launchd_install_failed",
            fix_suggestion=(
                "Could not write the LaunchAgent plist; the LaunchAgents directory may be "
                "sandboxed or read-only. Follow the printed manual instructions instead."
            ),
            phase="write",
            error_message=str(exc),
        )
        return False

    user_id = os.getuid()
    service_target = f"gui/{user_id}/{_ORBIT_LAUNCHD_LABEL}"
    domain_target = f"gui/{user_id}"

    # Bootout any already-loaded agent FIRST so a re-install replaces rather than duplicates.
    # A non-zero return just means nothing was loaded (fresh install) — not an error, so its
    # return code is intentionally not inspected.
    try:
        launchctl_runner(["launchctl", "bootout", service_target])
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_error(
            "setup_launchd_install_failed",
            fix_suggestion=(
                "Could not run `launchctl bootout`; ensure `launchctl` is available (not "
                "sandboxed). Follow the printed manual instructions instead."
            ),
            phase="bootout",
            error_message=str(exc),
        )
        return False

    try:
        bootstrap_result = launchctl_runner(["launchctl", "bootstrap", domain_target, str(plist_path)])
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_error(
            "setup_launchd_install_failed",
            fix_suggestion=(
                "Could not run `launchctl bootstrap`; ensure `launchctl` is available (not "
                "sandboxed). Follow the printed manual instructions instead."
            ),
            phase="bootstrap",
            error_message=str(exc),
        )
        return False

    if bootstrap_result.returncode != 0:
        log.log_error(
            "setup_launchd_install_failed",
            fix_suggestion=(
                "`launchctl bootstrap` returned non-zero; the agent was not loaded. Follow "
                "the printed manual instructions to load it with `launchctl bootstrap`."
            ),
            phase="bootstrap",
            returncode=bootstrap_result.returncode,
            stderr=bootstrap_result.stderr.strip(),
        )
        return False

    log.log_info(
        "setup_launchd_installed",
        launchd_label=_ORBIT_LAUNCHD_LABEL,
        plist_path=str(plist_path),
        permissions_note=_PERMISSIONS_NOTE,
    )
    return True


def _remove_orbit_cron_entry(*, crontab_runner: CrontabRunner = _default_crontab_runner) -> bool:
    """Retire the legacy ``# orbit-daily-digest`` crontab line, preserving unrelated jobs.

    The launchd migration: drop ONLY the orbit-tagged line (matched on
    :data:`_ORBIT_CRON_MARKER`) and pipe the rest back, so unrelated user cron jobs survive
    untouched. A crontab with no orbit line (fresh user, or already migrated) is left
    entirely alone — the crontab is never rewritten when there is nothing to remove.

    Fail-soft: an absent ``crontab`` binary or a genuinely unreadable crontab logs
    ``setup_cron_migration_skipped`` and returns ``False`` WITHOUT writing — refusing to
    clobber a crontab we couldn't read (matching :func:`install_cron_entry`'s posture). A
    migration failure is non-fatal: launchd is already the live scheduler.

    Args:
        crontab_runner: The injectable crontab boundary; tests inject a scripted fake.

    Returns:
        ``True`` when the orbit line was removed (or there was nothing to remove),
        ``False`` when the crontab could not be read/written and was left untouched.
    """
    try:
        read_result = crontab_runner(["crontab", "-l"], None)
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_warning(
            "setup_cron_migration_skipped",
            fix_suggestion=(
                "Could not read the crontab to retire the legacy orbit line; remove it "
                "manually with `crontab -e` if present. launchd is already scheduled."
            ),
            phase="read",
            error_message=str(exc),
        )
        return False

    existing_lines = _existing_crontab_lines(read_result)
    if existing_lines is None:
        log.log_warning(
            "setup_cron_migration_skipped",
            fix_suggestion=(
                "Reading the crontab failed unexpectedly; refusing to rewrite it. Remove the "
                "legacy `# orbit-daily-digest` line manually with `crontab -e` if present."
            ),
            phase="read",
            returncode=read_result.returncode,
            stderr=read_result.stderr.strip(),
        )
        return False

    kept_lines = [line for line in existing_lines if _ORBIT_CRON_MARKER not in line]
    if len(kept_lines) == len(existing_lines):
        # No orbit line to retire — leave the user's crontab entirely untouched.
        return True

    new_crontab = ("\n".join(kept_lines) + "\n") if kept_lines else ""
    try:
        write_result = crontab_runner(["crontab", "-"], new_crontab)
    except (OSError, subprocess.SubprocessError) as exc:
        log.log_warning(
            "setup_cron_migration_skipped",
            fix_suggestion="Could not write the crontab to retire the legacy orbit line; remove it manually.",
            phase="write",
            error_message=str(exc),
        )
        return False

    if write_result.returncode != 0:
        log.log_warning(
            "setup_cron_migration_skipped",
            fix_suggestion="The crontab write returned non-zero; retire the legacy orbit line manually.",
            phase="write",
            returncode=write_result.returncode,
            stderr=write_result.stderr.strip(),
        )
        return False

    log.log_info("setup_cron_migrated", cron_marker=_ORBIT_CRON_MARKER)
    return True


def install_daily_scheduler(
    schedule: str,
    *,
    repo_path: Optional[Path] = None,
    launch_agents_dir: Optional[Path] = None,
    launchctl_runner: LaunchctlRunner = _default_launchctl_runner,
    crontab_runner: CrontabRunner = _default_crontab_runner,
) -> bool:
    """Install the daily launchd agent and migrate the legacy cron entry, failing SOFT.

    The single seam the wizard calls in setup step 5 (replacing the cron install). It:

      1. Builds the ``com.orbit.daily`` plist for ``schedule`` (fixed 7am).
      2. Writes + loads it idempotently (bootout before bootstrap — one agent by label).
      3. Only AFTER launchd is confirmed live, retires the legacy orbit crontab line so no
         user is ever left with no scheduler (migration is best-effort — its failure is
         logged but does not fail the install, since launchd is already scheduled).

    Returns ``False`` (never raises) when launchd could not be installed, so the caller
    prints :func:`manual_setup_instructions` and setup still completes.

    Args:
        schedule: The 5-field cron schedule (fixed 7am ``"0 7 * * *"``); its minute/hour
            drive the plist ``StartCalendarInterval``.
        repo_path: The agent's working directory; defaults to cwd.
        launch_agents_dir: The LaunchAgents directory; defaults to
            :data:`DEFAULT_LAUNCH_AGENTS_DIR`. Tests pass a tmp path.
        launchctl_runner: The injectable launchctl boundary; tests inject a scripted fake.
        crontab_runner: The injectable crontab boundary (for the migration); tests inject
            a scripted fake.

    Returns:
        ``True`` when the launchd agent was installed, ``False`` on any launchd failure.

    Raises:
        ValueError: If ``schedule`` cannot be expressed as a launchd calendar interval.
    """
    minute, hour = _calendar_from_schedule(schedule)
    resolved_dir = launch_agents_dir if launch_agents_dir is not None else DEFAULT_LAUNCH_AGENTS_DIR
    plist_contents = generate_launchd_plist(repo_path=repo_path, minute=minute, hour=hour)

    if not _install_launchd_agent(plist_contents, launch_agents_dir=resolved_dir, launchctl_runner=launchctl_runner):
        return False

    _remove_orbit_cron_entry(crontab_runner=crontab_runner)
    return True


def manual_setup_instructions(
    *, repo_path: Optional[Path] = None, launch_agents_dir: Optional[Path] = None, schedule: str = DEFAULT_SCHEDULE
) -> str:
    """Build the printed fallback: save-this-plist + the ``launchctl bootstrap`` command.

    Used when :func:`install_daily_scheduler` fails soft (sandboxed / no ``launchctl``), so
    the user can install the wake-proof agent by hand.

    Args:
        repo_path: The agent's working directory (baked into the plist); defaults to cwd.
        launch_agents_dir: The LaunchAgents directory the plist is saved to; defaults to
            :data:`DEFAULT_LAUNCH_AGENTS_DIR`.
        schedule: The 5-field cron schedule whose minute/hour set the calendar interval.

    Returns:
        A multi-line instruction string: where to save the plist, its contents, and the
        ``launchctl bootstrap`` command to load it.
    """
    minute, hour = _calendar_from_schedule(schedule)
    plist_path = default_plist_path(launch_agents_dir)
    plist_contents = generate_launchd_plist(repo_path=repo_path, minute=minute, hour=hour)
    user_id = os.getuid()
    return (
        f"1. Save the following to {plist_path}:\n\n"
        f"{plist_contents}\n"
        f"2. Load it:\n\n"
        f"  launchctl bootstrap gui/{user_id} {plist_path}\n"
    )
