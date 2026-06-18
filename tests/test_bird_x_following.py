"""DoD tests for the X Following loader (Phase 4, Sub-phase 2 / Stage 0).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does:

  1. ``test_load_following_parses_and_persists`` — Stage 0 is the foundation the X
     half stands on. If a canned Following payload did not parse into the right
     ``creator_handle``s AND land in ``sources`` as ``platform="x"``, the delta engine
     (Sub-phase 3) would have no X baseline to diff and the user would silently see no
     tweets in the digest.
  2. ``test_auth_failure_raises_loud_error`` — expired/absent cookies MUST fail loud
     with a re-login-to-X / README pointer (Rule 12), never a silent empty load that
     looks like "you follow no one".
  3. ``test_no_credential_value_appears_in_logs`` — the hard security invariant
     (brief §4/§8.6): no cookie / ``auth_token`` / ``ct0`` value may ever reach the
     JSON log stream. A dummy token is fed through ``set_credentials`` and the captured
     stdout (the log stream) is asserted free of it.

The subprocess boundary is mocked (``subproc.run_with_timeout`` patched to return a
canned ``SubprocResult``) — NO live X call, NO real cookies. The store is pointed at a
temp DB via ``ORBIT_DB_PATH`` + ``store._db_override``, mirroring tests/test_store.py.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# Make ``skills/orbit/scripts`` importable so ``import store`` and ``from lib import ...``
# resolve regardless of the working directory. Mirrors tests/test_orbit_stage0.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "orbit" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402
from lib import bird_x, paths, subproc  # noqa: E402

# A dummy auth token used only to prove it never reaches the log stream. NOT a real
# credential (security rule: no real tokens in fixtures).
_DUMMY_AUTH_TOKEN = "dummy_auth_token_THIS_MUST_NEVER_BE_LOGGED"
_DUMMY_CT0 = "dummy_ct0_THIS_MUST_NEVER_BE_LOGGED"

# A canned `--following --json` payload, matching Sub-phase 1's documented output shape.
_CANNED_FOLLOWING_JSON = [
    {"creator_handle": "alice", "display_name": "Alice", "rest_id": "1001"},
    {"creator_handle": "@bob", "display_name": "Bob", "rest_id": "1002"},
    {"creator_handle": "carol", "display_name": "Carol", "rest_id": "1003"},
]


def _fresh_store(tmp_dir: Path) -> Path:
    """Point the store at a temp DB and initialize it. Returns the DB path."""
    db_path = tmp_dir / "orbit.db"
    os.environ[paths.ORBIT_DB_PATH_ENV_VAR] = str(db_path)
    store._db_override = db_path
    return store.init_db()


def _reset_module_state() -> None:
    """Clear injected credentials and the self-id env var between tests."""
    bird_x._credentials.clear()
    os.environ.pop(bird_x.X_USER_ID_ENV_VAR, None)


def _result(returncode: int, stdout: str, stderr: str = "") -> subproc.SubprocResult:
    """Build a canned SubprocResult standing in for the Node subprocess."""
    return subproc.SubprocResult(returncode=returncode, stdout=stdout, stderr=stderr)


def test_load_following_parses_and_persists() -> None:
    """A canned Following payload must parse to the right handles AND be queryable as ``x`` sources.

    WHY: this is the only point where the X half seeds the shared ``sources`` table.
    If parsing or persistence were wrong, Sub-phase 3's delta engine would have no X
    baseline and the unified digest would silently omit every tweet.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        _reset_module_state()
        os.environ[bird_x.X_USER_ID_ENV_VAR] = "777000"
        bird_x.set_credentials(_DUMMY_AUTH_TOKEN, _DUMMY_CT0)

        canned = _result(0, json.dumps(_CANNED_FOLLOWING_JSON))
        with patch.object(bird_x, "is_bird_installed", return_value=True), patch.object(
            bird_x.subproc, "run_with_timeout", return_value=canned
        ) as mock_run:
            follows = bird_x.load_x_following("chrome")

            # Parsed the right handles (and stripped a leading @ on bob).
            assert [f.creator_handle for f in follows] == ["alice", "bob", "carol"]
            assert follows[0].rest_id == "1001"
            assert follows[1].display_name == "Bob"

            # Subprocess invoked with the NUMERIC self-id from env, not a screen name.
            invoked_cmd = mock_run.call_args.args[0]
            assert invoked_cmd[:3] == ["node", str(bird_x._BIRD_SEARCH_MJS), "--following"]
            assert "777000" in invoked_cmd
            assert "--json" in invoked_cmd

            persisted = bird_x.persist_following(follows)
            assert persisted == 3

        x_sources = store.list_sources(platform="x")
        stored_handles = {source["external_id"] for source in x_sources}
        assert stored_handles == {"alice", "bob", "carol"}
        # Persisted as signal-category X sources with a refresh timestamp.
        assert all(source["platform"] == "x" for source in x_sources)
        assert all(source["category"] == "signal" for source in x_sources)
        assert all(source["last_refreshed_at"] for source in x_sources)


