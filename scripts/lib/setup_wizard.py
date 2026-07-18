"""The `/orbit --setup` first-run wizard (Phase 6 / Sub-phase 2).

Implements the 5-step setup from brief §8.3: read the user's YouTube subscriptions and
X follows (M1/M2 loaders), auto-classify each creator's recent titles into signal/noise
via the EXISTING two-axis classify path (:func:`lib.classify.classify_item` — no separate
classifier), present categories for confirmation, let the user pick priority creators
(``creator_weights``), seed ``interests`` from subscription titles, set the delivery
target, write ``orbit.config.json`` matching ``reference/api-contracts.md``, and INSTALL
the wake-proof daily-run scheduler — a launchd LaunchAgent installed via
:func:`lib.scheduler.install_daily_scheduler` at the fixed 7am default (which also retires
the legacy orbit crontab line) — falling back to printing manual plist instructions if
``launchctl`` is unavailable.

Rule 5 discipline: the ONLY model use is the auto-classify judgment call (routed through
the injectable :data:`lib.classify.LlmClassifier`). EVERYTHING else — schedule handling,
prompt routing, weight assembly, IO — is deterministic code. All scheduler install (plist
generation, launchctl, cron migration) lives in :mod:`lib.scheduler`; the wizard only calls
into it (the 500-line-file convention drove the extraction — conventions.md §File size).

Dependency-injection discipline (so tests never touch live services): the subscription
loader, the X-following loader, the LLM classifier, the interactive ``input`` function,
the config-output path, and the launchctl/crontab boundaries are ALL injectable. The
defaults wire the real loaders / ``input`` / config path / launchctl; tests inject mocks +
a tmp path.

Stdlib-only (pydantic is NOT an Orbit dependency).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.setup_wizard`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors config.py / youtube_yt.py's sys.path pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402
from lib import classify, log, scheduler  # noqa: E402
from lib.bird_x import Follow, XAuthError, load_x_following  # noqa: E402
from lib.classify import LlmClassifier, _default_llm_classifier  # noqa: E402
from lib.config import DEFAULT_CONFIG_FILENAME, DEFAULT_SCHEDULE  # noqa: E402
from lib.youtube_yt import Subscription, load_youtube_subscriptions  # noqa: E402

# A weight applied to every creator the user marks as priority. The brief leaves the
# exact value to the maintainer; 2.0 is a clear "thumb on the scale" (api-contracts.md
# shows weights like 1.5/2.0) — config is user-editable afterward.
_PRIORITY_CREATOR_WEIGHT: float = 2.0

# Default delivery HTML path written when the user accepts the default (api-contracts.md).
_DEFAULT_HTML_PATH: str = "~/orbit/out/today.html"

# The classify Axis-A prior every channel starts from before the model judges it: a
# subscription is a signal source by default (mirrors persist_subscriptions' "signal").
_DEFAULT_CHANNEL_CATEGORY: str = "signal"


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


def _flip_category(category: str) -> str:
    """Invert a signal/noise category — the user's single binary disagreement with the default."""
    return "noise" if category == "signal" else "signal"


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
    sources: list[tuple[str, str, str]],
    *,
    interests: list[str],
    llm_classifier: LlmClassifier,
    input_fn: Callable[[str], str],
    store_module: Any = store,
) -> dict[str, str]:
    """Confirm each creator's signal/noise category and PERSIST it as a user override (step 3).

    For each ``(platform, external_id, display_name)``:

      * If the store already holds a USER-SET category for this source — a prior setup run left
        ``category_is_user_override == 1`` — present the STORED value as the default and do NOT
        re-classify. The user adjusts a prior choice instead of redoing it, mirroring the sacred
        "a user override is never re-judged" rule the daily classify path already honors.
      * Otherwise auto-classify via the existing classify path (:func:`_classify_creator` — the
        only model use, Rule 5), show the verdict, and let the user keep (Enter) or flip it.

    The confirmed category is written to ``sources`` with ``is_user_override=1`` via
    :func:`store.upsert_source`, so it takes effect immediately AND is frozen against the weekly
    YouTube / daily X source refresh (which re-upserts every source with a hardcoded ``signal``
    prior). Persisting here is the fix for the discard bug — previously this map was returned and
    dropped, so the user's marks never reached the DB. ``platform`` is carried so each write
    lands under the correct ``UNIQUE(platform, external_id)`` key.

    Args:
        sources: ``(platform, external_id, display_name)`` triples across YouTube + X.
        interests: The user's interest keywords (drives Axis B in the classify call).
        llm_classifier: The injectable LLM boundary (mocked in tests).
        input_fn: The injectable input function (scripted in tests).
        store_module: The store module (injectable; defaults to :mod:`store`). Tests inject a
            mock (offline) or the real store pointed at a temp DB (to assert the persisted value).

    Returns:
        A map of ``external_id`` -> confirmed category (``"signal"`` | ``"noise"``).
    """
    categories: dict[str, str] = {}
    for platform, external_id, display_name in sources:
        existing = store_module.get_source(platform, external_id)
        if existing and existing["category_is_user_override"] == 1:
            # Re-run: the user already set this one. Offer the stored value; never re-classify.
            default_category = existing["category"]
            prompt_text = f"'{display_name}' is set to {default_category}. Keep this? (y/n)"
        else:
            default_category = _classify_creator(
                display_name, interests=interests, llm_classifier=llm_classifier, store_module=store_module
            )
            prompt_text = f"'{display_name}' classified as {default_category}. Keep this? (y/n)"

        answer = _prompt(input_fn, prompt_text, default="y")
        confirmed_category = default_category if _is_yes(answer) else _flip_category(default_category)

        store_module.upsert_source(
            platform=platform,
            external_id=external_id,
            display_name=display_name,
            category=confirmed_category,
            is_user_override=1,
        )
        categories[external_id] = confirmed_category
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
    """Collect the delivery block (``html_path`` + optional ``email_to``) — step 4.

    ``html_path`` always has a sane default; ``email_to`` is opt-in (an empty answer
    leaves it unset so delivery stays opt-in — Orbit never emails a digest without a
    configured recipient). Deterministic.

    Args:
        input_fn: The injectable input function (scripted in tests).

    Returns:
        A delivery dict with ``html_path`` and, when given, ``email_to``.
    """
    html_path = _prompt(input_fn, "Where should the digest HTML be written?", default=_DEFAULT_HTML_PATH)
    email_to = _prompt(input_fn, "Email address to send your digest to (optional, blank to skip)", default="")
    delivery: dict[str, Any] = {"html_path": html_path}
    if email_to:
        delivery["email_to"] = email_to
    return delivery


