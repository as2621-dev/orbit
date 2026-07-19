"""Orbit digest archive — push digest.md + Tiles HTML to a private repo, fail-soft (issue #7).

After render, the day's ``digest.md`` and Tiles HTML page(s) land in the private
``as2621-dev/orbit-digests`` repo under ``YYYY/MM/DD/`` — ONE commit per run, via the
GitHub git-data API through the ``gh`` CLI (blobs -> tree -> commit -> ref update).
Stateless by design: no local clone that grows with every archived day, and no git
credential-helper dance — ``gh`` is the only auth surface, and it works headless under
the LaunchAgent (verified 2026-07-18: a one-shot ``gui/<uid>`` LaunchAgent ran
``gh api user`` successfully with the token in the macOS keyring).

THE FAIL-SOFT BOUNDARY LIVES AT THIS MODULE'S EDGE (PRD story #19): the archive is
strictly secondary to the email. :func:`archive_digest` never raises — any failure
(no network, bad auth, non-fast-forward, missing ``gh`` binary, vanished files) is a
loud structured log with a ``fix_suggestion`` and a ``False`` return, so the pipeline
always reaches delivery untouched.

Privacy guard (hard rule): the digest is the owner's full private feed. Every run
verifies the repo is PRIVATE (``gh repo view --json visibility``) BEFORE any write; a
non-private repo refuses the push loudly.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as ``lib.archive`` or run
# from the scripts dir directly. Mirrors deliver.py / config.py.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log, subproc  # noqa: E402  (import must follow the sys.path inserts above)

# The existing private archive repo (issue #7 pre-verified) and its default branch.
ARCHIVE_REPO: str = "as2621-dev/orbit-digests"
ARCHIVE_BRANCH: str = "master"

# Per-gh-call timeout. Generous for a ~430KB base64 blob upload on a slow morning link,
# but bounded so a network black hole can't stall the pipeline between render and email.
GH_TIMEOUT_SECONDS: int = 60

# The injected gh boundary: argv (WITHOUT the leading "gh") in, SubprocResult out.
# Tests fake this so no test shells out or touches the network; one call == one gh run.
GhRunner = Callable[[Sequence[str]], subproc.SubprocResult]


def _run_gh_command(args: Sequence[str]) -> subproc.SubprocResult:
    """Run one real ``gh`` command with the shared timeout/process-group cleanup."""
    return subproc.run_with_timeout(["gh", *args], timeout=GH_TIMEOUT_SECONDS)


class _ArchiveStepError(Exception):
    """One gh step exited non-zero; carries the step name + trimmed stderr for the log."""

    def __init__(self, step_name: str, stderr: str) -> None:
        super().__init__(f"gh step '{step_name}' failed")
        self.step_name = step_name
        self.stderr_excerpt = (stderr or "").strip()[:300]


def _gh_output(run_gh: GhRunner, args: Sequence[str], step_name: str) -> str:
    """Run one gh step and return its stripped stdout, raising on a non-zero exit."""
    result = run_gh(list(args))
    if result.returncode != 0:
        raise _ArchiveStepError(step_name, result.stderr)
    return result.stdout.strip()


def archive_digest(
    digest_md_path: Path,
    html_paths: Sequence[Path],
    *,
    archive_date: Optional[date] = None,
    run_gh: GhRunner = _run_gh_command,
) -> bool:
    """Push today's digest files to the private archive repo — one commit, never raising.

    Collects whichever of ``digest_md_path`` + ``html_paths`` exist on disk (a missing
    markdown twin costs only that file, not the day's archive), verifies the repo is
    PRIVATE, then builds one commit under ``YYYY/MM/DD/`` via the git-data API and
    fast-forwards the branch ref (never force). Every failure mode is fail-soft: a loud
    structured error log with a ``fix_suggestion``, then ``False`` — the caller's email
    path is untouched by design.

    Args:
        digest_md_path: The ``digest.md`` twin path (skipped with a warning if absent).
        html_paths: The rendered Tiles page paths (page 1 first).
        archive_date: The date partition to file under (defaults to today, UTC — the
            same convention as the digest dateline/subject).
        run_gh: Injected gh runner (tests fake it; no test pushes for real).

    Returns:
        True only when the branch ref was updated to the new commit; False on any
        skip, refusal, or failure.
    """
    try:
        return _push_archive_commit(
            digest_md_path,
            html_paths,
            archive_date=archive_date or datetime.now(timezone.utc).date(),
            run_gh=run_gh,
        )
    except _ArchiveStepError as step_error:
        log.log_error(
            "digest_archive_failed",
            fix_suggestion=(
                f"The archive push to {ARCHIVE_REPO} failed at the '{step_error.step_name}' step; "
                "the digest email is unaffected. Check the network and `gh auth status` (needs the "
                "'repo' scope), confirm the repo exists, then re-run — the next run re-archives today."
            ),
            channel="archive",
            step=step_error.step_name,
            stderr_excerpt=step_error.stderr_excerpt,
        )
        return False
    except Exception as unexpected_error:
        log.log_error(
            "digest_archive_failed",
            fix_suggestion=(
                f"The archive push to {ARCHIVE_REPO} failed before completing; the digest email is "
                "unaffected. Confirm the `gh` CLI is installed and on PATH for the LaunchAgent, "
                "then re-run — the next run re-archives today."
            ),
            channel="archive",
            step="unexpected",
            error_type=type(unexpected_error).__name__,
            error_message=str(unexpected_error),
        )
        return False


def _collect_archive_files(digest_md_path: Path, html_paths: Sequence[Path]) -> list[Path]:
    """Return the files that actually exist on disk, warning (not failing) per absentee."""
    existing_files: list[Path] = []
    for candidate_path in (Path(digest_md_path), *(Path(p) for p in html_paths)):
        if candidate_path.is_file():
            existing_files.append(candidate_path)
        else:
            log.log_warning(
                "digest_archive_file_missing",
                channel="archive",
                missing_path=str(candidate_path),
            )
    return existing_files


def _verify_repo_is_private(run_gh: GhRunner, archive_repo: str) -> bool:
    """Check the archive repo's visibility; log loudly and return False unless PRIVATE."""
    visibility = _gh_output(
        run_gh,
        ["repo", "view", archive_repo, "--json", "visibility", "--jq", ".visibility"],
        step_name="verify_repo_private",
    )
    if visibility.upper() == "PRIVATE":
        return True
    log.log_error(
        "digest_archive_repo_not_private",
        fix_suggestion=(
            f"The archive repo {archive_repo} is '{visibility}', not PRIVATE — pushing would publish "
            "your full feed digest. Make the repo private (gh repo edit --visibility private) and "
            "re-run; the push was skipped and the digest email is unaffected."
        ),
        channel="archive",
        repo_visibility=visibility,
    )
    return False


def _push_archive_commit(
    digest_md_path: Path,
    html_paths: Sequence[Path],
    *,
    archive_date: date,
    run_gh: GhRunner,
) -> bool:
    """Build and push the one archive commit (blobs -> tree -> commit -> ref fast-forward).

    Raises :class:`_ArchiveStepError` on any non-zero gh exit; :func:`archive_digest`
    owns turning that into the fail-soft log.
    """
    archive_files = _collect_archive_files(digest_md_path, html_paths)
    if not archive_files:
        log.log_info(
            "digest_archive_skipped",
            channel="archive",
            reason="no_files_on_disk",
            detail="Nothing rendered to archive; skipping the push entirely.",
        )
        return False

    if not _verify_repo_is_private(run_gh, ARCHIVE_REPO):
        return False

    date_prefix = f"{archive_date.year:04d}/{archive_date.month:02d}/{archive_date.day:02d}"
    api_base = f"repos/{ARCHIVE_REPO}/git"

    base_commit_sha = _gh_output(
        run_gh,
        ["api", f"{api_base}/ref/heads/{ARCHIVE_BRANCH}", "--jq", ".object.sha"],
        step_name="fetch_branch_ref",
    )
    base_tree_sha = _gh_output(
        run_gh,
        ["api", f"{api_base}/commits/{base_commit_sha}", "--jq", ".tree.sha"],
        step_name="fetch_base_tree",
    )

    # Payloads ride via --input files, never argv: a base64 Tiles page (~430KB) would
    # blow past the OS argv size limit as a command-line argument.
    with tempfile.TemporaryDirectory(prefix="orbit-archive-") as payload_dir_name:
        payload_dir = Path(payload_dir_name)

        def post_json(
            endpoint: str, payload: dict[str, Any], payload_name: str, *, method: str = "POST", jq: str = ".sha"
        ) -> str:
            payload_path = payload_dir / f"{payload_name}.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            return _gh_output(
                run_gh,
                ["api", endpoint, "--method", method, "--input", str(payload_path), "--jq", jq],
                step_name=payload_name,
            )

        tree_entries = []
        for file_index, archive_file in enumerate(archive_files):
            blob_sha = post_json(
                f"{api_base}/blobs",
                {
                    "content": base64.b64encode(archive_file.read_bytes()).decode("ascii"),
                    "encoding": "base64",
                },
                f"upload_blob_{file_index}",
            )
            tree_entries.append(
                {"path": f"{date_prefix}/{archive_file.name}", "mode": "100644", "type": "blob", "sha": blob_sha}
            )

        new_tree_sha = post_json(
            f"{api_base}/trees",
            {"base_tree": base_tree_sha, "tree": tree_entries},
            "create_tree",
        )
        new_commit_sha = post_json(
            f"{api_base}/commits",
            {
                "message": f"orbit digest {archive_date.isoformat()}",
                "tree": new_tree_sha,
                "parents": [base_commit_sha],
            },
            "create_commit",
        )
        # Fast-forward only (force omitted -> false): a concurrent push surfaces as a
        # non-zero exit here, which is a fail-soft skip — never a history rewrite.
        post_json(
            f"{api_base}/refs/heads/{ARCHIVE_BRANCH}",
            {"sha": new_commit_sha},
            "update_branch_ref",
            method="PATCH",
            jq=".object.sha",
        )

    log.log_info(
        "digest_archived",
        channel="archive",
        repo=ARCHIVE_REPO,
        date_prefix=date_prefix,
        file_count=len(archive_files),
        commit_sha=new_commit_sha,
    )
    return True