def test_auth_failure_raises_loud_error() -> None:
    """An auth-failure signal from the subprocess must raise a loud, actionable error.

    WHY (Rule 12): expired/absent X cookies must NOT silently return an empty list that
    looks identical to "you follow no one". The user must be told to re-log-in to X and
    pointed at the README troubleshooting section so the run fails loud, not silent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        _reset_module_state()
        os.environ[bird_x.X_USER_ID_ENV_VAR] = "777000"
        bird_x.set_credentials(_DUMMY_AUTH_TOKEN, _DUMMY_CT0)

        # The CLI's --json auth-failure shape: a credentials error with empty items.
        auth_fail = _result(1, json.dumps({"error": "No Twitter credentials found", "items": []}))
        with patch.object(bird_x, "is_bird_installed", return_value=True), patch.object(
            bird_x.subproc, "run_with_timeout", return_value=auth_fail
        ):
            try:
                bird_x.load_x_following("chrome")
                raise AssertionError("expected XAuthError, none raised")
            except bird_x.XAuthError as exc:
                message = str(exc).lower()
                assert "log into x" in message or "re-run" in message
                assert "readme" in message or "§8.6" in message or "troubleshooting" in message


def test_no_credential_value_appears_in_logs() -> None:
    """No cookie / auth_token / ct0 VALUE may ever appear in the JSON log stream.

    WHY (security, brief §4/§8.6): a leaked cookie is the worst failure mode of this
    phase. A dummy token is injected via ``set_credentials`` and a full load is run
    while capturing stdout (the structured-log stream); the dummy value must be absent
    from every captured line — including the auth-failure error path, which is the most
    likely place a careless implementation would echo a credential.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _fresh_store(Path(tmp))
        _reset_module_state()
        os.environ[bird_x.X_USER_ID_ENV_VAR] = "777000"
        bird_x.set_credentials(_DUMMY_AUTH_TOKEN, _DUMMY_CT0)

        captured = io.StringIO()

        # Exercise BOTH the happy path and the auth-failure path under capture, since
        # error logs are where a credential is most likely to leak.
        canned = _result(0, json.dumps(_CANNED_FOLLOWING_JSON))
        with patch.object(bird_x, "is_bird_installed", return_value=True), patch.object(
            bird_x.subproc, "run_with_timeout", return_value=canned
        ):
            with redirect_stdout(captured):
                follows = bird_x.load_x_following("chrome")
                bird_x.persist_following(follows)

        auth_fail = _result(1, json.dumps({"error": "credentials expired", "items": []}))
        with patch.object(bird_x, "is_bird_installed", return_value=True), patch.object(
            bird_x.subproc, "run_with_timeout", return_value=auth_fail
        ):
            with redirect_stdout(captured):
                try:
                    bird_x.load_x_following("chrome")
                except bird_x.XAuthError:
                    pass

        log_output = captured.getvalue()
        assert _DUMMY_AUTH_TOKEN not in log_output
        assert _DUMMY_CT0 not in log_output
        # Sanity: we actually captured a log stream (so the assertion above is meaningful).
        assert "x_following_load_started" in log_output
