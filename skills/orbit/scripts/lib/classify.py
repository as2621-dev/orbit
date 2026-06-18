"""Two-axis item classification with a channel prior (Phase 2 / Stage 2).

Each feed item is judged on two independent binary axes:

  - **Axis A — signal/noise** (``axis_a_signal``): 1 = substantive signal, 0 = noise.
  - **Axis B — on/off-topic** (``axis_b_on_topic``): 1 = matches the user's interests,
    0 = off-topic.

The ONLY model use here (Rule 5) is the judgment call: render a prompt and ask the
host LLM for a strict-JSON verdict. Everything else is deterministic code — the
user-override short-circuit, the channel-prior seeding when the verdict is
unparseable, and the persistence to ``store.classifications``.

Design decision 5 (master plan): an item that FAILS either axis is NEVER dropped.
It is routed to the "they also posted" strip (deranked, not deleted) — encoded by
:attr:`Classification.is_also_posted`.

There is NO live LLM in this build environment. The LLM call therefore goes through
an INJECTABLE boundary (:data:`LlmClassifier`); the module-level default
(:func:`_default_llm_classifier`) FAILS LOUD with ``NotImplementedError`` rather than
faking a verdict. The real host-session wiring is out of scope for this sub-phase;
tests inject a mock.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` and ``store`` importable whether this module is imported as the package
# member ``lib.classify`` (via orbit.py's sys.path insert of the scripts dir) or run
# from the scripts dir directly. Mirrors store.py / youtube_yt.py's sys.path pattern so
# the imports below resolve in both cases. ``lib/`` is this file's parent; the scripts
# dir (which holds ``store.py``) is its grandparent.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (import must follow the sys.path inserts above)
from lib import log  # noqa: E402

# The prompt TEMPLATE lives in references/classify.md, NOT inline (so the maintainer
# can tune wording during real-day usage without touching code). Resolved relative to
# this file: scripts/lib/classify.py -> ../../references/classify.md.
_CLASSIFY_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "classify.md").resolve()

# When the user has stated no interests, Axis B defaults to on-topic. This keeps the
# "never drop" posture honest — absent a topic profile we do not derank for off-topic.
_NO_INTERESTS_PLACEHOLDER: str = "(no interests stated yet — treat the item as on-topic)"

# Axis-B default used by the prior-seed fallback. Without a clear off-topic signal from
# the model, we keep the item on-topic (1) rather than deranking it — Axis A (via the
# channel prior) carries the uncertainty; Axis B stays generous so we never silently
# bury an item on a parse failure.
_AXIS_B_PRIOR_DEFAULT: int = 1


@dataclass
class Classification:
    """The two-axis verdict for one feed item, plus its derived routing.

    Attributes:
        item_external_id: The classified item's stable id (YouTube ``video_id``).
        axis_a_signal: 1 = signal, 0 = noise.
        axis_b_on_topic: 1 = on-topic, 0 = off-topic.
        is_user_override: 1 if the user corrected this item (sacred — never re-classified).
    """

    item_external_id: str
    axis_a_signal: int
    axis_b_on_topic: int
    is_user_override: int

    @property
    def is_also_posted(self) -> bool:
        """True when the item fails EITHER axis -> routed to "they also posted".

        Design decision 5: items failing an axis are deranked into the "they also
        posted" strip, NEVER dropped. So this is True iff ``axis_a_signal == 0`` OR
        ``axis_b_on_topic == 0`` (noise, or off-topic, or both).

        Returns:
            True if the item belongs in the "also posted" strip, False if it passes
            both axes (a top-line item).
        """
        return self.axis_a_signal == 0 or self.axis_b_on_topic == 0


# The injectable LLM boundary: takes the rendered prompt, returns the model's raw
# JSON string. Tests inject a mock; the real host-session caller is wired at runtime.
LlmClassifier = Callable[[str], str]


def _default_llm_classifier(prompt: str) -> str:
    """Default LLM boundary — there is no live model in this build env, so fail loud.

    Args:
        prompt: The rendered classify prompt (unused; we never fake a verdict).

    Raises:
        NotImplementedError: Always. The real host Claude-session caller must be
            injected at runtime; tests inject a mock.
    """
    log.log_error(
        "classify_llm_not_wired",
        fix_suggestion=(
            "wire the host Claude session caller at runtime; tests must inject a mock "
            "via classify_item(..., llm_classifier=...)"
        ),
    )
    raise NotImplementedError(
        "No live LLM classifier is wired. Inject one via "
        "classify_item(..., llm_classifier=...). In this build env there is no live "
        "model; tests must mock the boundary."
    )


def _read_item_field(item: Any, field_name: str) -> str:
    """Read a field off an Upload (attribute) OR a dict (key), as a string.

    Args:
        item: An :class:`lib.youtube_yt.Upload` or a dict with at least
            ``video_id`` / ``title`` / ``description``.
        field_name: The field to read.

    Returns:
        The field value coerced to a string, or "" if absent.
    """
    if isinstance(item, dict):
        value = item.get(field_name)
    else:
        value = getattr(item, field_name, None)
    return "" if value is None else str(value)


def _render_prompt(item: Any, channel_category: str, interests: list[str]) -> str:
    """Load references/classify.md and substitute the item / prior / interests.

    Args:
        item: An Upload or dict carrying ``title`` and ``description``.
        channel_category: The channel-level Axis-A prior ("signal" | "noise").
        interests: The user's topic keywords (drives Axis B).

    Returns:
        The fully rendered prompt string ready to hand to the LLM boundary.
    """
    template = _CLASSIFY_PROMPT_PATH.read_text(encoding="utf-8")
    interests_text = ", ".join(interests) if interests else _NO_INTERESTS_PLACEHOLDER
    return template.format(
        item_title=_read_item_field(item, "title"),
        item_description=_read_item_field(item, "description"),
        channel_category=channel_category,
        interests=interests_text,
    )


def _coerce_axis(raw_value: Any) -> Optional[int]:
    """Coerce a raw verdict value to a 0/1 int, or None if it is not a clean binary.

    Args:
        raw_value: The value pulled from the parsed JSON verdict.

    Returns:
        0 or 1, or None when the value is missing / not a clean 0-or-1 signal.
    """
    if isinstance(raw_value, bool):
        return 1 if raw_value else 0
    if isinstance(raw_value, int):
        return 1 if raw_value == 1 else (0 if raw_value == 0 else None)
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if stripped in ("0", "1"):
            return int(stripped)
    return None


def _parse_verdict(raw: str, channel_category: str) -> tuple[int, int]:
    """Parse the strict-JSON verdict; fall back to the channel prior on any failure.

    On malformed JSON, a non-object payload, or missing/uncoercible axis keys, this
    seeds the verdict from the CHANNEL PRIOR (``channel_category``: "signal" -> Axis A
    1, "noise" -> 0) for Axis A and :data:`_AXIS_B_PRIOR_DEFAULT` for Axis B, and logs a
    ``classify_verdict_unparseable`` warning — it NEVER crashes (a flaky model line must
    not lose the whole classify run).

    Args:
        raw: The model's raw response string.
        channel_category: The channel-level Axis-A prior used as the fallback seed.

    Returns:
        A ``(axis_a_signal, axis_b_on_topic)`` tuple of 0/1 ints.
    """
    prior_axis_a = 1 if channel_category == "signal" else 0
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        log.log_warning(
            "classify_verdict_unparseable",
            reason="not_a_json_object",
            channel_category=channel_category,
            fix_suggestion=(
                "model did not return a strict JSON object; seeded Axis A from the "
                "channel prior. Tune references/classify.md's output contract if frequent."
            ),
        )
        return prior_axis_a, _AXIS_B_PRIOR_DEFAULT

    axis_a = _coerce_axis(parsed.get("axis_a_signal"))
    axis_b = _coerce_axis(parsed.get("axis_b_on_topic"))
    if axis_a is None or axis_b is None:
        log.log_warning(
            "classify_verdict_unparseable",
            reason="missing_or_invalid_axis",
            channel_category=channel_category,
            fix_suggestion=(
                "model JSON lacked a clean 0/1 for an axis; seeded the missing axis from "
                "the prior. Tune references/classify.md's output contract if frequent."
            ),
        )
        # Reason: seed only the axis that failed — keep any clean value the model gave.
        axis_a = prior_axis_a if axis_a is None else axis_a
        axis_b = _AXIS_B_PRIOR_DEFAULT if axis_b is None else axis_b

    return axis_a, axis_b


def classify_item(
    item: Any,
    channel_category: str,
    interests: list[str],
    *,
    llm_classifier: LlmClassifier = _default_llm_classifier,
    store_module: Any = store,
) -> Classification:
    """Classify one item on both axes, respecting user overrides and the channel prior.

    Flow (Rule 5 — only the verdict is a model judgment; the rest is deterministic):

      1. DETERMINISTIC override short-circuit FIRST. If a stored classification exists
         with ``is_user_override == 1``, return it WITHOUT calling the LLM — user
         corrections are sacred and never re-classified.
      2. Else render the prompt (from references/classify.md), call the injected LLM
         boundary, and parse the strict-JSON verdict. On an unparseable verdict the
         CHANNEL PRIOR seeds Axis A and a sensible default seeds Axis B.
      3. Persist via ``store.set_classification(..., is_user_override=0)`` and return.

    Items that fail either axis are NEVER dropped — :attr:`Classification.is_also_posted`
    routes them to the "they also posted" strip (design decision 5).

    Args:
        item: An :class:`lib.youtube_yt.Upload` or a dict with at least ``video_id`` /
            ``title`` / ``description``. ``item_external_id`` is ``video_id``.
        channel_category: The channel-level Axis-A prior ("signal" | "noise") from
            the ``sources`` row.
        interests: The user's topic keywords (drives Axis B).
        llm_classifier: The injectable LLM boundary. Defaults to the loud-failing stub;
            tests inject a mock, runtime injects the host session caller.
        store_module: The store module (injectable for tests). Defaults to :mod:`store`.

    Returns:
        The persisted :class:`Classification`.

    Example:
        >>> upload = {"video_id": "abc", "title": "A talk", "description": "..."}
        >>> result = classify_item(  # doctest: +SKIP
        ...     upload, channel_category="signal", interests=["ai"],
        ...     llm_classifier=lambda prompt: '{"axis_a_signal": 1, "axis_b_on_topic": 1}',
        ... )
        >>> result.is_also_posted  # doctest: +SKIP
        False
    """
    item_external_id = _read_item_field(item, "video_id")

    # 1. Deterministic override short-circuit — user corrections are sacred.
    existing = store_module.get_classification(item_external_id)
    if existing and existing["is_user_override"] == 1:
        log.log_info(
            "classify_override_respected",
            item_external_id=item_external_id,
            axis_a_signal=existing["axis_a_signal"],
            axis_b_on_topic=existing["axis_b_on_topic"],
        )
        return Classification(
            item_external_id=item_external_id,
            axis_a_signal=int(existing["axis_a_signal"]),
            axis_b_on_topic=int(existing["axis_b_on_topic"]),
            is_user_override=1,
        )

    # 2. The model judgment call (the only model use here, Rule 5).
    prompt = _render_prompt(item, channel_category, interests)
    raw_verdict = llm_classifier(prompt)
    axis_a_signal, axis_b_on_topic = _parse_verdict(raw_verdict, channel_category)

    # 3. Persist (deterministic) and return.
    store_module.set_classification(
        item_external_id=item_external_id,
        axis_a_signal=axis_a_signal,
        axis_b_on_topic=axis_b_on_topic,
        is_user_override=0,
    )
    log.log_info(
        "classify_completed",
        item_external_id=item_external_id,
        axis_a_signal=axis_a_signal,
        axis_b_on_topic=axis_b_on_topic,
        channel_category=channel_category,
    )
    return Classification(
        item_external_id=item_external_id,
        axis_a_signal=axis_a_signal,
        axis_b_on_topic=axis_b_on_topic,
        is_user_override=0,
    )
