"""LLM boundary — classification via the host Claude Code subscription (no API key).

Orbit's classify (:data:`lib.classify.LlmClassifier`) and chapterize
(:data:`lib.chapterize.ChapterSegmenter`) boundaries share one shape:
``Callable[[str], str]`` — a rendered prompt in, the model's raw text out (which the
callers parse as JSON). This module implements that boundary by shelling out to the
``claude`` CLI in headless mode (``claude -p``), which authenticates with the user's
existing Claude Code subscription — NO separate ``ANTHROPIC_API_KEY`` and NO
pay-as-you-go API credits.

Why the CLI, not the HTTP Messages API:

  - **Uses the subscription you already pay for.** A direct POST to ``api.anthropic.com``
    is billed as raw API usage (needs a funded credit balance); ``claude -p`` runs on the
    Claude Code subscription. This was the stub's original design intent — the "host
    Claude-session caller" it named.
  - **Subprocess-first, stdlib only.** Orbit already invokes ``yt-dlp`` and the Node X
    client as subprocesses (``pyproject.toml``: ``dependencies = []``). Shelling to
    ``claude`` via :func:`lib.subproc.run_with_timeout` keeps the zero-install posture —
    no ``anthropic`` SDK, no credentials to manage.
  - **Default model = Claude Sonnet 4.6.** The chosen classify model: sharper signal/noise
    and on/off-topic judgment than Haiku, on the subscription. Callers may override the
    model per boundary.
  - **Fail loud (Rule 12).** A missing ``claude`` binary, a non-zero exit, or empty output
    raises :class:`LlmCliError` with an actionable ``fix_suggestion`` rather than silently
    degrading.

:func:`load_dotenv` stays here as the process-startup ``.env`` seeder: the X loader
(:mod:`lib.bird_x`) reads ``AUTH_TOKEN`` / ``CT0`` / ``X_USER_ID`` from ``os.environ``, so
``orbit.py`` calls :func:`load_dotenv` once at startup to seed them. It is NOT needed for
classification (the CLI carries its own auth) and never logs a value.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

# Make ``lib`` importable whether imported as the package member ``lib.llm`` (via
# orbit.py's sys.path insert) or run from the scripts dir directly. Mirrors
# classify.py / chapterize.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log, subproc  # noqa: E402  (import must follow the sys.path inserts above)

# The Claude Code CLI binary Orbit shells out to for classification.
CLAUDE_CLI: str = "claude"

# Default model for the classify/chapterize boundary. Sonnet 4.6 — sharper judgment than
# Haiku on the signal/noise + on/off-topic calls; runs on the Claude Code subscription.
DEFAULT_CLASSIFY_MODEL: str = "claude-sonnet-4-6"

# Wall-clock cap for a single classify/segment CLI call, in seconds. A daily run makes many
# calls; one stuck ``claude -p`` spawn must not hang the whole pipeline indefinitely.
_CLI_TIMEOUT_SECONDS: int = 120

# Repo root (scripts/lib/llm.py -> ../../). Where a local ``.env`` lives.
_REPO_ROOT: Path = _SCRIPTS_DIR.parent.resolve()

# The subprocess runner shape (matches :func:`lib.subproc.run_with_timeout`), injectable so
# tests mock the subprocess boundary instead of spawning a real ``claude`` process.
CliRunner = Callable[..., subproc.SubprocResult]


class LlmCliError(RuntimeError):
    """Raised when the ``claude`` CLI is missing, times out, exits non-zero, or returns no
    usable output — fail loud, never fake a verdict."""


def load_dotenv(dotenv_path: Optional[Path] = None) -> None:
    """Seed ``os.environ`` from a ``.env`` file WITHOUT overriding existing values.

    A minimal, stdlib-only ``.env`` reader (the project takes no third-party deps).
    Parses ``KEY=VALUE`` lines, ignoring blanks and ``#`` comments, and strips one
    layer of matching surrounding quotes. Values already present in the environment
    win — the real shell environment is authoritative over the file.

    Called once at process startup (by ``orbit.py``) so downstream readers — notably the
    X loader (:mod:`lib.bird_x`), which reads ``AUTH_TOKEN`` / ``CT0`` / ``X_USER_ID`` from
    ``os.environ`` — see the user's local ``.env`` values.

    Args:
        dotenv_path: The ``.env`` file to read; defaults to ``<repo_root>/.env``.

    Example:
        >>> load_dotenv()  # reads <repo_root>/.env if it exists
    """
    path = dotenv_path or (_REPO_ROOT / ".env")
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # Reason: the real environment is authoritative — only fill gaps from .env.
        # Skip empty values so a placeholder line (e.g. ``AUTH_TOKEN=`` from .env.example)
        # never counts as "configured" or shadows a real value set on a later line.
        if key and value and key not in os.environ:
            os.environ[key] = value


def _extract_json_text(raw: str) -> str:
    """Pull the JSON payload out of a ``claude -p`` response, best-effort.

    ``claude -p`` usually returns clean JSON, but may wrap it in a ```` ```json ```` fence
    or add surrounding prose. Callers parse the result with ``json.loads`` (degrading to a
    prior on failure), so we: strip a markdown fence if present; return the text as-is when
    it already parses; otherwise slice the first balanced ``{...}`` or ``[...]`` block. If no
    JSON is found, return the stripped text and let the caller's fallback handle it.

    Args:
        raw: The raw stdout string from the ``claude`` CLI.

    Returns:
        The extracted JSON substring (or the stripped raw text if none is found).
    """
    text = raw.strip()
    if not text:
        return ""

    # Strip a leading ```/```json fence and its trailing ``` if the model fenced its output.
    if text.startswith("```"):
        inner = text[3:]
        first_newline = inner.find("\n")
        if first_newline != -1:
            inner = inner[first_newline + 1 :]
        inner = inner.rstrip()
        if inner.endswith("```"):
            inner = inner[:-3]
        text = inner.strip()

    # Already valid JSON (the common case from ``claude -p``) — keep it verbatim.
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: slice the first balanced object/array. Sufficient for the small, flat
    # verdict/segment payloads Orbit's prompts request; the caller degrades safely if this
    # still does not parse.
    candidate_starts = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if not candidate_starts:
        return text
    start = min(candidate_starts)
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    for index in range(start, len(text)):
        if text[index] == open_char:
            depth += 1
        elif text[index] == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def call_claude_cli(
    prompt: str,
    *,
    model: str = DEFAULT_CLASSIFY_MODEL,
    timeout: int = _CLI_TIMEOUT_SECONDS,
    runner: CliRunner = subproc.run_with_timeout,
) -> str:
    """Send one prompt to ``claude -p`` and return the model's response text.

    The single live-model call behind both Orbit boundaries. Runs
    ``claude -p --model <model> <prompt>`` and returns the response, JSON-extracted — the
    raw string the classify/chapterize callers parse. Authenticates via the Claude Code
    subscription; no API key is read or sent.

    Args:
        prompt: The fully rendered prompt (from references/classify.md or
            references/chapterize.md).
        model: The Claude model id (default: :data:`DEFAULT_CLASSIFY_MODEL`).
        timeout: Per-call wall-clock cap in seconds.
        runner: The subprocess runner; injectable so tests mock the spawn.

    Returns:
        The model's response text (JSON-extracted).

    Raises:
        LlmCliError: If the ``claude`` binary is missing, the call times out or exits
            non-zero, or the output carries no usable text.

    Example:
        >>> verdict_json = call_claude_cli(rendered_prompt, model="claude-sonnet-4-6")
    """
    if shutil.which(CLAUDE_CLI) is None:
        log.log_error(
            "llm_cli_missing",
            fix_suggestion=(
                "The 'claude' CLI is not on PATH. Install Claude Code and ensure 'claude' is "
                "runnable, then re-run. Orbit classifies via 'claude -p' on your subscription."
            ),
            cli=CLAUDE_CLI,
        )
        raise LlmCliError(f"'{CLAUDE_CLI}' CLI not found on PATH")

    command = [CLAUDE_CLI, "-p", "--model", model, prompt]
    try:
        result = runner(command, timeout=timeout)
    except subproc.SubprocTimeout as exc:
        log.log_error(
            "llm_cli_timeout",
            fix_suggestion="The 'claude -p' call exceeded the timeout; re-run, or raise the cap.",
            model=model,
            timeout_seconds=timeout,
        )
        raise LlmCliError(f"'{CLAUDE_CLI} -p' timed out after {timeout}s") from exc
    except (FileNotFoundError, OSError) as exc:
        log.log_error(
            "llm_cli_spawn_failed",
            fix_suggestion="Could not spawn 'claude'; check it is installed and executable.",
            error_message=str(exc),
        )
        raise LlmCliError(f"Failed to spawn '{CLAUDE_CLI}'") from exc

    if result.returncode != 0:
        # stderr can carry an actionable message (bad model id, rate limit) but NO secret —
        # the subscription auth is internal to the CLI, never on our command line.
        log.log_error(
            "llm_cli_nonzero_exit",
            fix_suggestion=(
                "Check the model id and your Claude subscription status/limits. "
                "Run 'claude -p --model <id> \"hi\"' manually to see the error."
            ),
            returncode=result.returncode,
            model=model,
            error_message=result.stderr.strip()[:500],
        )
        raise LlmCliError(f"'{CLAUDE_CLI} -p' exited {result.returncode}")

    text = _extract_json_text(result.stdout)
    if not text:
        log.log_error(
            "llm_cli_empty_output",
            fix_suggestion="'claude -p' returned no text; re-run and check the model id.",
            model=model,
        )
        raise LlmCliError(f"'{CLAUDE_CLI} -p' returned no usable output")
    return text


def make_llm_classifier(
    *,
    model: str = DEFAULT_CLASSIFY_MODEL,
    runner: CliRunner = subproc.run_with_timeout,
) -> Callable[[str], str]:
    """Build the real classify/segment boundary: a ``Callable[[str], str]``.

    The returned callable satisfies both :data:`lib.classify.LlmClassifier` and
    :data:`lib.chapterize.ChapterSegmenter` (identical shape). Wire it into the
    pipeline / setup wizard in place of the fail-loud default.

    Args:
        model: The Claude model id for this boundary.
        runner: The subprocess runner; injectable for tests.

    Returns:
        A function that takes a rendered prompt and returns the model's raw text.

    Example:
        >>> classifier = make_llm_classifier()
        >>> verdict_json = classifier(rendered_prompt)
    """

    def _classifier(prompt: str) -> str:
        return call_claude_cli(prompt, model=model, runner=runner)

    return _classifier
