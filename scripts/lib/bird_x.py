"""X (Twitter) Following loader — Stage 0 source loading for Orbit.

Resolves the logged-in user's *Following* list via the vendored Node bird-search
client (Sub-phase 1's ``--following <userId> --json`` op), parses it into typed
:class:`Follow` records, and persists each as a ``platform="x"`` row in the shared
``sources`` table so the rest of the pipeline treats X creators exactly like
YouTube channels.

Borrowed verbatim-in-spirit from the last30days ``bird_x.py`` reference: the
``set_credentials`` / ``_subprocess_env`` credential-injection recipe (cookies flow
to Node via env, never CLI args), :data:`DEPTH_CONFIG`, :func:`is_bird_installed`,
the ``_BIRD_SEARCH_MJS`` path, and the JSON-decode-retry shape. The reference's
topic-search machinery (``search_x`` / relevance / mentions) is deliberately NOT
carried over — Orbit follows a subscription model, not topic search.

Security (hard rule, brief §4/§8.6): cookies are read at runtime ONLY and reach the
Node subprocess via env (:func:`_subprocess_env`). Cookie / ``auth_token`` / ``ct0``
values are NEVER passed as CLI args, NEVER written to disk, NEVER logged, and NEVER
placed in an exception message. Expired/absent cookies raise a loud, actionable
:class:`XAuthError` pointing at the README troubleshooting section (§8.6) — never a
silent no-op (Rule 12).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Make ``lib`` and ``store`` importable whether this module is imported as the package
# member ``lib.bird_x`` (via orbit.py's sys.path insert of the scripts dir) or run from
# the scripts dir directly. Mirrors classify.py / store.py's sys.path pattern so the
# imports below resolve in both cases. ``lib/`` is this file's parent; the scripts dir
# (which holds ``store.py``) is its grandparent.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (import must follow the sys.path inserts above)
from lib import log, subproc  # noqa: E402

# Path to the vendored bird-search wrapper (Sub-phase 1's extended Node client).
_BIRD_SEARCH_MJS: Path = _LIB_DIR / "vendor" / "bird-search" / "bird-search.mjs"

# Depth configurations: number of items to request per source. Reused by Sub-phase 3
# to derive the per-run deep-pull budget for handle rotation.
DEPTH_CONFIG: Dict[str, int] = {
    "quick": 12,
    "default": 30,
    "deep": 60,
}

# Env var carrying the logged-in user's NUMERIC X user id (rest_id). The Following
# GraphQL op requires a numeric userId, and the vendored CLI's ``--whoami`` returns
# only the cookie SOURCE string (not the id) — so the self-id is supplied as config.
# See :func:`_resolve_self_user_id`.
X_USER_ID_ENV_VAR: str = "X_USER_ID"

# Timeout (seconds) for the Following subprocess. The full follow list paginates, so
# this is generous relative to a single search.
_FOLLOWING_TIMEOUT_SECONDS: int = 90

# Timeout (seconds) for a single per-handle ``from:<handle>`` SearchTimeline subprocess.
# A single handle's recent timeline is small; matches the reference's per-handle timeout.
_SEARCH_TIMELINE_TIMEOUT_SECONDS: int = 30

# Inter-request delay (seconds) applied BETWEEN per-handle SearchTimeline calls in the
# Python loop. This paces HANDLES (the Node client's ``pageDelayMs`` separately paces
# PAGES inside one handle's pagination). Conservative first-cut for X's ToS-gray posture
# (reference/integrations.md §2); the maintainer tunes against real rate-limit behavior.
INTER_REQUEST_DELAY_SECONDS: float = 1.5

# Max concurrent per-handle SearchTimeline subprocesses. Capped conservatively at 3
# (NOT the reference's 5) because cookie-based X reads are ToS-gray — high concurrency
# raises the rate-limit / account-flag risk (reference/integrations.md §2).
_MAX_CONCURRENT_HANDLE_PULLS: int = 3

# Where the README documents re-logging-in to X. Surfaced in auth-error messages so a
# stuck user knows exactly where to look (brief §8.6).
_README_TROUBLESHOOTING_REF: str = "the README troubleshooting section (§8.6)"

# Module-level credentials injected from config at runtime. NEVER logged, NEVER
# serialized — only merged into the Node subprocess env by :func:`_subprocess_env`.
_credentials: Dict[str, str] = {}


class XAuthError(Exception):
    """Raised when X cookies are absent/expired or the self user id is unresolved.

    The message is actionable (points the user at re-logging-in to X and the README
    troubleshooting section) and NEVER contains a credential value.
    """


@dataclass
class Follow:
    """A single followed X creator, matching ``--following --json`` output exactly.

    Attributes:
        creator_handle: The followed account's screen name / username (no leading ``@``).
        display_name: The followed account's human-readable display name.
        rest_id: The followed account's numeric user id, as a string.
    """

    creator_handle: str
    display_name: str
    rest_id: str


@dataclass
class Tweet:
    """A single new tweet pulled from a followed handle's ``from:<handle>`` timeline.

    Carries exactly what the downstream pipeline needs (Sub-phase 4 maps a :class:`Tweet`
    to the shared ``RankableItem`` and builds an x.com card): the text to classify, the
    ``tweet_id`` (the delta key + the card's status-url tail), the author ``handle``,
    ``created_at``, and the four engagement counts used by ranking.

    Attributes:
        text: The tweet body text.
        tweet_id: The tweet's numeric id as a string — the delta key (``store.get_seen_ids``
            / ``store.mark_seen``) and the tail of ``https://x.com/<handle>/status/<tweet_id>``.
        handle: The author's screen name / username (no leading ``@``).
        created_at: The tweet's creation timestamp, as emitted by the CLI (raw string).
        like_count: Like count, or None if absent in the payload.
        retweet_count: Retweet count, or None if absent.
        reply_count: Reply count, or None if absent.
        quote_count: Quote count, or None if absent.
    """

    text: str
    tweet_id: str
    handle: str
    created_at: str
    like_count: Optional[int] = None
    retweet_count: Optional[int] = None
    reply_count: Optional[int] = None
    quote_count: Optional[int] = None


def set_credentials(auth_token: Optional[str], ct0: Optional[str]) -> None:
    """Inject X session cookies so the Node subprocess can authenticate.

    Cookies are held in a module-level dict and merged into the subprocess env by
    :func:`_subprocess_env`. They are NEVER logged or written to disk.

    Args:
        auth_token: The X ``auth_token`` cookie value, or None to leave unset.
        ct0: The X ``ct0`` (CSRF) cookie value, or None to leave unset.

    Example:
        >>> set_credentials("dummy_auth", "dummy_ct0")
    """
    if auth_token:
        _credentials["AUTH_TOKEN"] = auth_token
    if ct0:
        _credentials["CT0"] = ct0


def _has_injected_credentials() -> bool:
    """Return True when both X session cookies were injected via :func:`set_credentials`."""
    return bool(_credentials.get("AUTH_TOKEN") and _credentials.get("CT0"))


def _has_process_credentials() -> bool:
    """Return True when ``AUTH_TOKEN`` and ``CT0`` are present in the process env."""
    return bool(os.environ.get("AUTH_TOKEN") and os.environ.get("CT0"))


def _subprocess_env() -> Dict[str, str]:
    """Build the env dict for the Node subprocess, merging injected credentials.

    Disables the Node client's browser-cookie fallback (``BIRD_DISABLE_BROWSER_COOKIES``)
    by default so normal runs never trigger a Safari/Chrome Keychain prompt; when no
    cookies are injected/in-env the Node ``cookies.js`` would otherwise reach for the
    browser store. The returned dict is passed to ``subproc.run_with_timeout(env=...)``
    and never logged.

    Returns:
        A copy of the process env with injected ``AUTH_TOKEN``/``CT0`` merged in and
        ``BIRD_DISABLE_BROWSER_COOKIES=1`` set.
    """
    env = os.environ.copy()
    env.update(_credentials)
    env["BIRD_DISABLE_BROWSER_COOKIES"] = "1"
    return env


def is_bird_installed() -> bool:
    """Check that the vendored bird-search client and Node.js are available.

    Returns:
        True if ``bird-search.mjs`` exists and ``node`` is on PATH.
    """
    if not _BIRD_SEARCH_MJS.exists():
        return False
    return shutil.which("node") is not None


def _resolve_self_user_id() -> str:
    """Resolve the logged-in user's NUMERIC X user id from config/env.

    The Following GraphQL op requires a numeric ``userId``. The vendored CLI's
    ``--whoami`` returns only the cookie SOURCE string (e.g. ``"Chrome"``), NOT the
    numeric id, and the read-only Node client has no screen_name→id lookup. So the
    self-id is supplied as config via the :data:`X_USER_ID_ENV_VAR` env var — the
    simplest honest path (the prompt's option (a)): we pass the numeric id we have
    rather than handing the CLI a screen_name it will reject.

    Returns:
        The numeric user id as a string.

    Raises:
        XAuthError: If the env var is unset or non-numeric — raised LOUD (Rule 12),
            never a silent no-op.
    """
    raw = os.environ.get(X_USER_ID_ENV_VAR, "").strip()
    if not raw or not raw.isdigit():
        raise XAuthError(
            f"X user id not configured: set the {X_USER_ID_ENV_VAR} environment "
            f"variable to your numeric X user id (the Following list is fetched by "
            f"numeric userId, not screen name). See {_README_TROUBLESHOOTING_REF}."
        )
    return raw


def _is_auth_failure(returncode: int, parsed: Any, stderr: str) -> bool:
    """Decide whether a subprocess result signals an X auth failure.

    The vendored CLI surfaces auth failure as one of: a JSON ``{"error": "...credentials..."}``
    object, a JSON ``{"authenticated": false}`` object (the ``--check`` shape), or a
    non-zero exit with a credentials-related stderr message.

    Args:
        returncode: The subprocess return code.
        parsed: The parsed stdout JSON (any shape), or None if stdout was not JSON.
        stderr: The subprocess stderr text.

    Returns:
        True if the result indicates absent/expired credentials.
    """
    if isinstance(parsed, dict):
        if parsed.get("authenticated") is False:
            return True
        error_text = str(parsed.get("error", "")).lower()
        if error_text and ("credential" in error_text or "auth" in error_text or "cookie" in error_text):
            return True
    if returncode != 0:
        stderr_lower = stderr.lower()
        if "credential" in stderr_lower or "not authenticated" in stderr_lower or "auth" in stderr_lower:
            return True
    return False


def load_x_following(cookie_source: str) -> List[Follow]:
    """Load the logged-in user's X Following list as typed :class:`Follow` records.

    Resolves cookies at runtime (priority: injected via :func:`set_credentials` → env
    ``AUTH_TOKEN``/``CT0`` → the Node client's browser cookie store), resolves the
    user's numeric self-id from :data:`X_USER_ID_ENV_VAR`, then shells out to
    ``bird-search.mjs --following <id> --json`` via :func:`lib.subproc.run_with_timeout`
    and parses the JSON array into :class:`Follow` records.

    The ``cookie_source`` argument mirrors the YouTube loader's signature so Stage 0
    wiring treats both platforms uniformly; it is the browser name (e.g. ``"chrome"``)
    or ``"env"`` and is recorded for logging only — actual cookies flow via env, never
    as a CLI arg, and the value is treated as a non-secret hint.

    Args:
        cookie_source: Browser name to read cookies from (e.g. ``"chrome"``) or
            ``"env"`` to defer to ``AUTH_TOKEN``/``CT0``. Never a raw cookie value.

    Returns:
        A list of :class:`Follow` records (possibly empty if the user follows no one).

    Raises:
        XAuthError: If cookies are absent/expired or the numeric self-id is
            unconfigured — the message points at re-logging-in to X and the README
            troubleshooting section. Loud, not a silent no-op (Rule 12).

    Example:
        >>> set_credentials("dummy_auth", "dummy_ct0")  # doctest: +SKIP
        >>> follows = load_x_following("chrome")  # doctest: +SKIP
        >>> follows[0].creator_handle  # doctest: +SKIP
        'alice'
    """
    if not is_bird_installed():
        raise XAuthError(
            f"X client unavailable: the vendored bird-search client or Node.js is "
            f"missing. Install Node.js 22+ and ensure {_BIRD_SEARCH_MJS.name} is present. "
            f"See {_README_TROUBLESHOOTING_REF}."
        )

    self_user_id = _resolve_self_user_id()

    log.log_info(
        "x_following_load_started",
        cookie_source=cookie_source,
        self_user_id=self_user_id,
    )

    command = ["node", str(_BIRD_SEARCH_MJS), "--following", self_user_id, "--json"]

    try:
        result = subproc.run_with_timeout(
            command,
            timeout=_FOLLOWING_TIMEOUT_SECONDS,
            env=_subprocess_env(),
        )
    except subproc.SubprocTimeout as exc:
        log.log_error(
            "x_following_load_timed_out",
            fix_suggestion="Check network connectivity to X; the Following list can be large.",
            cookie_source=cookie_source,
        )
        raise XAuthError(
            f"Loading your X Following list timed out after {_FOLLOWING_TIMEOUT_SECONDS}s. "
            f"Check your connection and retry. See {_README_TROUBLESHOOTING_REF}."
        ) from exc
    except OSError as exc:
        # Reason: str(exc) here is an OS spawn error (e.g. node not found), never a
        # credential — safe to surface. Cookies never reach this path.
        log.log_error(
            "x_following_load_spawn_failed",
            fix_suggestion="Ensure Node.js 22+ is installed and on PATH.",
            cookie_source=cookie_source,
        )
        raise XAuthError(
            f"Could not start the X client: {exc}. Ensure Node.js is installed. "
            f"See {_README_TROUBLESHOOTING_REF}."
        ) from exc

    parsed = _parse_stdout(result.stdout)

    if _is_auth_failure(result.returncode, parsed, result.stderr):
        log.log_error(
            "x_following_auth_failed",
            fix_suggestion=f"Log into X in your browser, then re-run. See {_README_TROUBLESHOOTING_REF}.",
            cookie_source=cookie_source,
            returncode=result.returncode,
        )
        raise XAuthError(
            "X authentication failed: your X session cookies are missing or expired. "
            "Log into X in your browser, then re-run Orbit. "
            f"See {_README_TROUBLESHOOTING_REF}."
        )

    if result.returncode != 0:
        # Reason: non-auth, non-zero exit (e.g. transient GraphQL error). stderr may
        # carry an X error message but never a credential (those are only in headers).
        error_detail = result.stderr.strip() or "unknown error"
        log.log_error(
            "x_following_load_failed",
            fix_suggestion="Retry; if it persists, check the README troubleshooting section.",
            cookie_source=cookie_source,
            returncode=result.returncode,
            error_detail=error_detail,
        )
        raise XAuthError(
            f"Loading your X Following list failed: {error_detail}. "
            f"See {_README_TROUBLESHOOTING_REF}."
        )

    if isinstance(parsed, dict) and parsed.get("error"):
        # The CLI's --json error shape: {"error": "...", "items": []}. If it slipped
        # past the auth check (a non-credential error), surface it loud.
        error_detail = str(parsed.get("error"))
        log.log_error(
            "x_following_load_error_payload",
            fix_suggestion=f"See {_README_TROUBLESHOOTING_REF}.",
            cookie_source=cookie_source,
            error_detail=error_detail,
        )
        raise XAuthError(
            f"Loading your X Following list failed: {error_detail}. "
            f"See {_README_TROUBLESHOOTING_REF}."
        )

    follows = _parse_follows(parsed)
    log.log_info(
        "x_following_load_completed",
        cookie_source=cookie_source,
        total_follows=len(follows),
    )
    return follows


def _parse_stdout(stdout: str) -> Any:
    """Parse subprocess stdout as JSON, returning None when it is not JSON.

    Args:
        stdout: Raw subprocess stdout.

    Returns:
        The parsed JSON value, or None when stdout is empty or not valid JSON.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_follows(parsed: Any) -> List[Follow]:
    """Convert parsed ``--following --json`` output into :class:`Follow` records.

    Args:
        parsed: The parsed JSON — a list of follow dicts on success.

    Returns:
        A list of :class:`Follow` records. Entries missing a ``creator_handle`` or
        ``rest_id`` are skipped (a malformed entry must not crash the load).
    """
    if not isinstance(parsed, list):
        return []
    follows: List[Follow] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        creator_handle = str(entry.get("creator_handle", "")).lstrip("@").strip()
        rest_id = str(entry.get("rest_id", "")).strip()
        if not creator_handle or not rest_id:
            continue
        display_name = str(entry.get("display_name", "") or creator_handle).strip()
        follows.append(
            Follow(creator_handle=creator_handle, display_name=display_name, rest_id=rest_id)
        )
    return follows


def persist_following(follows: List[Follow]) -> int:
    """Upsert each :class:`Follow` into the shared ``sources`` table as ``platform="x"``.

    Each follow becomes a ``signal``-category source keyed on
    ``(platform="x", external_id=creator_handle)`` so a repeat load updates rather than
    duplicates (the ``UNIQUE(platform, external_id)`` upsert in
    :func:`store.upsert_source`). After this call the follows are queryable via
    ``store.list_sources(platform="x")``.

    Args:
        follows: The follows to persist (typically from :func:`load_x_following`).

    Returns:
        The number of follows persisted.

    Example:
        >>> count = persist_following([Follow("alice", "Alice", "1001")])  # doctest: +SKIP
        >>> count  # doctest: +SKIP
        1
    """
    refreshed_at = datetime.now(timezone.utc).isoformat()
    persisted_count = 0
    for follow in follows:
        store.upsert_source(
            platform="x",
            external_id=follow.creator_handle,
            display_name=follow.display_name,
            category="signal",
            last_refreshed_at=refreshed_at,
        )
        persisted_count += 1
    log.log_info("x_following_persisted", total_persisted=persisted_count)
    return persisted_count


# --- Stage 1: per-handle SearchTimeline delta (rotation + pacing) -----------


def _first_present(*values: Any) -> Any:
    """Return the first non-None value, or None if all are None.

    Used to coalesce the camelCase / snake_case engagement field variants the CLI
    may emit (``likeCount`` vs ``like_count``) into one value.

    Args:
        *values: Candidate values, most-preferred first.

    Returns:
        The first value that is not None, else None.
    """
    for value in values:
        if value is not None:
            return value
    return None


def _coerce_optional_int(value: Any) -> Optional[int]:
    """Coerce a raw engagement value to ``int``, returning None when not coercible.

    Args:
        value: A raw count (str/int/None) from the parsed CLI payload.

    Returns:
        The int value, or None when the value is None or non-numeric.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def select_rotation_window(handles: List[str], depth: str, run_day_ordinal: int) -> List[str]:
    """Select the deep-pull handle window via deterministic round-robin rotation.

    The per-run deep-pull budget is ``DEPTH_CONFIG[depth]`` interpreted as the MAX
    NUMBER OF HANDLES pulled this run (a handle-count cap, NOT a per-handle item count).
    When ``len(handles) <= budget`` every handle is pulled (no rotation needed). When
    ``len(handles) > budget`` a window of size ``budget`` is taken starting at offset
    ``run_day_ordinal % len(handles)``, wrapping around — so over successive
    ``run_day_ordinal`` values the windows rotate to cover every handle (the resolved
    Q5 fairness policy: a high-follow user is never permanently starved).

    The caller is responsible for ordering ``handles`` STABLY first (this module orders
    X sources by ``source_id`` ascending — see :func:`fetch_new_tweets`).

    Args:
        handles: The stably-ordered list of candidate handles.
        depth: ``quick`` | ``default`` | ``deep`` — selects the budget from
            :data:`DEPTH_CONFIG`.
        run_day_ordinal: The run's day ordinal; rotates the window across runs.

    Returns:
        The selected window of handles (length ``min(len(handles), budget)``).

    Example:
        >>> select_rotation_window(["a", "b", "c", "d", "e"], "quick", 0)  # budget 12 >= 5
        ['a', 'b', 'c', 'd', 'e']
    """
    if not handles:
        return []
    budget = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    total = len(handles)
    if total <= budget:
        return list(handles)
    offset = run_day_ordinal % total
    # Reason: wrap-around window of size ``budget`` starting at ``offset``; a plain slice
    # would truncate at the end of the list and never cover the wrapped-around handles.
    return [handles[(offset + step) % total] for step in range(budget)]


def _parse_tweets(parsed: Any, handle: str) -> List[Tweet]:
    """Convert a parsed ``from:<handle>`` SearchTimeline payload into :class:`Tweet`s.

    The vendored CLI emits a JSON array of tweet dicts. Each tweet's id and text are
    required; engagement fields are optional and may arrive camelCase (``likeCount``)
    or snake_case (``like_count``). The author handle is taken from the payload when
    present, else falls back to the queried ``handle`` (a ``from:<handle>`` query only
    returns that handle's tweets). Entries missing an id are skipped (a malformed entry
    must not crash the whole pull).

    Args:
        parsed: The parsed JSON — a list of tweet dicts on success.
        handle: The handle that was queried, used as the author fallback.

    Returns:
        A list of :class:`Tweet` records.
    """
    if not isinstance(parsed, list):
        return []
    tweets: List[Tweet] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        tweet_id = str(entry.get("id", "") or "").strip()
        if not tweet_id:
            continue
        author = entry.get("author") or entry.get("user") or {}
        author_handle = ""
        if isinstance(author, dict):
            author_handle = str(author.get("username") or author.get("screen_name") or "").strip()
        author_handle = (author_handle or handle).lstrip("@")
        text = str(_first_present(entry.get("text"), entry.get("full_text"), "") or "").strip()
        created_at = str(_first_present(entry.get("createdAt"), entry.get("created_at"), "") or "").strip()
        tweets.append(
            Tweet(
                text=text,
                tweet_id=tweet_id,
                handle=author_handle,
                created_at=created_at,
                like_count=_coerce_optional_int(
                    _first_present(entry.get("likeCount"), entry.get("like_count"), entry.get("favorite_count"))
                ),
                retweet_count=_coerce_optional_int(
                    _first_present(entry.get("retweetCount"), entry.get("retweet_count"))
                ),
                reply_count=_coerce_optional_int(
                    _first_present(entry.get("replyCount"), entry.get("reply_count"))
                ),
                quote_count=_coerce_optional_int(
                    _first_present(entry.get("quoteCount"), entry.get("quote_count"))
                ),
            )
        )
    return tweets


def _pull_handle_tweets(handle: str, depth: str) -> List[Tweet]:
    """Run ``from:<handle>`` SearchTimeline once and parse the result into :class:`Tweet`s.

    Shells out to ``bird-search.mjs "from:<handle>" --count <budget> --json`` via
    :func:`lib.subproc.run_with_timeout` (the Node client handles cursor pagination +
    refresh-on-404 internally). A timeout / spawn failure / non-zero exit / error payload
    yields an empty list for THIS handle — one bad handle must not abort the whole run
    (the caller logs the failure and mark_seen is skipped for that handle).

    Args:
        handle: The handle to pull (no leading ``@``).
        depth: Depth, used as the per-handle ``--count`` (item) request.

    Returns:
        The parsed tweets for the handle (empty on any failure).
    """
    handle = handle.lstrip("@")
    per_handle_count = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    command = [
        "node",
        str(_BIRD_SEARCH_MJS),
        f"from:{handle}",
        "--count",
        str(per_handle_count),
        "--json",
    ]
    try:
        result = subproc.run_with_timeout(
            command,
            timeout=_SEARCH_TIMELINE_TIMEOUT_SECONDS,
            env=_subprocess_env(),
        )
    except subproc.SubprocTimeout:
        log.log_warning(
            "x_handle_pull_timed_out",
            handle=handle,
            fix_suggestion="X may be rate-limiting; reduce depth or retry later.",
        )
        return []
    except OSError as exc:
        # Reason: str(exc) is an OS spawn error (e.g. node missing), never a credential.
        log.log_warning("x_handle_pull_spawn_failed", handle=handle, error_detail=str(exc))
        return []

    parsed = _parse_stdout(result.stdout)
    if result.returncode != 0 or (isinstance(parsed, dict) and parsed.get("error")):
        error_detail = (
            str(parsed.get("error")) if isinstance(parsed, dict) and parsed.get("error") else result.stderr.strip()
        ) or "unknown error"
        log.log_warning("x_handle_pull_failed", handle=handle, error_detail=error_detail)
        return []
    return _parse_tweets(parsed, handle)


def fetch_new_tweets(
    sources: List[Dict[str, Any]],
    depth: str,
    run_day_ordinal: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> List[Tweet]:
    """Pull each rotated X handle's recent ``from:<handle>`` tweets as a paced delta.

    The Stage-1 entry point for the X half. Deterministic, no LLM (Rule 5): rotation,
    delta-filtering and pacing are all code.

    1. Order the X ``sources`` STABLY by ``source_id`` ascending.
    2. Select the deep-pull window via :func:`select_rotation_window` — the budget is
       ``DEPTH_CONFIG[depth]`` interpreted as the MAX NUMBER OF HANDLES this run, so over
       successive ``run_day_ordinal`` values coverage rotates across every handle even
       when the follow count exceeds the budget (resolved Q5 fairness policy).
    3. For each selected handle, run ``from:<handle>`` SearchTimeline (subprocess; the
       Node client paginates + refreshes-on-404), parse to :class:`Tweet`s, FILTER to
       only those whose ``tweet_id`` is NOT in ``store.get_seen_ids(source_id)`` (delta),
       then ``store.mark_seen(source_id, tweet_id)`` ONLY after a successful fetch+parse.
    4. Pace: a bounded ThreadPool (``max_workers`` capped at
       :data:`_MAX_CONCURRENT_HANDLE_PULLS` = 3, ToS-gray posture) plus an injected
       ``sleeper`` invoked :data:`INTER_REQUEST_DELAY_SECONDS` once per submitted handle
       past the first, so handles are spaced out (the test injects a no-op sleeper).

    Args:
        sources: X source rows (from ``store.list_sources(platform="x")``); each carries
            ``source_id`` and ``external_id`` (= handle). Non-``x`` rows are ignored.
        depth: ``quick`` | ``default`` | ``deep`` — selects the handle budget.
        run_day_ordinal: The run's day ordinal; rotates which handles are pulled.
        sleeper: Injected sleep function (defaults to :func:`time.sleep`); the inter-handle
            delay calls this so pacing is mockable without real sleeping in tests.

    Returns:
        The list of NEW :class:`Tweet`s across all selected handles (delta-filtered).

    Example:
        >>> rows = store.list_sources(platform="x")  # doctest: +SKIP
        >>> new_tweets = fetch_new_tweets(rows, "default", run_day_ordinal=0)  # doctest: +SKIP
    """
    # Reason: ``source_id`` ascending is the stable ordering key (persistence order,
    # deterministic across runs) that the rotation window indexes into.
    x_sources = sorted(
        (s for s in sources if s.get("platform") == "x" and s.get("external_id")),
        key=lambda s: int(s["source_id"]),
    )
    handles = [str(s["external_id"]) for s in x_sources]
    source_id_by_handle: Dict[str, int] = {str(s["external_id"]): int(s["source_id"]) for s in x_sources}

    selected_handles = select_rotation_window(handles, depth, run_day_ordinal)
    log.log_info(
        "x_fetch_new_tweets_started",
        depth=depth,
        run_day_ordinal=run_day_ordinal,
        total_handles=len(handles),
        selected_handles=len(selected_handles),
    )

    new_tweets: List[Tweet] = []
    max_workers = max(1, min(_MAX_CONCURRENT_HANDLE_PULLS, len(selected_handles)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_handle = {}
        for index, handle in enumerate(selected_handles):
            # Pace HANDLES: delay before every submission past the first so the loop does
            # not fan out all handles instantly (the Node ``pageDelayMs`` only paces pages
            # WITHIN a handle). Injected ``sleeper`` makes this mockable.
            if index > 0:
                sleeper(INTER_REQUEST_DELAY_SECONDS)
            future_to_handle[executor.submit(_pull_handle_tweets, handle, depth)] = handle

        for future in as_completed(future_to_handle):
            handle = future_to_handle[future]
            source_id = source_id_by_handle[handle]
            tweets = future.result()
            seen_ids = store.get_seen_ids(source_id)
            handle_new_count = 0
            for tweet in tweets:
                if tweet.tweet_id in seen_ids:
                    continue
                new_tweets.append(tweet)
                # mark_seen ONLY after a successful fetch+parse for this handle.
                store.mark_seen(source_id, tweet.tweet_id)
                handle_new_count += 1
            log.log_info(
                "x_handle_delta_completed",
                handle=handle,
                fetched=len(tweets),
                new=handle_new_count,
            )

    log.log_info("x_fetch_new_tweets_completed", total_new_tweets=len(new_tweets))
    return new_tweets