def _write_config(config: dict[str, Any], config_path: Path) -> None:
    """Write the assembled config dict to ``config_path`` as pretty JSON (UTF-8).

    Args:
        config: The assembled ``orbit.config.json`` shape (api-contracts.md).
        config_path: The output path (tests pass a tmp path).
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    log.log_info("setup_config_written", config_path=str(config_path))


def _install_daily_schedule(
    schedule: str,
    *,
    repo_path: Optional[Path],
    launch_agents_dir: Optional[Path],
    launchctl_runner: scheduler.LaunchctlRunner,
    crontab_runner: scheduler.CrontabRunner,
) -> None:
    """Install the wake-proof daily launchd agent (step 5), printing a manual fallback on failure.

    Delegates ALL scheduler logic to :func:`lib.scheduler.install_daily_scheduler` (Rule 5
    — wiring only). On success prints where the agent landed; on a soft failure (sandboxed /
    no ``launchctl``) prints the manual plist instructions so setup still completes.

    Args:
        schedule: The fixed 7am cron schedule (drives the plist calendar interval).
        repo_path: The agent's working directory; defaults to cwd when None.
        launch_agents_dir: The LaunchAgents directory (tmp path in tests).
        launchctl_runner: The injectable launchctl boundary (faked in tests).
        crontab_runner: The injectable crontab boundary for the cron migration (faked in tests).
    """
    installed = scheduler.install_daily_scheduler(
        schedule,
        repo_path=repo_path,
        launch_agents_dir=launch_agents_dir,
        launchctl_runner=launchctl_runner,
        crontab_runner=crontab_runner,
    )
    if installed:
        plist_path = scheduler.default_plist_path(launch_agents_dir)
        print("\nInstalled your daily Orbit run as a launchd agent (com.orbit.daily, 7am, catches up on wake):\n")
        print(f"  {plist_path}\n")
    else:
        print("\nCouldn't install the launchd agent automatically. Set it up yourself:\n")
        print(
            scheduler.manual_setup_instructions(
                repo_path=repo_path, launch_agents_dir=launch_agents_dir, schedule=schedule
            )
        )


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
    launch_agents_dir: Optional[Path] = None,
    launchctl_runner: scheduler.LaunchctlRunner = scheduler._default_launchctl_runner,
    crontab_runner: scheduler.CrontabRunner = scheduler._default_crontab_runner,
) -> int:
    """Run the interactive first-run wizard (brief §8.3), writing ``orbit.config.json``.

    Steps (deterministic except the per-creator classify judgment call, Rule 5):

      1. Ask for the cookie source (default the injected ``cookie_source``).
      2. Read YouTube subscriptions (injectable loader; fatal on auth failure) and X
         follows (injectable loader; best-effort — an :class:`XAuthError` is swallowed so
         a YouTube-only user still gets a config).
      3. Auto-classify each creator into signal/noise via the EXISTING classify path
         (:func:`lib.classify.classify_item`, NO separate classifier), let the user confirm
         categories — PERSISTED to ``sources`` as user overrides so they survive later refreshes
         (a re-run offers the stored value and skips re-classifying already-set channels) — and
         pick priority creators (``creator_weights``).
      4. Seed ``interests`` from subscription titles; gather the delivery target. The
         schedule is NOT asked — it is fixed at :data:`lib.config.DEFAULT_SCHEDULE` (7am
         daily) per the 2026-07-06 local-auto-cron decision, still written to the config.
      5. Write ``orbit.config.json`` (api-contracts.md shape) to ``config_path``, then
         INSTALL the wake-proof launchd agent via
         :func:`lib.scheduler.install_daily_scheduler` (which also retires the legacy orbit
         crontab line). Fail-soft: on any launchctl error it prints manual plist
         instructions instead.

    ALL external boundaries are injectable so tests run offline: loaders, the LLM
    classifier, the ``input`` function, the config output path, and the launchctl/crontab
    subprocess runners. The defaults wire the real loaders / ``input`` /
    ``./orbit.config.json`` / real launchctl.

    Args:
        cookie_source: Default browser name (or ``"env"``) for cookie reading.
        config_path: Where to write the config; defaults to ``./orbit.config.json``.
        repo_path: The repo directory the launchd agent runs in; defaults to cwd.
        youtube_loader: Subscription loader; defaults to ``load_youtube_subscriptions``.
        x_loader: X following loader; defaults to ``load_x_following``.
        llm_classifier: The injectable classify LLM boundary; tests inject a mock.
        input_fn: The injectable input function; defaults to builtin ``input``.
        store_module: The store module used by the classify path (injectable; defaults to
            :mod:`store`). Tests inject a mock so auto-classify never touches the real DB.
        launch_agents_dir: The LaunchAgents directory the plist is written to; defaults to
            ``~/Library/LaunchAgents``. Tests pass a tmp path.
        launchctl_runner: The injectable launchctl boundary; defaults to
            :func:`lib.scheduler._default_launchctl_runner`. Tests inject a scripted fake so
            setup never runs a real ``launchctl``.
        crontab_runner: The injectable crontab boundary for the cron migration; defaults to
            :func:`lib.scheduler._default_crontab_runner`. Tests inject a scripted fake so
            setup never touches the real user crontab.

    Returns:
        Process exit code: 0 on success.
    """
    log.log_info("setup_wizard_started", cookie_source=cookie_source)

    # Ensure the store exists and is at the latest schema BEFORE step 3 reads/writes ``sources``.
    # The --setup path does not otherwise call init_db (only the pipeline's Stage 0 does), so on
    # a fresh machine the base tables would be missing, and on the user's live v1 DB migration 2
    # (the category_is_user_override column) would be unapplied — either would crash the confirm
    # step. init_db is idempotent, so this is a no-op once the DB is current.
    store_module.init_db()

    resolved_config_path = config_path if config_path is not None else Path.cwd() / DEFAULT_CONFIG_FILENAME

    chosen_cookie_source = _prompt(
        input_fn, "Which browser holds your logins? (chrome/firefox/safari/edge/brave/env)", default=cookie_source
    )

    subscriptions = _load_youtube_subscriptions_safe(chosen_cookie_source, youtube_loader)
    follows = _load_x_following_best_effort(chosen_cookie_source, x_loader)

    # Build the unified source list as (platform, external_id, display_name) triples: channel_id
    # for YouTube, creator_handle for X. Platform is carried so each confirmed category persists
    # under the right UNIQUE(platform, external_id) key.
    sources_to_persist: list[tuple[str, str, str]] = [
        ("youtube", sub.channel_id, sub.display_name) for sub in subscriptions
    ]
    sources_to_persist += [("x", follow.creator_handle, follow.display_name) for follow in follows]

    interests = _seed_interests_from_subscriptions(subscriptions)

    # Step 3: confirm categories (auto-classify via the existing path, then PERSIST each as a
    # user override so a noise mark survives every later refresh) + pick priorities.
    confirmed_categories = _confirm_categories(
        sources_to_persist,
        interests=interests,
        llm_classifier=llm_classifier,
        input_fn=input_fn,
        store_module=store_module,
    )
    creators = [(external_id, display_name) for _platform, external_id, display_name in sources_to_persist]
    creator_weights = _pick_priority_creators(creators, input_fn=input_fn)

    # Step 4: delivery. The schedule is no longer asked (decision 2026-07-06 — local auto-run
    # at a fixed 7am): the wizard installs DEFAULT_SCHEDULE itself in step 5. The config still
    # carries ``schedule`` so its api-contracts shape is unchanged and user-editable.
    delivery = _gather_delivery(input_fn)
    schedule = DEFAULT_SCHEDULE

    config: dict[str, Any] = {
        "cookie_source": chosen_cookie_source,
        "creator_weights": creator_weights,
        "interests": interests,
        "depth": "default",
        "delivery": delivery,
        "schedule": schedule,
    }

    _write_config(config, resolved_config_path)

    # Step 5: install the wake-proof launchd agent (and migrate out the legacy cron line).
    # On failure, fall back to printing manual plist instructions so a sandboxed/CI run
    # still completes setup (install_daily_scheduler logs why).
    _install_daily_schedule(
        schedule,
        repo_path=repo_path,
        launch_agents_dir=launch_agents_dir,
        launchctl_runner=launchctl_runner,
        crontab_runner=crontab_runner,
    )

    log.log_info(
        "setup_wizard_completed",
        cookie_source=chosen_cookie_source,
        creator_count=len(creators),
        priority_creator_count=len(creator_weights),
        noise_creator_count=sum(1 for category in confirmed_categories.values() if category == "noise"),
        interest_count=len(interests),
        schedule=schedule,
    )
    return 0
