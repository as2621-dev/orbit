"""Tests for lib.llm — the Claude CLI boundary behind classify/chapterize.

Why these tests matter (Rule 9 — encode WHY, not just WHAT):

  * The boundary's contract is ``Callable[[str], str]``: a rendered prompt in, the
    model's raw text out (which classify/chapterize parse as JSON). The happy-path
    test fails if the response handling regresses (e.g. someone returns stderr, or
    forgets to JSON-extract a fenced reply) — that would silently break every
    classification.
  * Fail loud (Rule 12): a missing ``claude`` binary, a non-zero exit, or empty output
    must RAISE :class:`llm.LlmCliError`, not fake a verdict or return "". A silent
    degrade would route every item by the channel-prior fallback and look like it
    "worked".
  * The subprocess is the external boundary and is ALWAYS mocked (the injected
    ``runner``) — a test that spawned a real ``claude`` process would be slow, flaky,
    and consume subscription quota.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the skill's scripts dir importable so ``from lib import llm`` resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import llm, subproc  # noqa: E402


def _runner_returning(stdout: str, *, returncode: int = 0, stderr: str = ""):
    """Build a fake subprocess runner that captures the command and returns a fixed result.

    Mirrors :func:`lib.subproc.run_with_timeout`'s shape so the injected ``runner`` is a
    faithful stand-in: it accepts ``(cmd, *, timeout, ...)`` and yields a ``SubprocResult``.
    """
    captured: dict = {}

    def _runner(cmd, *, timeout, **_kwargs) -> subproc.SubprocResult:
        captured["cmd"] = list(cmd)
        captured["timeout"] = timeout
        return subproc.SubprocResult(returncode=returncode, stdout=stdout, stderr=stderr)

    _runner.captured = captured  # type: ignore[attr-defined]
    return _runner


def test_call_claude_cli_returns_clean_json_and_passes_model() -> None:
    """Happy path: clean JSON stdout is returned verbatim, and the chosen model reaches the CLI.

    Encodes the contract callers depend on — the raw JSON string flows straight to
    ``json.loads`` — and that ``--model`` is actually wired (a regression dropping it would
    silently classify on the wrong/expensive model)."""
    verdict = '{"axis_a_signal":1,"axis_b_on_topic":0}'
    runner = _runner_returning(verdict)

    result = llm.call_claude_cli("prompt", model="claude-sonnet-4-6", runner=runner)

    assert result == verdict
    assert runner.captured["cmd"] == ["claude", "-p", "--model", "claude-sonnet-4-6", "prompt"]


def test_call_claude_cli_strips_markdown_fence() -> None:
    """A ```json fenced reply is unwrapped so callers get parseable JSON, not a fenced blob.

    ``claude -p`` sometimes fences output; without extraction every such verdict would fail
    ``json.loads`` and fall back to the channel prior — a silent accuracy loss."""
    fenced = '```json\n{"axis_a_signal":0,"axis_b_on_topic":1}\n```'
    result = llm.call_claude_cli("prompt", runner=_runner_returning(fenced))

    assert result == '{"axis_a_signal":0,"axis_b_on_topic":1}'


def test_call_claude_cli_extracts_json_from_surrounding_prose() -> None:
    """A reply with leading prose still yields the JSON object (best-effort slice)."""
    noisy = 'Here is the verdict:\n{"axis_a_signal":1,"axis_b_on_topic":1}\nHope that helps.'
    result = llm.call_claude_cli("prompt", runner=_runner_returning(noisy))

    assert result == '{"axis_a_signal":1,"axis_b_on_topic":1}'


def test_call_claude_cli_nonzero_exit_raises_loud() -> None:
    """A non-zero CLI exit must raise, never silently degrade (Rule 12)."""
    runner = _runner_returning("", returncode=1, stderr="bad model id")
    with pytest.raises(llm.LlmCliError):
        llm.call_claude_cli("prompt", runner=runner)


def test_call_claude_cli_empty_output_raises_loud() -> None:
    """Empty stdout must raise — an empty verdict would look like a successful classify."""
    with pytest.raises(llm.LlmCliError):
        llm.call_claude_cli("prompt", runner=_runner_returning("   "))


def test_call_claude_cli_timeout_raises_loud() -> None:
    """A timed-out spawn surfaces as LlmCliError, not a hang or a swallowed error."""

    def _timeout_runner(cmd, *, timeout, **_kwargs):
        raise subproc.SubprocTimeout("claude timed out")

    with pytest.raises(llm.LlmCliError):
        llm.call_claude_cli("prompt", runner=_timeout_runner)


def test_call_claude_cli_missing_binary_raises_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the ``claude`` binary is not on PATH, fail loud before attempting a spawn."""
    monkeypatch.setattr(llm.shutil, "which", lambda _name: None)
    with pytest.raises(llm.LlmCliError):
        llm.call_claude_cli("prompt", runner=_runner_returning("{}"))


def test_make_llm_classifier_returns_callable_shape() -> None:
    """The factory returns a ``Callable[[str], str]`` that round-trips a prompt through the CLI."""
    classifier = llm.make_llm_classifier(runner=_runner_returning('{"ok":1}'))
    assert classifier("rendered prompt") == '{"ok":1}'


def test_load_dotenv_seeds_without_overriding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``.env`` fills gaps in os.environ but NEVER overrides a value already set in the shell.

    The shell must stay authoritative — otherwise a stale ``.env`` could silently shadow a
    credential the user exported for this run."""
    monkeypatch.delenv("ORBIT_TEST_KEY", raising=False)
    monkeypatch.setenv("ORBIT_TEST_PRESET", "from_shell")
    dotenv = tmp_path / ".env"
    dotenv.write_text("ORBIT_TEST_KEY=from_file\nORBIT_TEST_PRESET=from_file\n", encoding="utf-8")

    llm.load_dotenv(dotenv)

    assert os.environ["ORBIT_TEST_KEY"] == "from_file"
    assert os.environ["ORBIT_TEST_PRESET"] == "from_shell"


def test_load_dotenv_ignores_empty_placeholder_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty placeholder line (``AUTH_TOKEN=``) must not register as 'configured'.

    .env.example ships blank placeholders; treating them as set would shadow a real value or
    fake configuration the loader should report as absent."""
    monkeypatch.delenv("ORBIT_TEST_EMPTY", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("ORBIT_TEST_EMPTY=\n", encoding="utf-8")

    llm.load_dotenv(dotenv)

    assert "ORBIT_TEST_EMPTY" not in os.environ
