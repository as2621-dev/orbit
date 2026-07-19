"""Tests for lib.archive — the fail-soft digest archive push (issue #7).

After render, ``digest.md`` + the Tiles HTML pages are pushed to the private
``as2621-dev/orbit-digests`` repo under ``YYYY/MM/DD/`` — ONE commit per run, via the
GitHub git-data API through ``gh`` (stateless: no local clone that grows forever). The
``gh`` boundary is INJECTED (a runner callable), so no test here shells out, touches the
network, or performs a real push.

Why these tests matter (Rule 9 — encode WHY, not just WHAT):

  * The archive is strictly SECONDARY to the email (PRD story #19): every failure mode —
    no network, bad auth, non-fast-forward, even a missing ``gh`` binary — must be a loud
    log + ``False``, NEVER an exception that could reach the pipeline between render and
    delivery. The fail-soft boundary lives at this module's edge; these tests prove no
    failure escapes it.
  * Privacy guard: the digest contains the owner's full feed. If the archive repo ever
    turns non-private, pushing would publish it — the guard must refuse loudly BEFORE any
    write, not after.
  * One commit per run is the archive's shape contract: the git-data flow must produce
    exactly one commit whose tree carries every file under the date prefix.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pytest

# Make the skill's scripts dir importable so ``from lib import archive`` resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import archive  # noqa: E402
from lib.subproc import SubprocResult  # noqa: E402

_ARCHIVE_DATE = date(2026, 7, 18)


@dataclass
class _RecordedCall:
    """One recorded gh invocation: the argv and any parsed ``--input`` JSON payload."""

    args: list[str]
    input_payload: Optional[dict[str, Any]] = None


@dataclass
class _ScriptedGhRunner:
    """Fake gh runner: routes each argv to a scripted result; records every call.

    ``script`` maps a *route substring* (matched against ``" ".join(args)``) to the
    ``SubprocResult`` to return. First match wins, in insertion order. Any ``--input``
    JSON file is read AT CALL TIME (the module may delete temp files afterwards).
    """

    script: dict[str, SubprocResult]
    calls: list[_RecordedCall] = field(default_factory=list)

    def __call__(self, args: list[str]) -> SubprocResult:
        joined_args = " ".join(str(a) for a in args)
        input_payload = None
        if "--input" in args:
            input_path = Path(args[args.index("--input") + 1])
            input_payload = json.loads(input_path.read_text(encoding="utf-8"))
        self.calls.append(_RecordedCall(list(str(a) for a in args), input_payload))
        for route_substring, result in self.script.items():
            if route_substring in joined_args:
                return result
        raise AssertionError(f"unscripted gh call: {joined_args}")


def _ok(stdout: str) -> SubprocResult:
    return SubprocResult(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str) -> SubprocResult:
    return SubprocResult(returncode=1, stdout="", stderr=stderr)


def _happy_script() -> dict[str, SubprocResult]:
    """The full happy-path script for one archive push (route -> result)."""
    return {
        "repo view": _ok("PRIVATE\n"),
        "git/ref/heads/": _ok("basecommitsha\n"),
        "git/commits/basecommitsha": _ok("basetreesha\n"),
        "git/blobs": _ok("blobsha\n"),
        "git/trees": _ok("newtreesha\n"),
        "git/commits --method POST": _ok("newcommitsha\n"),
        "git/refs/heads/": _ok("newcommitsha\n"),
    }


def _write_digest_files(tmp_path: Path) -> tuple[Path, list[Path]]:
    digest_md = tmp_path / "digest.md"
    digest_md.write_text("# Orbit Digest\n\nA thing happened ☕.\n", encoding="utf-8")
    page_1 = tmp_path / "today.html"
    page_1.write_text("<!DOCTYPE html><html>page one</html>", encoding="utf-8")
    return digest_md, [page_1]


def test_archive_pushes_one_commit_with_date_prefixed_paths(tmp_path: Path) -> None:
    """Happy path: one commit whose tree carries digest.md + HTML under YYYY/MM/DD/.

    WHY: "one commit per run" and the date-partitioned layout ARE the archive contract
    (PRD story #18) — a commit per file, or files at the repo root, would make the
    archive unbrowsable and the history noisy. The ref update must also never force
    (a non-fast-forward is a fail-soft skip, not a history rewrite).
    """
    digest_md, html_pages = _write_digest_files(tmp_path)
    runner = _ScriptedGhRunner(_happy_script())

    archived = archive.archive_digest(
        digest_md, html_pages, archive_date=_ARCHIVE_DATE, run_gh=runner
    )

    assert archived is True

    joined_calls = [" ".join(call.args) for call in runner.calls]
    commit_calls = [call for call in runner.calls if "git/commits" in " ".join(call.args) and "POST" in call.args]
    assert len(commit_calls) == 1, "exactly ONE commit per run"
    assert commit_calls[0].input_payload["parents"] == ["basecommitsha"], "the commit extends master, no orphan"

    tree_calls = [call for call in runner.calls if "git/trees" in " ".join(call.args)]
    assert len(tree_calls) == 1
    tree_paths = sorted(entry["path"] for entry in tree_calls[0].input_payload["tree"])
    assert tree_paths == ["2026/07/18/digest.md", "2026/07/18/today.html"]
    assert tree_calls[0].input_payload["base_tree"] == "basetreesha", "prior days stay in the tree"

    ref_updates = [call for call in runner.calls if "git/refs/heads/" in " ".join(call.args)]
    assert len(ref_updates) == 1
    assert ref_updates[0].input_payload.get("force") is not True, "never force-push the archive"
    assert any("repo view" in joined for joined in joined_calls), "privacy is checked on every run"


def test_archive_refuses_to_push_to_a_non_private_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A non-private archive repo is a LOUD error and the push is skipped entirely.

    WHY: the digest is the owner's full private feed; pushing it to a public repo would
    publish it. The guard must refuse BEFORE any blob/tree/commit write — and stay
    fail-soft (return False) so the email still sends.
    """
    digest_md, html_pages = _write_digest_files(tmp_path)
    runner = _ScriptedGhRunner({"repo view": _ok("PUBLIC\n")})

    archived = archive.archive_digest(digest_md, html_pages, archive_date=_ARCHIVE_DATE, run_gh=runner)

    assert archived is False
    assert len(runner.calls) == 1, "no git-data call may follow a failed privacy check"
    captured = capsys.readouterr().out
    assert "digest_archive_repo_not_private" in captured
    assert "fix_suggestion" in captured


@pytest.mark.parametrize(
    ("failing_route", "expected_reached_routes"),
    [
        ("repo view", 1),  # privacy check itself fails (network down / gh unauthenticated)
        ("git/ref/heads/", 2),  # base ref fetch fails
        ("git/blobs", 4),  # blob upload fails (repo view + ref + base commit reached first)
        ("git/refs/heads/", 8),  # final ref update fails (e.g. non-fast-forward) — all 8 steps ran
    ],
)
def test_archive_any_step_failure_is_fail_soft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing_route: str,
    expected_reached_routes: int,
) -> None:
    """Every push-step failure returns False with a loud fix_suggestion — never raises.

    WHY (the acceptance criterion): the archive sits between render and delivery in the
    pipeline; ANY exception escaping here would kill the email. Driving each failure
    point proves the fail-soft boundary holds at every step, and the call count proves
    the flow stops at the failed step instead of pushing partial state further.
    """
    digest_md, html_pages = _write_digest_files(tmp_path)
    script = _happy_script()
    script[failing_route] = _fail("gh: step exploded")
    # dict insertion order routes "git/refs/heads/" AFTER "git/ref/heads/" would match it
    # first — rebuild with the failing route first so it wins the routing.
    runner = _ScriptedGhRunner({failing_route: script.pop(failing_route), **script})

    archived = archive.archive_digest(digest_md, html_pages, archive_date=_ARCHIVE_DATE, run_gh=runner)

    assert archived is False
    assert len(runner.calls) == expected_reached_routes, "the flow stops at the failed step"
    captured = capsys.readouterr().out
    assert "digest_archive_failed" in captured
    assert "fix_suggestion" in captured


