"""DoD tests for the `/orbit --setup` wizard (Phase 6 / Sub-phase 2).

Per Rule 9, each test encodes WHY the behavior matters, constructed to FAIL on wrong
BUSINESS logic, not merely "returns something":

  1. The wizard, driven by MOCKED loaders + a mock LLM + scripted input, writes an
     ``orbit.config.json`` that LOADS BACK CLEANLY through ``lib.config.load_config`` and
     carries the chosen ``creator_weights``, seeded ``interests``, and ``schedule`` — fails
     if the wizard writes a shape the real loader rejects (onboarding would hand the user a
     config the pipeline then refuses), or drops the user's choices.
  2. The wizard auto-classifies via the EXISTING classify path — asserted by the injected
     ``llm_classifier`` being CALLED. Fails if a separate classifier is introduced (DoD:
     "no separate classifier"), which would diverge from how the daily run judges items.
  3. An X-loader ``XAuthError`` lets the wizard continue YouTube-only and still write a
     valid config — fails if an unconfigured optional source aborts setup (X is additive).
  4. Setup step 5 installs the wake-proof launchd agent (via the injected ``launchctl``) at
     the fixed 7am schedule, and degrades to printed manual plist instructions when
     ``launchctl`` is unavailable — the scheduler MECHANICS themselves are pinned in
     ``tests/test_scheduler.py``; here we prove step 5 is wired to that seam.

Loaders/LLM/input/IO/launchctl/crontab are all mocked — NO live calls.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable so ``from lib import ...`` resolves regardless
# of the working directory. Mirrors tests/test_config.py / test_orbit_stage0.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import paths  # noqa: E402
from lib.bird_x import Follow, XAuthError, persist_following  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.setup_wizard import run_setup_wizard  # noqa: E402
from lib.subproc import SubprocResult  # noqa: E402
from lib.youtube_yt import Subscription, persist_subscriptions  # noqa: E402


def _scripted_input(answers: list[str]) -> MagicMock:
    """Build an ``input_fn`` that returns the scripted answers in order.

    Extra prompts past the scripted list return "" (the wizard treats empty as the
    prompt's default), so a test only scripts the answers whose value it asserts on.
    """
    return MagicMock(side_effect=lambda _prompt: answers.pop(0) if answers else "")


class _FakeCrontab:
    """An in-memory ``crontab_runner`` double so no test touches the real user crontab.

    The wizard still routes the launchd cron-migration through a ``crontab_runner``; an
    empty fake makes the migration a clean no-op. Simulates ``crontab -l`` (read) and
    ``crontab -`` (write) against a stored crontab body.

    Attributes:
        stored: The current crontab body returned by subsequent reads.
        writes: Every crontab body written, in order.
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
    """An in-memory ``launchctl_runner`` double so setup never runs a real ``launchctl``.

    Records every argv in order and returns 0 by default; ``raise_on`` simulates a
    missing / sandboxed binary (the runner raising).

    Attributes:
        commands: Every launchctl argv received, in order.
        raise_on: An exception to raise instead of running (missing binary / sandbox).
    """

    def __init__(self, *, raise_on: BaseException | None = None) -> None:
        self.commands: list[list[str]] = []
        self.raise_on = raise_on

    def __call__(self, command: list[str]) -> SubprocResult:
        if self.raise_on is not None:
            raise self.raise_on
        self.commands.append(command)
        return SubprocResult(returncode=0, stdout="", stderr="")


def _fake_store() -> MagicMock:
    """A store double for the classify path: no prior override, persist is a no-op.

    The classify path reads ``get_classification`` (None => no user override, so it asks
    the LLM) and writes ``set_classification``. ``get_source`` returns None so the wizard's
    confirm step treats every creator as first-seen (auto-classify path, not the re-run
    stored-default path). Mocking it keeps auto-classify entirely offline — NO real per-user
    DB is touched (the directive's "IO mocked" rule).
    """
    fake = MagicMock()
    fake.get_classification.return_value = None
    fake.get_source.return_value = None
    return fake


def _real_store(tmp_path: Path) -> object:
    """Point the REAL store at a temp DB and initialize it; return the store module.

    Unlike :func:`_fake_store` (a MagicMock that keeps auto-classify offline), this drives the
    real SQLite store so a test can assert the PERSISTED category value — the acceptance bar is
    "assert the persisted value, not that a function was called". The LLM boundary is still
    mocked, so no live model call happens; only the DB is real (and thrown away with tmp_path).
    """
    import os

    db_path = tmp_path / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    store.init_db()
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
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
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
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
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
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
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
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )

    loaded = load_config(config_path)
    assert loaded.delivery["email_to"] == "me@example.com"


