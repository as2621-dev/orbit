"""Summarize cluster winners into bullet points (the digest's "digest" step).

After classify filters noise and clustering crowns ONE winner per topic, this
module produces that winner's summary:

  * a long-form VIDEO winner -> exactly :data:`VIDEO_SUMMARY_BULLET_COUNT` (5)
    bullets, EACH deep-linked to the transcript moment it summarizes (the
    ``start_seconds`` the model picks is SNAPPED to the nearest real cue offset via
    :func:`lib.chapterize.snap_to_nearest_cue_offset`, so a bullet's timestamp always
    traces back to a real cue — never an invented number).
  * an X (tweet) winner -> 2-3 bullets, NO timestamps (a tweet has no transcript).

This mirrors :mod:`lib.chapterize` exactly in shape: an INJECTABLE LLM boundary
(:data:`Summarizer`) whose module-level default FAILS LOUD (there is no live model
in this build env; tests inject a mock, the host session wires the real caller),
strict-JSON parse-with-fallback that never crashes, and a sacred user-override
short-circuit (a user-edited summary persisted with ``is_user_override == 1`` is
returned verbatim and never re-generated, exactly like classify).

Rule 5: the model's ONLY job is the judgment call (what the key points are);
timestamp snapping, the bullet cap, persistence, and the override check are all
deterministic code here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Make ``lib`` importable whether imported as the package member ``lib.summarize``
# (via orbit.py's sys.path insert of the scripts dir) or run from the scripts dir
# directly. Mirrors chapterize.py / classify.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

import store  # noqa: E402  (import must follow the sys.path inserts above)
from lib import log  # noqa: E402
from lib.chapterize import snap_to_nearest_cue_offset  # noqa: E402
from lib.transcribe import Transcript, build_deep_link  # noqa: E402

# A video summary is EXACTLY this many timestamped bullets (locked decision 4).
VIDEO_SUMMARY_BULLET_COUNT: int = 5
# A tweet summary is 2-3 bullets (no timestamps).
TWEET_SUMMARY_MIN_BULLETS: int = 2
TWEET_SUMMARY_MAX_BULLETS: int = 3

# Prompt TEMPLATES live in references/ (tuned during real-day usage, not in code),
# resolved relative to this file: scripts/lib/summarize.py -> ../../references/.
_SUMMARIZE_VIDEO_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "summarize_video.md").resolve()
_SUMMARIZE_X_PROMPT_PATH: Path = (_LIB_DIR.parent.parent / "references" / "summarize_x.md").resolve()

# The injectable summarization boundary: takes the rendered prompt, returns the
# model's raw JSON string (a list of bullet objects). Same shape as the classify /
# chapterize boundaries, so ONE ``claude -p`` caller serves all three at runtime.
Summarizer = Callable[[str], str]


@dataclass
class SummaryBullet:
    """One summary bullet — a key point, optionally deep-linked to its moment.

    Attributes:
        text: The bullet text (a concise key point).
        start_seconds: The transcript offset the point is discussed at (videos only),
            SNAPPED to a real cue offset; None for tweets (no transcript).
        deep_link: The ``watch?v=ID&t=Ns`` link to that moment (videos only); None for
            tweets.
    """

    text: str
    start_seconds: Optional[float] = None
    deep_link: Optional[str] = None


@dataclass
class Summary:
    """An item's summary: its bullet list plus the sacred user-override flag.

    Attributes:
        item_external_id: The summarized item's id (matches RankableItem / store key).
        bullets: The :class:`SummaryBullet` list (5 for a video, 2-3 for a tweet,
            possibly empty when no transcript was available — never crashes).
        is_user_override: 1 when the user edited the summary (then it is sacred and
            never regenerated), else 0.
    """

    item_external_id: str
    bullets: list[SummaryBullet] = field(default_factory=list)
    is_user_override: int = 0


def _default_summarizer(prompt: str) -> str:
    """Default summarization boundary — no live model here, so fail loud.

    Args:
        prompt: The rendered summarize prompt (unused; we never fake a summary).

    Raises:
        NotImplementedError: Always. The real host Claude-session caller must be
            injected at runtime; tests inject a mock.
    """
    log.log_error(
        "summarize_summarizer_not_wired",
        fix_suggestion=(
            "wire the host Claude session caller at runtime; tests must inject a mock "
            "via summarize_video(..., summarizer=...) / summarize_tweet(..., summarizer=...)"
        ),
    )
    raise NotImplementedError(
        "No live summarizer is wired. Inject one via summarize_video(..., summarizer=...) "
        "or summarize_tweet(..., summarizer=...). In this build env there is no live model; "
        "tests must mock the boundary."
    )


def _read_item_external_id(item: Any) -> str:
    """Resolve an item's external id from a RankableItem or a raw Upload/Tweet.

    Reads ``item_external_id`` first (RankableItem), then ``video_id`` (Upload), then
    ``tweet_id`` (Tweet) — whichever is present and non-empty.

    Args:
        item: A RankableItem / Upload / Tweet (or any object exposing one of those).

    Returns:
        The item's external id as a string (empty when none is present).
    """
    for attribute in ("item_external_id", "video_id", "tweet_id"):
        value = getattr(item, attribute, None)
        if value:
            return str(value)
    return ""


def _read_title_text(item: Any) -> str:
    """Resolve the item's headline text: ``title`` (YouTube) or ``text`` (tweet)."""
    for attribute in ("title", "text"):
        value = getattr(item, attribute, None)
        if value:
            return str(value)
    return ""


