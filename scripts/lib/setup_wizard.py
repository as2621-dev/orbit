"""The `/orbit --setup` first-run wizard + OS-cron-entry generation (Phase 6 / Sub-phase 2).

Implements the 5-step setup from brief §8.3: read the user's YouTube subscriptions and
X follows (M1/M2 loaders), auto-classify each creator's recent titles into signal/noise
via the EXISTING two-axis classify path (:func:`lib.classify.classify_item` — no separate
classifier), present categories for confirmation, let the user pick priority creators
(``creator_weights``), seed ``interests`` from subscription titles, set the delivery
target + schedule, write ``orbit.config.json`` matching ``reference/api-contracts.md``,
and PRINT the exact OS cron entry (``<cron_expr> cd <repo> && claude -p "/orbit"``).

Rule 5 discipline: the ONLY model use is the auto-classify judgment call (routed through
the injectable :data:`lib.classify.LlmClassifier`). EVERYTHING else — cron-string
building, prompt routing, weight assembly, IO — is deterministic code.

Dependency-injection discipline (so tests never touch live services): the subscription
loader, the X-following loader, the LLM classifier, the interactive ``input`` function,
and the config-output path are ALL injectable. The defaults wire the real loaders /
``input`` / config path; tests inject mocks + a tmp path.

Stdlib-only (pydantic is NOT an Orbit dependency).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.setup_wizard`` (via orbit.py's sys.path insert of the scripts dir) or run from
# the scripts dir directly. Mirrors config.py / youtube_yt.py's sys.path pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402
from lib import classify, log  # noqa: E402
from lib.bird_x import Follow, XAuthError, load_x_following  # noqa: E402
from lib.classify import LlmClassifier, _default_llm_classifier  # noqa: E402
from lib.config import DEFAULT_CONFIG_FILENAME, DEFAULT_SCHEDULE, is_valid_cron_expression  # noqa: E402
from lib.youtube_yt import Subscription, load_youtube_subscriptions  # noqa: E402

# The default scheduler command, per brief §8.3 step 5 / §2 (OS cron -> claude -p "/orbit").
# ``{repo}`` is the directory the user runs Orbit from; the cron line cds there first so
# the relative ``orbit.config.json`` resolves the same way a manual run would.
_DEFAULT_CRON_COMMAND_TEMPLATE: str = 'cd {repo} && claude -p --dangerously-skip-permissions "/orbit"'

# A weight applied to every creator the user marks as priority. The brief leaves the
# exact value to the maintainer; 2.0 is a clear "thumb on the scale" (api-contracts.md
# shows weights like 1.5/2.0) — config is user-editable afterward.
_PRIORITY_CREATOR_WEIGHT: float = 2.0

# Default delivery HTML path written when the user accepts the default (api-contracts.md).
_DEFAULT_HTML_PATH: str = "~/orbit/out/today.html"

# The classify Axis-A prior every channel starts from before the model judges it: a
# subscription is a signal source by default (mirrors persist_subscriptions' "signal").
_DEFAULT_CHANNEL_CATEGORY: str = "signal"


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
        '0 7 * * * cd /home/me/orbit && claude -p "/orbit"'
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


def _prompt(input_fn: Callable[[str], str], message: str, default: str = "") -> str:
    """Ask the user one question via the injected ``input_fn``, returning a stripped answer.

    All wizard interactivity routes through ``input_fn`` (defaults to builtin ``input``)
    so tests script answers deterministically — the wizard NEVER calls ``input`` directly.
    An empty answer falls back to ``default``.

    Args:
        input_fn: The injected input function (``input`` in production, a scripted
            callable in tests).
        message: The prompt text shown to the user.
        default: The value used when the user enters nothing.

    Returns:
        The user's stripped answer, or ``default`` if the answer was empty.
    """
    suffix = f" [{default}]" if default else ""
    answer = input_fn(f"{message}{suffix}: ").strip()
    return answer or default


def _is_yes(answer: str) -> bool:
    """Return True for an affirmative answer (``y``/``yes``, case-insensitive)."""
    return answer.strip().lower() in ("y", "yes")


def _classify_creator(
    display_name: str,
    *,
    interests: list[str],
    llm_classifier: LlmClassifier,
    store_module: Any = store,
) -> str:
    """Auto-classify ONE creator into ``signal``/``noise`` via the EXISTING classify path.

    Uses :func:`lib.classify.classify_item` (NOT a separate classifier — DoD) with the
    creator's title/name as the classify body, so the same two-axis judgment the pipeline
    uses decides the channel category. The injected ``llm_classifier`` is the only model
    use (Rule 5); tests inject a mock returning a scripted JSON verdict.

    Args:
        display_name: The creator's title/name, used as the classify input body.
        interests: The user's interest keywords (drives Axis B).
        llm_classifier: The injectable LLM boundary (mocked in tests).

    Returns:
        ``"signal"`` when Axis A judged the creator signal, else ``"noise"``.
    """
    # Reason: classify_item reads ``title``/``text`` off the item; a plain dict carrying
    # ``title`` reuses the exact pipeline classify path without inventing a new shape.
    item = {"video_id": f"setup::{display_name}", "title": display_name, "description": display_name}
    classification = classify.classify_item(
        item,
        channel_category=_DEFAULT_CHANNEL_CATEGORY,
        interests=interests,
        llm_classifier=llm_classifier,
        store_module=store_module,
    )
    return "signal" if classification.axis_a_signal == 1 else "noise"


def _seed_interests_from_subscriptions(subscriptions: list[Subscription]) -> list[str]:
    """Seed a de-duplicated interest list from subscription display names (brief §8.3 step 3).

    First-run interests are auto-seeded from what the user already follows
    (api-contracts.md: "Seeded from subs on first run, user-editable"). Deterministic:
    each channel's display name becomes a candidate keyword, lower-cased and de-duplicated
    while preserving first-seen order. The user edits ``interests`` in the config later.

    Args:
        subscriptions: The loaded YouTube subscriptions.

    Returns:
        A de-duplicated list of seed interest keywords (possibly empty).
    """
    seen: set[str] = set()
    seeds: list[str] = []
    for subscription in subscriptions:
        keyword = subscription.display_name.strip().lower()
        if keyword and keyword not in seen:
            seen.add(keyword)
            seeds.append(keyword)
    return seeds


def _load_youtube_subscriptions_safe(
    cookie_source: str,
    loader: Callable[[str], list[Subscription]],
) -> list[Subscription]:
    """Load YouTube subscriptions, returning ``[]`` on an empty load.

    YouTube is the core source; an auth failure here is fatal and propagates (the caller
    surfaces it). This wrapper exists only to keep :func:`run_setup_wizard` readable.

    Args:
        cookie_source: Browser name (or ``"env"``) passed to the loader.
        loader: The (injectable) subscription loader.

    Returns:
        The loaded subscriptions (possibly empty).
    """
    subscriptions = loader(cookie_source)
    log.log_info("setup_youtube_subscriptions_loaded", count=len(subscriptions))
    return subscriptions


def _load_x_following_best_effort(
    cookie_source: str,
    x_loader: Callable[[str], list[Follow]],
) -> list[Follow]:
    """Load X follows best-effort: an :class:`XAuthError` is logged + swallowed (YouTube-only).

    Mirrors orbit.py Stage 0's posture — X is an ADDITIVE source, so an unconfigured /
    expired X session must not abort setup. The user still gets a valid YouTube-only
    config.

    Args:
        cookie_source: Browser name (or ``"env"``) passed to the loader.
        x_loader: The (injectable) X-following loader.

    Returns:
        The loaded follows, or ``[]`` when X is unavailable.
    """
    try:
        follows = x_loader(cookie_source)
    except XAuthError as exc:
        log.log_warning(
            "setup_x_following_skipped",
            fix_suggestion=(
                "X following not loaded (auth/config). Set AUTH_TOKEN/CT0 + X_USER_ID to "
                "include X; setup continues YouTube-only."
            ),
            error_message=str(exc),
        )
        return []
    log.log_info("setup_x_following_loaded", count=len(follows))
    return follows


def _confirm_categories(
    creators: list[tuple[str, str]],
    *,
    interests: list[str],
    llm_classifier: LlmClassifier,
    input_fn: Callable[[str], str],
    store_module: Any = store,
) -> dict[str, str]:
    """Auto-classify each creator then let the user confirm/flip the category (brief §8.3 step 3).

    For each ``(external_id, display_name)`` the wizard auto-classifies via the existing
    classify path, shows the verdict, and asks the user to keep it (Enter) or flip it
    (answer ``n``). Deterministic except the per-creator classify judgment call (Rule 5).

    Args:
        creators: ``(external_id, display_name)`` pairs across YouTube + X.
        interests: The user's interest keywords (drives Axis B in the classify call).
        llm_classifier: The injectable LLM boundary (mocked in tests).
        input_fn: The injectable input function (scripted in tests).

    Returns:
        A map of ``external_id`` -> confirmed category (``"signal"`` | ``"noise"``).
    """
    categories: dict[str, str] = {}
    for external_id, display_name in creators:
        auto_category = _classify_creator(
            display_name, interests=interests, llm_classifier=llm_classifier, store_module=store_module
        )
        answer = _prompt(
            input_fn,
            f"'{display_name}' classified as {auto_category}. Keep this? (y/n)",
            default="y",
        )
        if _is_yes(answer):
            categories[external_id] = auto_category
        else:
            # Reason: a single binary flip — the user disagrees with the auto verdict, so
            # invert it (signal <-> noise) rather than re-prompting for a free-text label.
            categories[external_id] = "noise" if auto_category == "signal" else "signal"
    return categories


def _pick_priority_creators(
    creators: list[tuple[str, str]],
    *,
    input_fn: Callable[[str], str],
) -> dict[str, float]:
    """Let the user pick priority creators, building the ``creator_weights`` map (step 3).

    Shows each creator and asks whether to prioritize it; a yes assigns
    :data:`_PRIORITY_CREATOR_WEIGHT`. Deterministic — no model. Creators the user does not
    prioritize are simply absent from the map (a weight of 1.0 is the implicit baseline in
    ranking).

    Args:
        creators: ``(external_id, display_name)`` pairs across YouTube + X.
        input_fn: The injectable input function (scripted in tests).

    Returns:
        A map of ``external_id`` -> priority weight (float), for the chosen creators only.
    """
    weights: dict[str, float] = {}
    for external_id, display_name in creators:
        answer = _prompt(
            input_fn,
            f"Prioritize '{display_name}' in your digest? (y/n)",
            default="n",
        )
        if _is_yes(answer):
            weights[external_id] = _PRIORITY_CREATOR_WEIGHT
    return weights


def _gather_delivery(input_fn: Callable[[str], str]) -> dict[str, Any]:
    """Collect the delivery block (``html_path`` + optional ``imessage_to``) — step 4.

    ``html_path`` always has a sane default; ``imessage_to`` is opt-in (empty answer
    leaves it null so Orbit never messages without a configured target — the Sub-phase 3
    opt-in intent). Deterministic.

    Args:
        input_fn: The injectable input function (scripted in tests).

    Returns:
        A delivery dict with ``html_path`` and, when given, ``imessage_to``.
    """
    html_path = _prompt(input_fn, "Where should the digest HTML be written?", default=_DEFAULT_HTML_PATH)
    imessage_to = _prompt(input_fn, "iMessage number for the TL;DR (optional, blank to skip)", default="")
    delivery: dict[str, Any] = {"html_path": html_path}
    if imessage_to:
        delivery["imessage_to"] = imessage_to
    return delivery


def _gather_schedule(input_fn: Callable[[str], str]) -> str:
    """Collect a valid cron schedule, re-prompting until valid or falling back (step 4).

    Asks for a 5-field cron expression (default :data:`lib.config.DEFAULT_SCHEDULE`). An
    invalid entry is rejected loudly (Rule 12) and re-asked once; a second invalid entry
    falls back to the default rather than looping forever in a non-interactive context.

    Args:
        input_fn: The injectable input function (scripted in tests).

    Returns:
        A syntactically valid 5-field cron expression.
    """
    for _attempt in range(2):
        schedule = _prompt(input_fn, "Daily run schedule (cron, 5 fields)", default=DEFAULT_SCHEDULE)
        if is_valid_cron_expression(schedule):
            return schedule
        log.log_warning(
            "setup_invalid_schedule_retry",
            fix_suggestion="Enter a 5-field cron expression like '0 7 * * *' (7am daily).",
            invalid_schedule=schedule,
        )
    log.log_warning(
        "setup_schedule_fallback_to_default",
        fix_suggestion="Edit 'schedule' in orbit.config.json afterward if the default is wrong.",
        default_schedule=DEFAULT_SCHEDULE,
    )
    return DEFAULT_SCHEDULE


def _write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write the assembled config dict to ``config_path`` as pretty JSON (UTF-8).

    Args:
        config: The assembled ``orbit.config.json`` shape (api-contracts.md).
        config_path: The output path (tests pass a tmp path).
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    log.log_info("setup_config_written", config_path=str(config_path))


def run_setup_wizard(
    *,
    cookie_source: str = "chrome",
    config_path: Optional[Path] = None,
    repo_path: Optional[Path] = None,
    youtube_loader: Callable[[str], list[Subscription]] = load_youtube_subscriptions,
    x_loader: Callable[[str], list[Follow]] = load_x_following,
    llm_classifier: LlmClassifier = _default_llm_classifier,
    input_fn: Callable[[str], str] = input,
    store_module: Any = store,
) -> int:
    """Run the interactive first-run wizard (brief §8.3), writing ``orbit.config.json``.

    Steps (deterministic except the per-creator classify judgment call, Rule 5):

      1. Ask for the cookie source (default the injected ``cookie_source``).
      2. Read YouTube subscriptions (injectable loader; fatal on auth failure) and X
         follows (injectable loader; best-effort — an :class:`XAuthError` is swallowed so
         a YouTube-only user still gets a config).
      3. Auto-classify each creator into signal/noise via the EXISTING classify path
         (:func:`lib.classify.classify_item`, NO separate classifier), let the user confirm
         categories, and pick priority creators (``creator_weights``).
      4. Seed ``interests`` from subscription titles; gather the delivery target + schedule.
      5. Write ``orbit.config.json`` (api-contracts.md shape) to ``config_path``, then PRINT
         and log the exact OS cron entry via :func:`generate_cron_entry`.

    ALL external boundaries are injectable so tests run offline: loaders, the LLM
    classifier, the ``input`` function, and the config output path. The defaults wire the
    real loaders / ``input`` / ``./orbit.config.json``.

    Args:
        cookie_source: Default browser name (or ``"env"``) for cookie reading.
        config_path: Where to write the config; defaults to ``./orbit.config.json``.
        repo_path: The repo directory the printed cron entry ``cd``s into; defaults to cwd.
        youtube_loader: Subscription loader; defaults to ``load_youtube_subscriptions``.
        x_loader: X following loader; defaults to ``load_x_following``.
        llm_classifier: The injectable classify LLM boundary; tests inject a mock.
        input_fn: The injectable input function; defaults to builtin ``input``.
        store_module: The store module used by the classify path (injectable; defaults to
            :mod:`store`). Tests inject a mock so auto-classify never touches the real DB.

    Returns:
        Process exit code: 0 on success.
    """
    log.log_info("setup_wizard_started", cookie_source=cookie_source)

    resolved_config_path = config_path if config_path is not None else Path.cwd() / DEFAULT_CONFIG_FILENAME

    chosen_cookie_source = _prompt(
        input_fn, "Which browser holds your logins? (chrome/firefox/safari/edge/brave/env)", default=cookie_source
    )

    subscriptions = _load_youtube_subscriptions_safe(chosen_cookie_source, youtube_loader)
    follows = _load_x_following_best_effort(chosen_cookie_source, x_loader)

    # Build the unified creator list keyed by external_id (channel_id for YT, handle for X).
    creators: list[tuple[str, str]] = [(sub.channel_id, sub.display_name) for sub in subscriptions]
    creators += [(follow.creator_handle, follow.display_name) for follow in follows]

    interests = _seed_interests_from_subscriptions(subscriptions)

    # Step 3: confirm categories (auto-classify via the existing path) + pick priorities.
    _confirm_categories(
        creators, interests=interests, llm_classifier=llm_classifier, input_fn=input_fn, store_module=store_module
    )
    creator_weights = _pick_priority_creators(creators, input_fn=input_fn)

    # Step 4: delivery + schedule.
    delivery = _gather_delivery(input_fn)
    schedule = _gather_schedule(input_fn)

    config: dict[str, Any] = {
        "cookie_source": chosen_cookie_source,
        "creator_weights": creator_weights,
        "interests": interests,
        "depth": "default",
        "delivery": delivery,
        "schedule": schedule,
    }

    _write_config(config, resolved_config_path)

    # Step 5: print the exact OS cron entry for the user to paste into their crontab.
    cron_entry = generate_cron_entry(schedule, repo_path=repo_path)
    log.log_info("setup_cron_entry_generated", cron_entry=cron_entry)
    print("\nAdd this line to your crontab (run `crontab -e`):\n")
    print(f"  {cron_entry}\n")

    log.log_info(
        "setup_wizard_completed",
        cookie_source=chosen_cookie_source,
        creator_count=len(creators),
        priority_creator_count=len(creator_weights),
        interest_count=len(interests),
        schedule=schedule,
    )
    return 0