def test_wizard_installs_launchd_agent_at_fixed_7am(tmp_path: Path) -> None:
    """End-to-end: the wizard installs exactly one 7am ``com.orbit.daily`` launchd agent.

    WHY: this is the phase DoD — a scripted wizard run must auto-install (via the injected
    launchctl) the wake-proof 7am agent, and the written config must carry that same fixed
    schedule. Proves the install path is wired into step 5, the agent is scheduled for 07:00
    (not something the user typed), and re-install order is bootout-before-bootstrap (idempotent
    by label). The plist mechanics themselves are pinned in test_scheduler.py.
    """
    config_path = tmp_path / "orbit.config.json"
    launch_agents_dir = tmp_path / "LaunchAgents"
    fake_launchctl = _FakeLaunchctl()

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
        launch_agents_dir=launch_agents_dir,
        launchctl_runner=fake_launchctl,
        crontab_runner=_FakeCrontab(),
    )

    assert exit_code == 0
    plist_path = launch_agents_dir / "com.orbit.daily.plist"
    assert plist_path.exists()  # exactly one agent installed
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}  # the fixed 7am schedule
    assert load_config(config_path).schedule == "0 7 * * *"  # config carries it too
    # Idempotent by label: the existing agent is booted out before the new one is bootstrapped.
    verbs = [command[1] for command in fake_launchctl.commands]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_wizard_no_longer_prompts_for_a_schedule(tmp_path: Path) -> None:
    """The wizard must NOT ask the user for a schedule anymore (fixed 7am decision).

    WHY: the 2026-07-06 ruling made the schedule a fixed local-auto default, not a config
    knob. If a schedule prompt survived, a user could still set a non-7am schedule,
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
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )

    prompt_texts = [call.args[0].lower() for call in input_mock.call_args_list]
    assert not any("schedule" in text or "cron" in text for text in prompt_texts), (
        f"the wizard must not prompt for a schedule anymore; prompts were: {prompt_texts}"
    )
    assert load_config(config_path).schedule == "0 7 * * *"


def test_wizard_prints_manual_plist_when_launchctl_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing ``launchctl`` must degrade to printing manual plist instructions — setup still succeeds.

    WHY: a sandboxed/CI run where ``launchctl`` is absent must not abort onboarding. The
    wizard completes (exit 0), logs the failure, and prints the manual ``launchctl bootstrap``
    command plus the ``com.orbit.daily`` plist so the user can install the wake-proof agent
    themselves — mirroring the old cron print-and-paste fallback.
    """
    config_path = tmp_path / "orbit.config.json"
    fake_launchctl = _FakeLaunchctl(raise_on=FileNotFoundError("launchctl: command not found"))

    answers = ["chrome", "y", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_x", display_name="X Chan")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=_fake_store(),
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=fake_launchctl,
        crontab_runner=_FakeCrontab(),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "setup_launchd_install_failed" in output  # the failure was surfaced, not hidden
    assert "launchctl bootstrap" in output  # the manual load command
    assert "com.orbit.daily" in output  # the agent to install


def test_wizard_persists_user_flipped_noise_category(tmp_path: Path) -> None:
    """A channel the user flips to noise during setup is PERSISTED as noise (user-set).

    WHY (the discard bug): ``_confirm_categories`` returned a category map that nothing
    assigned — the wizard never wrote ``sources`` rows, so a user's noise mark evaporated the
    moment setup ended. We assert the PERSISTED value in the real store (not that a function was
    called): the row must carry ``category='noise'`` AND ``category_is_user_override=1`` so the
    mark both takes effect and survives later refreshes.
    """
    real_store = _real_store(tmp_path)
    config_path = tmp_path / "orbit.config.json"

    # cookie, confirm cat = 'n' (flip the auto 'signal' to noise), prioritize 'n', html, email.
    answers = ["chrome", "n", "n", "~/orbit/out/today.html", ""]

    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_flip", display_name="Flip Me")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=real_store,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )

    assert exit_code == 0
    row = real_store.get_source("youtube", "UC_flip")
    assert row is not None, "the wizard did not persist the source row at all"
    assert row["category"] == "noise", "the user's noise flip was not persisted"
    assert row["category_is_user_override"] == 1, "the persisted category was not marked user-set"


def test_user_category_survives_real_youtube_refresh(tmp_path: Path) -> None:
    """A setup-time noise mark survives the REAL YouTube source refresh (driven end-to-end).

    WHY (the clobber bug): the issue is explicit that this must exercise the real refresh path,
    not ``upsert_source`` in isolation. The user flips a channel to noise in setup; then
    ``persist_subscriptions`` — the weekly Stage-0 refresh, which re-upserts every channel with a
    hardcoded ``category='signal'`` — runs. Before the fix that reset the mark weekly; the row
    must still read noise.
    """
    real_store = _real_store(tmp_path)
    config_path = tmp_path / "orbit.config.json"
    answers = ["chrome", "n", "n", "~/orbit/out/today.html", ""]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_weekly", display_name="Weekly")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=real_store,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )
    assert real_store.get_source("youtube", "UC_weekly")["category"] == "noise"

    # The weekly Stage-0 refresh re-upserts the same channel with the hardcoded signal category.
    persist_subscriptions([Subscription(channel_id="UC_weekly", display_name="Weekly")])

    assert real_store.get_source("youtube", "UC_weekly")["category"] == "noise", (
        "the weekly YouTube refresh clobbered the user's noise mark"
    )


def test_user_category_survives_real_x_refresh(tmp_path: Path) -> None:
    """A setup-time noise mark survives the REAL X source refresh — the DAILY, unguarded one.

    WHY: the X refresh (``persist_following``) has no weekly cache guard, so it re-upserts every
    handle with ``category='signal'`` on EVERY run — it reset a user's noise mark daily, the more
    urgent of the two paths per the issue. The user flips an X handle to noise in setup; after a
    real ``persist_following`` the row must still read noise.
    """
    real_store = _real_store(tmp_path)
    config_path = tmp_path / "orbit.config.json"
    answers = ["chrome", "n", "n", "~/orbit/out/today.html", ""]

    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[]),
        x_loader=MagicMock(return_value=[Follow(creator_handle="daily_x", display_name="Daily X", rest_id="42")]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=real_store,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )
    assert real_store.get_source("x", "daily_x")["category"] == "noise"

    # The daily X refresh re-upserts the handle with the hardcoded signal category.
    persist_following([Follow(creator_handle="daily_x", display_name="Daily X", rest_id="42")])

    assert real_store.get_source("x", "daily_x")["category"] == "noise", (
        "the daily X refresh clobbered the user's noise mark"
    )