def _parse_bullets(raw: str) -> Optional[list[dict]]:
    """Parse the strict-JSON bullet list; return None on any failure (never crash).

    Mirrors :func:`lib.chapterize._parse_segments`.

    Args:
        raw: The summarizer's raw response string.

    Returns:
        A list of bullet dicts, or None if the payload is not a JSON list.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [bullet for bullet in parsed if isinstance(bullet, dict)]


def _bullets_to_json(bullets: list[SummaryBullet]) -> str:
    """Serialize bullets to the JSON stored in the ``summaries`` table."""
    return json.dumps(
        [
            {"text": bullet.text, "start_seconds": bullet.start_seconds, "deep_link": bullet.deep_link}
            for bullet in bullets
        ]
    )


def _bullets_from_json(bullets_json: str) -> list[SummaryBullet]:
    """Deserialize stored bullet JSON back into :class:`SummaryBullet`s (never crashes)."""
    try:
        raw_bullets = json.loads(bullets_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw_bullets, list):
        return []
    bullets: list[SummaryBullet] = []
    for raw_bullet in raw_bullets:
        if not isinstance(raw_bullet, dict):
            continue
        start_seconds = raw_bullet.get("start_seconds")
        bullets.append(
            SummaryBullet(
                text=str(raw_bullet.get("text") or ""),
                start_seconds=float(start_seconds) if isinstance(start_seconds, (int, float)) else None,
                deep_link=(str(raw_bullet["deep_link"]) if raw_bullet.get("deep_link") else None),
            )
        )
    return bullets


def _override_summary(item_external_id: str, store_module: Any) -> Optional[Summary]:
    """Return the persisted summary IFF the user marked it an override (sacred), else None.

    Mirrors classify's override short-circuit: a user-edited summary is never
    regenerated by a re-run.

    Args:
        item_external_id: The item to look up.
        store_module: The store module (injectable).

    Returns:
        The persisted :class:`Summary` when ``is_user_override == 1``, else None.
    """
    existing = store_module.get_summary(item_external_id)
    if existing and int(existing.get("is_user_override") or 0) == 1:
        log.log_info("summarize_user_override_kept", item_external_id=item_external_id)
        return Summary(
            item_external_id=item_external_id,
            bullets=_bullets_from_json(str(existing.get("bullets_json") or "[]")),
            is_user_override=1,
        )
    return None


def _persist(summary: Summary, store_module: Any) -> None:
    """Persist a freshly generated summary (override flag 0)."""
    store_module.set_summary(
        item_external_id=summary.item_external_id,
        bullets_json=_bullets_to_json(summary.bullets),
        is_user_override=0,
    )


def _render_video_prompt(video_title: str, transcript: Transcript) -> str:
    """Render the video-summary prompt: title + the timed cue lines.

    Each cue becomes a ``<start_seconds>\\t<text>`` line (same format chapterize uses)
    so the model can only pick among REAL cue offsets — the snap step then enforces it.

    Args:
        video_title: The winner's title (context for the bullets).
        transcript: The cue-preserving transcript to summarize.

    Returns:
        The fully rendered prompt string.
    """
    template = _SUMMARIZE_VIDEO_PROMPT_PATH.read_text(encoding="utf-8")
    cue_lines = "\n".join(f"{cue.cue_start_seconds}\t{cue.text}" for cue in transcript.cues)
    return template.format(video_title=video_title, bullet_count=VIDEO_SUMMARY_BULLET_COUNT, cue_lines=cue_lines)


def _render_tweet_prompt(tweet_text: str) -> str:
    """Render the tweet-summary prompt (no timestamps)."""
    template = _SUMMARIZE_X_PROMPT_PATH.read_text(encoding="utf-8")
    return template.format(
        tweet_text=tweet_text,
        min_bullets=TWEET_SUMMARY_MIN_BULLETS,
        max_bullets=TWEET_SUMMARY_MAX_BULLETS,
    )


def summarize_video(
    item: Any,
    transcript: Optional[Transcript],
    *,
    summarizer: Summarizer = _default_summarizer,
    store_module: Any = store,
) -> Summary:
    """Summarize a long-form video winner into exactly 5 timestamped bullets.

    Decision flow (only the bullet content is the model's job — Rule 5):

      1. Sacred override: a persisted summary with ``is_user_override == 1`` is returned
         verbatim WITHOUT calling the model.
      2. No transcript / no cues: log a warning and return (and persist) an empty-bullet
         :class:`Summary` — never crashes (the winner still renders a card, just no
         bullet list).
      3. Otherwise: render the cues, call the injected ``summarizer``, parse the
         strict-JSON bullet list, SNAP each bullet's ``start_seconds`` to the nearest
         real cue offset and build its deep-link, keep at most
         :data:`VIDEO_SUMMARY_BULLET_COUNT` valid bullets, persist, and return.

    Args:
        item: The winner (a RankableItem / Upload — read for its id + title).
        transcript: The cue-preserving transcript, or None.
        summarizer: The injectable LLM boundary (default fails loud; tests mock it).
        store_module: The store module (injectable; defaults to :mod:`store`).

    Returns:
        The :class:`Summary` (possibly empty-bulleted).
    """
    item_external_id = _read_item_external_id(item)

    override = _override_summary(item_external_id, store_module)
    if override is not None:
        return override

    cues = transcript.cues if transcript else []
    if not cues:
        log.log_warning(
            "summarize_no_transcript",
            item_external_id=item_external_id,
            fix_suggestion=(
                "video winner had no transcript cues to summarize; it renders without a "
                "bullet summary. Check the transcript fetch for this id."
            ),
        )
        summary = Summary(item_external_id=item_external_id, bullets=[])
        _persist(summary, store_module)
        return summary

    cue_offsets = [cue.cue_start_seconds for cue in cues]
    prompt = _render_video_prompt(_read_title_text(item), transcript)
    raw = summarizer(prompt)
    parsed = _parse_bullets(raw)

    if not parsed:
        log.log_warning(
            "summarize_verdict_unparseable",
            item_external_id=item_external_id,
            fix_suggestion=(
                "summarizer did not return a strict JSON array of bullets; the winner "
                "renders without a bullet summary. Tune references/summarize_video.md if frequent."
            ),
        )
        summary = Summary(item_external_id=item_external_id, bullets=[])
        _persist(summary, store_module)
        return summary

    bullets: list[SummaryBullet] = []
    for raw_bullet in parsed:
        if len(bullets) >= VIDEO_SUMMARY_BULLET_COUNT:
            break
        text = str(raw_bullet.get("text") or "").strip()
        if not text:
            continue
        raw_start = raw_bullet.get("start_seconds")
        try:
            proposed = float(raw_start)
        except (TypeError, ValueError):
            # A bullet without a usable offset still carries its text, just no deep-link.
            bullets.append(SummaryBullet(text=text))
            continue
        snapped = snap_to_nearest_cue_offset(proposed, cue_offsets)
        bullets.append(
            SummaryBullet(text=text, start_seconds=snapped, deep_link=build_deep_link(item_external_id, snapped))
        )

    if len(bullets) < VIDEO_SUMMARY_BULLET_COUNT:
        log.log_warning(
            "summarize_fewer_bullets_than_target",
            item_external_id=item_external_id,
            bullet_count=len(bullets),
            target=VIDEO_SUMMARY_BULLET_COUNT,
            fix_suggestion="model returned fewer usable bullets than the target; tune references/summarize_video.md if frequent.",
        )

    summary = Summary(item_external_id=item_external_id, bullets=bullets)
    _persist(summary, store_module)
    log.log_info("summarize_video_completed", item_external_id=item_external_id, bullet_count=len(bullets))
    return summary


def summarize_tweet(
    item: Any,
    *,
    summarizer: Summarizer = _default_summarizer,
    store_module: Any = store,
) -> Summary:
    """Summarize an original-tweet winner into 2-3 timestamp-less bullets.

    Same shape as :func:`summarize_video` minus the transcript/timestamp machinery:
    sacred-override short-circuit, render the tweet text, call the injected
    ``summarizer``, parse, keep 2-3 bullets (no ``start_seconds`` / ``deep_link``),
    persist, return. Unparseable -> empty-bullet Summary (never crashes).

    Args:
        item: The tweet winner (a RankableItem / Tweet — read for id + text).
        summarizer: The injectable LLM boundary (default fails loud; tests mock it).
        store_module: The store module (injectable; defaults to :mod:`store`).

    Returns:
        The :class:`Summary` (2-3 bullets, or empty).
    """
    item_external_id = _read_item_external_id(item)

    override = _override_summary(item_external_id, store_module)
    if override is not None:
        return override

    prompt = _render_tweet_prompt(_read_title_text(item))
    raw = summarizer(prompt)
    parsed = _parse_bullets(raw)

    if not parsed:
        log.log_warning(
            "summarize_tweet_verdict_unparseable",
            item_external_id=item_external_id,
            fix_suggestion=(
                "summarizer did not return a strict JSON array of bullets; the tweet "
                "renders without a bullet summary. Tune references/summarize_x.md if frequent."
            ),
        )
        summary = Summary(item_external_id=item_external_id, bullets=[])
        _persist(summary, store_module)
        return summary

    bullets: list[SummaryBullet] = []
    for raw_bullet in parsed:
        if len(bullets) >= TWEET_SUMMARY_MAX_BULLETS:
            break
        text = str(raw_bullet.get("text") or "").strip()
        if text:
            bullets.append(SummaryBullet(text=text))

    summary = Summary(item_external_id=item_external_id, bullets=bullets)
    _persist(summary, store_module)
    log.log_info("summarize_tweet_completed", item_external_id=item_external_id, bullet_count=len(bullets))
    return summary