def test_archive_survives_a_missing_gh_binary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A runner that RAISES (gh not installed / not on launchd PATH) is still fail-soft.

    WHY: launchd runs with a minimal PATH; if ``gh`` vanishes, the archive must degrade
    to a loud log — the email (which needs no gh) must be unaffected. This pins the
    boundary against exceptions, not just non-zero exits.
    """
    digest_md, html_pages = _write_digest_files(tmp_path)

    def exploding_runner(args: list[str]) -> SubprocResult:
        raise FileNotFoundError("gh")

    archived = archive.archive_digest(digest_md, html_pages, archive_date=_ARCHIVE_DATE, run_gh=exploding_runner)

    assert archived is False
    captured = capsys.readouterr().out
    assert "digest_archive_failed" in captured
    assert "fix_suggestion" in captured


def test_archive_skips_cleanly_when_nothing_was_rendered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No on-disk files at all -> a logged skip with NO gh calls (not even the privacy check).

    WHY: a failed render leaves nothing to archive; shelling out to gh for an empty
    commit would create noise commits and mask the real failure upstream.
    """
    runner = _ScriptedGhRunner({})

    archived = archive.archive_digest(
        tmp_path / "digest.md", [tmp_path / "today.html"], archive_date=_ARCHIVE_DATE, run_gh=runner
    )

    assert archived is False
    assert runner.calls == [], "nothing to push means no gh subprocess at all"
    assert "digest_archive_skipped" in capsys.readouterr().out


def test_archive_pushes_html_even_when_digest_md_is_missing(tmp_path: Path) -> None:
    """A missing digest.md (twin failed to render) still archives the HTML pages.

    WHY (PRD story #19 shape): the markdown twin is fail-soft upstream; its absence must
    not cost the day's HTML archive too. The tree simply carries fewer files.
    """
    _, html_pages = _write_digest_files(tmp_path)
    runner = _ScriptedGhRunner(_happy_script())

    archived = archive.archive_digest(
        tmp_path / "never-written.md", html_pages, archive_date=_ARCHIVE_DATE, run_gh=runner
    )

    assert archived is True
    tree_calls = [call for call in runner.calls if "git/trees" in " ".join(call.args)]
    tree_paths = [entry["path"] for entry in tree_calls[0].input_payload["tree"]]
    assert tree_paths == ["2026/07/18/today.html"]