def test_wizard_initializes_and_migrates_the_db_before_persisting(tmp_path: Path) -> None:
    """Running --setup on a populated v1 DB migrates it and persists — it does NOT crash.

    WHY (the production path the other tests masked): the --setup path never calls init_db on
    its own (only the pipeline's Stage 0 does), so the wizard must initialize the store itself
    before it reads/writes ``sources``. This drives the user's real situation — an existing v1
    DB that predates the ``category_is_user_override`` column — WITHOUT pre-initializing it, and
    proves the wizard applies migration 2, preserves the existing row, and persists the new mark
    instead of raising ``no such column`` / ``KeyError``.
    """
    import os
    import sqlite3

    db_path = tmp_path / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path

    # Build a genuine populated v1 DB (baseline schema only, no migrations, no new column).
    seed_conn = sqlite3.connect(str(db_path))
    seed_conn.executescript(store.SCHEMA_ORBIT_V1)
    seed_conn.execute(
        "INSERT INTO sources (platform, external_id, display_name, category) VALUES (?, ?, ?, ?)",
        ("youtube", "UC_existing", "Existing", "signal"),
    )
    seed_conn.commit()
    seed_conn.close()

    config_path = tmp_path / "orbit.config.json"
    # cookie, confirm cat = 'n' (flip auto signal -> noise), prioritize 'n', html, email.
    answers = ["chrome", "n", "n", "~/orbit/out/today.html", ""]

    # NOTE: no init_db() call here — the wizard must do it, or this run crashes.
    exit_code = run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_flip2", display_name="Flip Two")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=_signal_classifier(),
        input_fn=_scripted_input(answers),
        store_module=store,  # the REAL store, deliberately un-initialized
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )

    assert exit_code == 0
    # Migration 2 was applied by the wizard's init_db.
    check = sqlite3.connect(str(db_path))
    columns = {row[1] for row in check.execute("PRAGMA table_info(sources)")}
    check.close()
    assert "category_is_user_override" in columns, "the wizard did not migrate the v1 DB"
    # The pre-existing row survived the migration.
    assert store.get_source("youtube", "UC_existing")["category"] == "signal"
    # The user's new noise mark was persisted as a user override.
    flipped = store.get_source("youtube", "UC_flip2")
    assert flipped["category"] == "noise"
    assert flipped["category_is_user_override"] == 1


def test_rerun_setup_uses_stored_category_as_default_and_skips_llm(tmp_path: Path) -> None:
    """A second setup run reads the stored user category as the default and skips re-classifying.

    WHY (story #15 + no wasted judgment): re-running setup should let the user ADJUST prior
    choices, not redo them from scratch. A channel the user already set must present its stored
    category as the default AND must skip the LLM classify call — mirroring the sacred
    'a user override is never re-judged' rule the daily classify path already honors. We pin the
    classifier call count at 0 on the second run and prove the stored 'noise' appears in the
    confirm prompt.
    """
    real_store = _real_store(tmp_path)
    config_path = tmp_path / "orbit.config.json"

    # Run 1: user flips the channel to noise; the classifier is consulted exactly once.
    llm_first = _signal_classifier()
    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_again", display_name="Again")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=llm_first,
        input_fn=_scripted_input(["chrome", "n", "n", "~/orbit/out/today.html", ""]),
        store_module=real_store,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )
    assert llm_first.call_count == 1
    assert real_store.get_source("youtube", "UC_again")["category"] == "noise"

    # Run 2: same channel, now user-set. The wizard must NOT call the LLM for it.
    llm_second = _signal_classifier()
    rerun_input = _scripted_input(["chrome", "y", "n", "~/orbit/out/today.html", ""])
    run_setup_wizard(
        config_path=config_path,
        repo_path=tmp_path,
        youtube_loader=MagicMock(return_value=[Subscription(channel_id="UC_again", display_name="Again")]),
        x_loader=MagicMock(return_value=[]),
        llm_classifier=llm_second,
        input_fn=rerun_input,
        store_module=real_store,
        launch_agents_dir=tmp_path / "LaunchAgents",
        launchctl_runner=_FakeLaunchctl(),
        crontab_runner=_FakeCrontab(),
    )
    assert llm_second.call_count == 0, "a user-set channel must not be re-classified on re-run"
    # The confirm prompt presented the stored category as the default.
    prompts = [call.args[0].lower() for call in rerun_input.call_args_list]
    assert any("noise" in text for text in prompts), f"stored category not shown as default: {prompts}"
    # And the mark is still noise after the user keeps it.
    assert real_store.get_source("youtube", "UC_again")["category"] == "noise"
