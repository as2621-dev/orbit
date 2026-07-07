"""Weighted derank scoring (Phase 3 / Stage 6a) for Orbit.

This module turns the Phase-2 outputs — a YouTube :class:`lib.youtube_yt.Upload`,
its two-axis :class:`lib.classify.Classification`, and its
:class:`lib.chapterize.Chapter` list — into ONE rankable unit
(:class:`RankableItem`) and scores it with Orbit's deterministic weighted derank
formula (api-contracts.md "Derank score contract (Stage 6)").

Rule 5 — there is NO LLM here. Scoring is pure math. The score combines:

  1. **creator priority_weight** — the user's thumb on the scale, read from
     ``config.creator_weights`` keyed by the creator's channel external id
     (default ``1.0`` when the creator is absent). Applied multiplicatively so a
     priority-2.0 creator's item always outranks an otherwise-identical
     priority-1.0 creator's item (DoD #1).
  2. **source diversity / cluster size** — a neutral ``1.0`` hook in M1 (clustering
     is M3). Wired as :data:`CLUSTER_SIZE_NEUTRAL` so M3 fills it without a rewrite.
  3. **uniqueness boost** — a baseline floor scaled by priority_weight so a lone
     sharp take from a HIGH-priority creator does NOT sink to the bottom even with
     low engagement (DoD #3). The boost ties to priority_weight: the more the user
     trusts a creator, the higher the floor under their unique item.
  4. **engagement relative to the creator's OWN baseline** — NOT raw views. We
     blend view/like/comment counts via a ``log1p_safe`` transform (lifted in shape
     from last30days/signals.py, adapted to the Orbit ``Upload`` schema), then
     subtract the creator's OWN baseline (the per-creator MEDIAN of that blend
     across the batch — see :func:`compute_creator_engagement_baselines`). So an
     item far above its creator's normal level outranks an item with higher RAW
     engagement that is merely normal-for-its-creator (DoD #2).
  5. **recency decay** — newer ``upload_date`` (``YYYYMMDD``) scores higher via an
     exponential decay over days-since-upload. Empty/garbage dates degrade
     gracefully to a neutral mid decay, never crashing.
  6. **trending/scoop multiplier** — a neutral ``1.0`` hook in M1
     (:data:`TRENDING_MULTIPLIER_NEUTRAL`), wired so M3 extends rather than rewrites.

:func:`derank_items` scores every item and returns them sorted DESCENDING by score.
**Nothing is dropped** — rank controls density (Sub-phase 2's tiering), never
inclusion (api-contracts.md derank contract).
"""

from __future__ import annotations

import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.rerank`` (via orbit.py's sys.path insert of the scripts dir) or run from
# the scripts dir directly. Mirrors youtube_yt.py / classify.py / config.py.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)
from lib.images import derive_avatar_url, derive_youtube_thumb_url  # noqa: E402

# --- Named weight constants (the formula's tunable surface) -----------------
# Kept at module top per the brief so the maintainer tunes the derank shape here,
# not buried in code. These are first-cut values; real-day usage tunes them.

# Default creator priority when a creator is absent from config.creator_weights.
# 1.0 == neutral (no thumb on the scale).
DEFAULT_PRIORITY_WEIGHT: float = 1.0

# Blend weights for raw engagement (applied to log1p_safe of each count). View
# count dominates, likes next, comments least — mirrors the last30days YouTube
# engagement shape, adapted to Orbit's Upload (no top-comment slot in M1).
ENGAGEMENT_VIEW_WEIGHT: float = 0.50
ENGAGEMENT_LIKE_WEIGHT: float = 0.35
ENGAGEMENT_COMMENT_WEIGHT: float = 0.15

# Weight for the X-only quote count in the engagement blend. Quotes signal active
# discourse around a take, so they earn a positive term — but a quote is a weaker
# endorsement than a like, so the weight sits BELOW ENGAGEMENT_LIKE_WEIGHT. YouTube
# items carry ``quote_count is None`` -> ``log1p_safe`` returns 0.0, so this term is
# score-neutral for them (Phase 8 Sub-phase 3).
ENGAGEMENT_QUOTE_WEIGHT: float = 0.20

# How strongly relative-engagement (item's log-engagement minus its creator's
# baseline) moves the score. The relative term is signed: positive when an item
# beats its creator's norm, negative when it lags — so over-performers float.
RELATIVE_ENGAGEMENT_WEIGHT: float = 1.0

# How strongly the ABSOLUTE batch-engagement percentile (0.0–1.0 within the X batch)
# moves the score, ADDED alongside the creator-relative term. The relative term alone
# would let a median post from a loud account tie a genuine banger from a quiet one;
# this absolute term breaks that tie in favor of the batch's true top engagers. Only
# X items get a percentile (YouTube items are absent from the map -> 0.0 term, so this
# is score-neutral for YouTube). First-cut weight; tuned against real runs.
ABSOLUTE_ENGAGEMENT_WEIGHT: float = 0.5

# Final-score multiplier applied when an X item is itself a QUOTE of someone else's
# tweet. A quote of another creator's take is derivative, so it ranks below the
# creator's own original take of otherwise-identical strength. 1.0 would be a no-op;
# 0.5 halves a quote's score. Non-quote items (all YouTube, plain tweets) keep 1.0.
QUOTE_TWEET_MULTIPLIER: float = 0.5

# Hard cap on how many X tweets survive into the digest after scoring (Phase 8 Sub-phase
# 3 — top-N virality selection supersedes density-not-inclusion for the X half only).
# Applied at the rank seam AFTER scoring; YouTube items are never capped.
X_DIGEST_TWEET_CAP: int = 8

# Recency: exponential decay over days since upload. RECENCY_HALF_LIFE_DAYS is the
# age at which the recency term halves; RECENCY_WEIGHT scales its contribution.
# A missing/garbage upload_date falls back to RECENCY_NEUTRAL_DECAY (neutral mid),
# never crashing.
RECENCY_WEIGHT: float = 1.0
RECENCY_HALF_LIFE_DAYS: float = 7.0
RECENCY_NEUTRAL_DECAY: float = 0.5

# Uniqueness boost: a baseline floor placed UNDER every item, scaled by the
# creator's priority_weight. This is what keeps a lone sharp take from a
# high-priority creator off the bottom even with low engagement (DoD #3): the
# floor rises with how much the user trusts the creator.
UNIQUENESS_BASELINE_BOOST: float = 1.0

# M3 hooks — neutral no-ops in M1 so M3 EXTENDS rather than rewrites this module.
# cluster size / source diversity (clustering is M3) and trending/scoop multiplier.
CLUSTER_SIZE_NEUTRAL: float = 1.0
TRENDING_MULTIPLIER_NEUTRAL: float = 1.0


@dataclass
class RankableItem:
    """One unified, rankable feed unit — an Upload + its Classification + Chapters.

    This is the item model the whole rank/render pipeline (Sub-phases 2-4 and the
    Phase-4 X source) carries. It is intentionally source-agnostic where reasonable:
    YouTube is the only M1 producer, but a non-YouTube item is expressible by
    constructing this directly (X items in M2/Phase-4 build the same shape).
    :meth:`from_parts` is the canonical adapter every producer uses so items are
    built identically.

    Attributes:
        item_external_id: The source item's stable id (YouTube ``video_id`` /
            X ``tweet_id``). Matches ``Classification.item_external_id``.
        title: The item title (or headline text for a non-YouTube item).
        channel_name: Human-readable creator name (for display).
        creator_external_id: The creator's stable external id — ``channel_id`` (YT)
            or ``creator_handle`` (X). This is the KEY looked up in
            ``config.creator_weights`` for the priority_weight. Empty string when
            unknown (then priority defaults to :data:`DEFAULT_PRIORITY_WEIGHT`).
        view_count: Raw view count, or None if absent.
        like_count: Raw like count, or None if absent.
        comment_count: Raw comment count, or None if absent.
        upload_date: ``YYYYMMDD`` upload date string (yt-dlp shape), or "" if absent.
        classification: The two-axis :class:`lib.classify.Classification`, or None
            if the item was not classified. Carries ``is_also_posted`` (used by
            Sub-phase 2 to force "they also posted" routing) — scoring itself does
            NOT drop on it (rank controls density, never inclusion).
        chapters: The item's :class:`lib.chapterize.Chapter` list (possibly empty);
            carried through to the renderer for the chapter deep-links.
        card_url: An OPTIONAL source-specific card link (Phase 4 / M2). Empty by
            default → the renderer falls back to the YouTube ``watch?v=ID&t=0s`` form
            (so a YouTube item is byte-for-byte unchanged). An X item sets this to its
            ``https://x.com/{handle}/status/{tweet_id}`` permalink so it renders a real
            x.com card link in the same unified digest.
        image_url: OPTIONAL source image URL for the tile thumbnail / avatar (Phase 7).
            For a YouTube item this is the ``i.ytimg.com`` thumbnail derived from the
            ``video_id`` (:func:`lib.images.derive_youtube_thumb_url`); for an X item it
            is the ``unavatar.io`` avatar derived from the handle. Empty by default — the
            renderer falls back to the hatched ``.ph`` placeholder. The image is fetched
            + base64-inlined at render time, never at digest open (self-contained brief).
        summary: OPTIONAL ≤140-char LLM editorial blurb for the tile (Phase 7, populated
            by Sub-phase 2's ``lib.summarize``, NOT here). Empty by default — an absent
            blurb renders no element (graceful degradation, never fabricated).
        quote_count: OPTIONAL X quote count (Phase 8 Sub-phase 3). None for YouTube items
            and tweets with no quote data — ``log1p_safe`` maps None to 0.0 so the blend's
            quote term is score-neutral when absent.
        is_quote: True when this X item is itself a QUOTE of another tweet (Phase 8
            Sub-phase 3). Drives :data:`QUOTE_TWEET_MULTIPLIER` so a quote of someone
            else's take ranks below an original take. Always False for YouTube items.
    """

    item_external_id: str
    title: str
    channel_name: str
    creator_external_id: str
    view_count: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    upload_date: str
    classification: Any = None
    chapters: list = field(default_factory=list)
    card_url: str = ""
    image_url: str = ""
    summary: str = ""
    quote_count: Optional[int] = None
    is_quote: bool = False

    @classmethod
    def from_parts(
        cls,
        upload: Any,
        classification: Any = None,
        chapters: Optional[list] = None,
        *,
        creator_external_id: str = "",
    ) -> "RankableItem":
        """Build a :class:`RankableItem` from an Upload, its Classification, Chapters.

        The canonical item-ingestion adapter every producer (the YouTube pipeline now,
        the Phase-4 X source later) uses, so items are constructed identically. Reads
        the REAL :class:`lib.youtube_yt.Upload` field names (``video_id``, ``title``,
        ``channel_name``, ``view_count``/``like_count``/``comment_count``,
        ``upload_date``); the chapters/classification are passed through verbatim.

        Args:
            upload: An :class:`lib.youtube_yt.Upload` (or any object exposing the same
                attributes). ``video_id`` becomes ``item_external_id``.
            classification: The item's :class:`lib.classify.Classification`, or None.
            chapters: The item's :class:`lib.chapterize.Chapter` list, or None (-> []).
            creator_external_id: The creator's channel external id for the
                priority-weight lookup. Defaults to "" (then priority is neutral) —
                the YouTube pipeline passes the source row's ``external_id`` here.

        Returns:
            The unified :class:`RankableItem`.

        Example:
            >>> from lib.youtube_yt import Upload
            >>> upload = Upload(
            ...     video_id="abc", title="A talk", description="", upload_date="20260101",
            ...     view_count=1000, like_count=50, comment_count=5, duration=1800,
            ...     channel_name="Some Channel",
            ... )
            >>> item = RankableItem.from_parts(upload, None, [], creator_external_id="UC123")
            >>> item.item_external_id
            'abc'
        """
        video_id = str(getattr(upload, "video_id", ""))
        return cls(
            item_external_id=video_id,
            title=str(getattr(upload, "title", "") or ""),
            channel_name=str(getattr(upload, "channel_name", "") or ""),
            creator_external_id=creator_external_id,
            view_count=getattr(upload, "view_count", None),
            like_count=getattr(upload, "like_count", None),
            comment_count=getattr(upload, "comment_count", None),
            upload_date=str(getattr(upload, "upload_date", "") or ""),
            classification=classification,
            chapters=list(chapters) if chapters else [],
            # Reason: the tile thumbnail is the YouTube mqdefault frame derived from the
            # video id; fetched + base64-inlined at render time (Phase 7 self-contained).
            image_url=derive_youtube_thumb_url(video_id) if video_id else "",
        )

    @classmethod
    def from_tweet(
        cls,
        tweet: Any,
        classification: Any = None,
        *,
        creator_external_id: str = "",
    ) -> "RankableItem":
        """Build a :class:`RankableItem` from an X :class:`lib.bird_x.Tweet` (Phase 4 / M2).

        The X-source analog of :func:`from_parts`, so X tweets enter the SAME unified
        rank/render stream as YouTube uploads (no separate item model). A tweet is
        text-only (no chapters); its body becomes the ``title``, its author handle the
        ``channel_name`` + ``creator_external_id`` (the ``creator_weights`` lookup key,
        matching how X handles persist as ``sources.external_id``), and its engagement
        counts map onto the shared fields (``retweet_count`` → ``view_count``,
        ``like_count`` → ``like_count``, ``reply_count`` → ``comment_count``) so the
        same blend scores it. ``card_url`` is set to the tweet's x.com permalink so the
        renderer links the card to x.com rather than the YouTube fallback.

        ``upload_date`` is left empty unless ``created_at`` is an ISO-8601 string we can
        reduce to ``YYYYMMDD`` — the recency term degrades gracefully to neutral on an
        unparseable/absent date (never crashes), so a raw Twitter date string is safe.

        Args:
            tweet: An :class:`lib.bird_x.Tweet` (or any object exposing ``tweet_id`` /
                ``text`` / ``handle`` / ``created_at`` and the engagement counts).
            classification: The tweet's :class:`lib.classify.Classification`, or None.
            creator_external_id: The creator key for the priority-weight lookup —
                defaults to the tweet's ``handle`` when not given explicitly.

        Returns:
            The unified :class:`RankableItem` carrying an x.com ``card_url``.

        Example:
            >>> from types import SimpleNamespace
            >>> tweet = SimpleNamespace(
            ...     tweet_id="123", text="a sharp take", handle="alice",
            ...     created_at="2026-06-18T00:00:00Z", like_count=10,
            ...     retweet_count=5, reply_count=2, quote_count=1,
            ... )
            >>> item = RankableItem.from_tweet(tweet)
            >>> item.card_url
            'https://x.com/alice/status/123'
        """
        handle = str(getattr(tweet, "handle", "") or "")
        tweet_id = str(getattr(tweet, "tweet_id", "") or "")
        return cls(
            item_external_id=tweet_id,
            title=str(getattr(tweet, "text", "") or ""),
            channel_name=handle,
            creator_external_id=creator_external_id or handle,
            view_count=getattr(tweet, "retweet_count", None),
            like_count=getattr(tweet, "like_count", None),
            comment_count=getattr(tweet, "reply_count", None),
            upload_date=_tweet_upload_date(getattr(tweet, "created_at", "")),
            classification=classification,
            chapters=[],
            card_url=f"https://x.com/{handle}/status/{tweet_id}",
            # Reason: quote_count feeds the engagement blend's discourse term and is_quote
            # drives QUOTE_TWEET_MULTIPLIER (a quote of another take ranks below an
            # original). Tolerant getattr defaults keep pre-Phase-8 Tweet stand-ins valid.
            quote_count=getattr(tweet, "quote_count", None),
            is_quote=bool(getattr(tweet, "is_quote", False)),
            # Reason: tweet tiles carry the author's unavatar.io profile pic (a deliberate
            # design extension — the base design has none); inlined at render time.
            image_url=derive_avatar_url(handle) if handle else "",
        )


@dataclass
class ScoredItem:
    """A :class:`RankableItem` paired with its computed derank score.

    Attributes:
        item: The rankable item that was scored.
        score: Its float derank score (higher == ranks earlier). Sub-phase 2 maps
            the sorted score distribution to density tiers; nothing is dropped here.
    """

    item: RankableItem
    score: float


def log1p_safe(value: float | int | None) -> float:
    """Return ``log1p`` of a non-negative value, or ``0.0`` for None/garbage/<=0.

    Lifted in shape from last30days/signals.py (adapted: same contract). Used to
    compress raw engagement counts so a 1M-view item does not linearly dwarf a
    10k-view one. Never raises — a bad value degrades to ``0.0``.

    Args:
        value: A raw count (views/likes/comments), possibly None or non-numeric.

    Returns:
        ``math.log1p(value)`` when ``value`` is a positive number, else ``0.0``.

    Example:
        >>> log1p_safe(None)
        0.0
        >>> round(log1p_safe(0), 4)
        0.0
        >>> round(log1p_safe(99), 4)
        4.6052
    """
    if value is None:
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric <= 0:
        return 0.0
    return math.log1p(numeric)


def engagement_blend(item: RankableItem) -> float:
    """Blend an item's raw view/like/comment counts into one log-space signal.

    Each count is ``log1p_safe``-compressed then weighted (views dominant, comments
    least — :data:`ENGAGEMENT_VIEW_WEIGHT` etc.). This is the RAW engagement signal;
    the score uses it RELATIVE to the creator's own baseline, not on its own (so a
    big channel's normal video does not auto-outrank a small channel's breakout).

    X items add a fourth term for ``quote_count`` (:data:`ENGAGEMENT_QUOTE_WEIGHT`);
    YouTube items carry ``quote_count is None`` so that term is 0.0 — the blend is
    byte-for-byte unchanged for YouTube (Phase 8 Sub-phase 3).

    Args:
        item: The item whose engagement to blend.

    Returns:
        The weighted log-space engagement blend (``>= 0.0``). ``0.0`` when the item
        has no engagement at all (all counts None/0).

    Example:
        >>> item = RankableItem(
        ...     item_external_id="x", title="t", channel_name="c",
        ...     creator_external_id="UC1", view_count=1000, like_count=50,
        ...     comment_count=5, upload_date="20260101",
        ... )
        >>> engagement_blend(item) > 0
        True
    """
    return (
        ENGAGEMENT_VIEW_WEIGHT * log1p_safe(item.view_count)
        + ENGAGEMENT_LIKE_WEIGHT * log1p_safe(item.like_count)
        + ENGAGEMENT_COMMENT_WEIGHT * log1p_safe(item.comment_count)
        + ENGAGEMENT_QUOTE_WEIGHT * log1p_safe(item.quote_count)
    )


def compute_creator_engagement_baselines(items: list[RankableItem]) -> dict[str, float]:
    """Compute each creator's OWN engagement baseline: the median of their batch blend.

    The baseline source (documented per the brief): since M1 has no historical
    per-creator engagement store wired into rank, the baseline is derived FROM THE
    BATCH — for each ``creator_external_id`` we take the MEDIAN of
    :func:`engagement_blend` across that creator's items in this run. The median (not
    the mean) resists a single viral outlier skewing a creator's "normal". A creator
    with one item in the batch has a baseline equal to that item's blend (so its
    relative engagement is ~0 — neutral, neither rewarded nor penalized for being
    alone). M3 can replace this with a true historical median without touching the
    score function (it consumes whatever baseline map it is given).

    Args:
        items: The full batch of rankable items to derive baselines from.

    Returns:
        A map of ``creator_external_id`` -> median engagement blend for that creator.

    Example:
        >>> a = RankableItem("a", "t", "c", "UC1", 10, None, None, "20260101")
        >>> b = RankableItem("b", "t", "c", "UC1", 1000, None, None, "20260101")
        >>> baselines = compute_creator_engagement_baselines([a, b])
        >>> "UC1" in baselines
        True
    """
    blends_by_creator: dict[str, list[float]] = {}
    for item in items:
        blends_by_creator.setdefault(item.creator_external_id, []).append(engagement_blend(item))
    return {creator: statistics.median(blends) for creator, blends in blends_by_creator.items()}


def is_x_item(item: RankableItem) -> bool:
    """Return True when a :class:`RankableItem` originated from X (a tweet).

    The discriminator is the ``card_url``: :meth:`RankableItem.from_tweet` sets it to the
    ``https://x.com/{handle}/status/{id}`` permalink, while YouTube's
    :meth:`RankableItem.from_parts` leaves it empty (the renderer falls back to the
    ``watch?v=`` form). This is the one field that reliably distinguishes the two sources
    without threading a platform tag through every producer. Used by the absolute-engagement
    percentile (X batch only) and the X digest cap (Phase 8 Sub-phase 3).

    Args:
        item: The rankable item to classify by source.

    Returns:
        True if the item is an X tweet, False otherwise (YouTube and any future source
        that does not set an x.com card url).
    """
    return item.card_url.startswith("https://x.com/")


def compute_batch_engagement_percentile(items: list[RankableItem]) -> dict[str, float]:
    """Rank each item's :func:`engagement_blend` into a 0.0–1.0 percentile within the batch.

    The ABSOLUTE-engagement signal (Phase 8 Sub-phase 3): where
    :func:`compute_creator_engagement_baselines` measures an item against its OWN creator's
    norm, this measures it against the WHOLE batch, so a genuine banger from a quiet account
    can outrank a merely-median post from a loud one. Intended for the X batch (the caller
    passes only X items) so the term stays score-neutral for YouTube.

    The percentile is the fraction of batch items whose blend is ``<=`` this item's blend
    (a cumulative rank): the top item scores ``1.0``, a lone item scores ``1.0`` (nothing
    ranks above it), tied blends share a percentile, and the mapping is monotonic in blend.

    Args:
        items: The batch to rank (typically the run's X items). Keyed by ``item_external_id``.

    Returns:
        A map of ``item_external_id`` -> percentile in ``(0.0, 1.0]`` (empty for an empty
        batch). An item absent from the map (e.g. a YouTube item never passed in) contributes
        no absolute term at scoring time.

    Example:
        >>> a = RankableItem("a", "t", "c", "h", 10, None, None, "20260101", card_url="https://x.com/h/status/a")
        >>> b = RankableItem("b", "t", "c", "h", 10000, None, None, "20260101", card_url="https://x.com/h/status/b")
        >>> pct = compute_batch_engagement_percentile([a, b])
        >>> pct["b"]  # the higher-engagement tweet tops the batch
        1.0
    """
    if not items:
        return {}
    blends = [(item.item_external_id, engagement_blend(item)) for item in items]
    total = len(blends)
    percentiles: dict[str, float] = {}
    for external_id, blend in blends:
        count_at_or_below = sum(1 for _, other_blend in blends if other_blend <= blend)
        percentiles[external_id] = count_at_or_below / total
    return percentiles


def priority_weight_for(item: RankableItem, config: Any) -> float:
    """Look up the creator's priority_weight from ``config.creator_weights``.

    Keyed by ``item.creator_external_id`` (a YouTube ``channel_id`` or X
    ``creator_handle`` — matching the api-contracts ``creator_weights`` key shape).
    Defaults to :data:`DEFAULT_PRIORITY_WEIGHT` (1.0, neutral) when the creator is
    absent or the weight is non-numeric, so an unweighted creator never crashes the
    run and simply gets no thumb on the scale.

    Args:
        item: The item whose creator weight to resolve.
        config: An :class:`lib.config.OrbitConfig` (read for ``creator_weights``).

    Returns:
        The creator's priority weight as a float (``DEFAULT_PRIORITY_WEIGHT`` if absent).
    """
    creator_weights = getattr(config, "creator_weights", {}) or {}
    raw_weight = creator_weights.get(item.creator_external_id, DEFAULT_PRIORITY_WEIGHT)
    try:
        return float(raw_weight)
    except (TypeError, ValueError):
        log.log_warning(
            "rerank_priority_weight_non_numeric",
            creator_external_id=item.creator_external_id,
            raw_weight=raw_weight,
            fix_suggestion=(
                "creator_weights value is not a number; using the neutral default. "
                "Set the weight to a float in orbit.config.json."
            ),
        )
        return DEFAULT_PRIORITY_WEIGHT


def _tweet_upload_date(created_at: Any) -> str:
    """Reduce a tweet ``created_at`` to the ``YYYYMMDD`` shape recency scoring expects.

    Accepts an ISO-8601 string (e.g. ``"2026-06-18T00:00:00Z"``) and returns its
    ``YYYYMMDD`` date. Anything we cannot cleanly parse (a Twitter ``"Wed Jan 15 ..."``
    string, None, garbage) returns "" — :func:`recency_decay` then degrades to a neutral
    mid decay rather than crashing, so an X item is never buried for an odd date format.

    Args:
        created_at: The tweet's raw ``created_at`` value (any type).

    Returns:
        A ``YYYYMMDD`` string, or "" when the value is not a parseable ISO date.
    """
    text = str(created_at or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.strftime("%Y%m%d")


def _parse_upload_date(upload_date: str) -> Optional[date]:
    """Parse a ``YYYYMMDD`` upload-date string to a ``date``, or None on garbage.

    Never raises (Rule 12 graceful degradation): an empty string, a non-8-digit
    string, or an impossible date (``20269999``) returns None so the caller can fall
    back to a neutral recency rather than crashing the whole rank.

    Args:
        upload_date: The yt-dlp ``upload_date`` (``YYYYMMDD``) or "".

    Returns:
        The parsed ``date``, or None if the string is empty/malformed.
    """
    candidate = (upload_date or "").strip()
    if len(candidate) != 8 or not candidate.isdigit():
        return None
    try:
        return datetime.strptime(candidate, "%Y%m%d").date()
    except ValueError:
        return None


def recency_decay(upload_date: str, *, reference_date: Optional[date] = None) -> float:
    """Exponential recency factor in ``(0, 1]`` — newer uploads score higher.

    Uses a half-life decay: an item uploaded today scores ``1.0``; one
    :data:`RECENCY_HALF_LIFE_DAYS` old scores ``0.5``; older keeps halving. An
    empty/garbage ``upload_date`` degrades to :data:`RECENCY_NEUTRAL_DECAY` (a
    neutral mid value) rather than crashing — we never bury an item just because its
    date was unparseable. A future date (clock skew) is clamped to "today" (decay 1.0).

    Args:
        upload_date: The ``YYYYMMDD`` upload date, or "".
        reference_date: "Now" for the decay (defaults to today UTC). Injectable so
            tests are deterministic.

    Returns:
        A decay factor in ``(0, 1]`` (or exactly :data:`RECENCY_NEUTRAL_DECAY` on a
        bad date).

    Example:
        >>> from datetime import date
        >>> recency_decay("20260101", reference_date=date(2026, 1, 1))
        1.0
        >>> round(recency_decay("20251225", reference_date=date(2026, 1, 1)), 3)
        0.5
        >>> recency_decay("", reference_date=date(2026, 1, 1))
        0.5
    """
    reference = reference_date or datetime.now(timezone.utc).date()
    parsed = _parse_upload_date(upload_date)
    if parsed is None:
        return RECENCY_NEUTRAL_DECAY
    days_old = (reference - parsed).days
    if days_old <= 0:
        # Reason: an item dated today or (clock skew) in the future is maximally fresh.
        return 1.0
    return math.pow(0.5, days_old / RECENCY_HALF_LIFE_DAYS)


def score_item(
    item: RankableItem,
    config: Any,
    *,
    creator_baselines: Optional[dict[str, float]] = None,
    reference_date: Optional[date] = None,
    trending_multipliers: Optional[dict[str, float]] = None,
    engagement_percentiles: Optional[dict[str, float]] = None,
) -> float:
    """Score one item with Orbit's deterministic weighted derank formula (Rule 5).

    The score (higher == ranks earlier) is::

        score = priority_weight
              * CLUSTER_SIZE_NEUTRAL          # M3 hook (source diversity)
              * TRENDING_MULTIPLIER_NEUTRAL   # M3 hook (trending/scoop)
              * ( UNIQUENESS_BASELINE_BOOST                       # priority-scaled floor
                + RELATIVE_ENGAGEMENT_WEIGHT * relative_engagement  # vs creator's OWN baseline
                + RECENCY_WEIGHT * recency_decay
                + ABSOLUTE_ENGAGEMENT_WEIGHT * batch_percentile )   # vs the WHOLE X batch

    then, for an X item that is itself a quote, the whole score is multiplied by
    :data:`QUOTE_TWEET_MULTIPLIER` (so a quote ranks below an identical original).

    where ``relative_engagement = engagement_blend(item) - creator_baseline`` (the
    creator's batch-median blend; see :func:`compute_creator_engagement_baselines`).
    Because the bracket is multiplied by ``priority_weight``, a higher-weight creator
    always outranks an identical lower-weight one (DoD #1). The
    ``UNIQUENESS_BASELINE_BOOST`` floor inside the bracket — also multiplied by
    ``priority_weight`` — keeps a lone high-priority item with low engagement off the
    bottom (DoD #3). And because engagement is RELATIVE to the creator's own baseline,
    an item far above its creator's norm beats an item with higher RAW engagement
    that's normal-for-its-creator (DoD #2).

    Args:
        item: The :class:`RankableItem` to score.
        config: An :class:`lib.config.OrbitConfig` (read for ``creator_weights``).
        creator_baselines: Precomputed ``creator_external_id`` -> baseline-blend map
            (from :func:`compute_creator_engagement_baselines` over the batch). When
            None, the item's own blend is used as its baseline (relative engagement
            == 0 — neutral for a lone item).
        reference_date: "Now" for recency decay (defaults to today UTC; injectable
            for deterministic tests).
        trending_multipliers: OPTIONAL map of ``item_external_id`` -> trending/scoop
            multiplier (M3 / Phase 5 Sub-phase 4). Looks the item up by its
            ``item_external_id``; an item present in the map is multiplied by its
            (> 1.0) factor so a scoop/trending item's score rises ABOVE an otherwise
            identical non-scoop one. When None or the item is absent, the multiplier
            falls back to :data:`TRENDING_MULTIPLIER_NEUTRAL` (1.0) — so the M1/M2
            scoring path is byte-for-byte unchanged. The map is built by
            :func:`lib.external_trending.build_trending_multiplier_map` (kept out of this module
            so rerank stays trending-agnostic — it consumes whatever map it is given).
        engagement_percentiles: OPTIONAL map of ``item_external_id`` -> batch-engagement
            percentile (0.0–1.0) from :func:`compute_batch_engagement_percentile` over the
            X batch (Phase 8 Sub-phase 3). An item present in the map gains
            ``ABSOLUTE_ENGAGEMENT_WEIGHT * percentile`` in the intrinsic bracket; an item
            absent (every YouTube item) contributes 0.0, so YouTube scoring is unchanged.

    Returns:
        The item's derank score as a float.

    Example:
        >>> from types import SimpleNamespace
        >>> from datetime import date
        >>> config = SimpleNamespace(creator_weights={"UC1": 2.0})
        >>> item = RankableItem(
        ...     "v", "t", "c", "UC1", 1000, 50, 5, "20260101",
        ... )
        >>> score_item(config=config, item=item, reference_date=date(2026, 1, 1)) > 0  # doctest: +SKIP
        True
    """
    priority_weight = priority_weight_for(item, config)

    item_blend = engagement_blend(item)
    if creator_baselines is not None and item.creator_external_id in creator_baselines:
        creator_baseline = creator_baselines[item.creator_external_id]
    else:
        # Reason: with no batch baseline for this creator, treat the item as its own
        # baseline -> relative engagement 0 (neutral). A lone item is judged on
        # priority + recency + the uniqueness floor, not on raw engagement scale.
        creator_baseline = item_blend
    relative_engagement = item_blend - creator_baseline

    recency = recency_decay(item.upload_date, reference_date=reference_date)

    # Reason: the M3 trending/scoop multiplier replaces the neutral 1.0 no-op for items
    # present in the injected map; everything else keeps TRENDING_MULTIPLIER_NEUTRAL so
    # the M1/M2 score is unchanged when no map is passed (DoD #4 regression).
    if trending_multipliers:
        trending_multiplier = float(trending_multipliers.get(item.item_external_id, TRENDING_MULTIPLIER_NEUTRAL))
    else:
        trending_multiplier = TRENDING_MULTIPLIER_NEUTRAL

    # Reason: the absolute batch-engagement term rewards a tweet that ranks high across the
    # WHOLE X batch (not just vs its own creator). Items absent from the map (every YouTube
    # item, which is never passed into the percentile) contribute 0.0 — score-neutral for
    # YouTube (Phase 8 Sub-phase 3). Added alongside the creator-relative term in the bracket.
    if engagement_percentiles:
        engagement_percentile = float(engagement_percentiles.get(item.item_external_id, 0.0))
    else:
        engagement_percentile = 0.0

    intrinsic = (
        UNIQUENESS_BASELINE_BOOST
        + RELATIVE_ENGAGEMENT_WEIGHT * relative_engagement
        + RECENCY_WEIGHT * recency
        + ABSOLUTE_ENGAGEMENT_WEIGHT * engagement_percentile
    )
    score = priority_weight * CLUSTER_SIZE_NEUTRAL * trending_multiplier * intrinsic

    # Reason: a quote of someone else's tweet is derivative, so it ranks below an otherwise
    # identical original. Applied to the FINAL score (after the intrinsic bracket) so it
    # scales the whole ranking value. is_quote is always False for YouTube -> no-op there.
    if item.is_quote:
        score *= QUOTE_TWEET_MULTIPLIER

    log.log_debug(
        "rerank_scored_item",
        item_external_id=item.item_external_id,
        creator_external_id=item.creator_external_id,
        priority_weight=priority_weight,
        relative_engagement=round(relative_engagement, 4),
        recency_decay=round(recency, 4),
        trending_multiplier=round(trending_multiplier, 4),
        engagement_percentile=round(engagement_percentile, 4),
        is_quote=item.is_quote,
        score=round(score, 4),
    )
    return score


def derank_items(
    items: list[RankableItem],
    config: Any,
    *,
    reference_date: Optional[date] = None,
    trending_multipliers: Optional[dict[str, float]] = None,
) -> list[ScoredItem]:
    """Score every item and return them sorted DESCENDING by score (nothing dropped).

    Computes per-creator engagement baselines over the whole batch first (so
    relative-engagement is consistent), scores each item, and returns the items as
    :class:`ScoredItem`s sorted highest-score-first. **Nothing is dropped** — rank
    controls DENSITY (Sub-phase 2's tiering), never inclusion (api-contracts derank
    contract). Ties break by ``item_external_id`` for a stable, deterministic order.

    Args:
        items: The full batch of :class:`RankableItem`s to score.
        config: An :class:`lib.config.OrbitConfig` (read for ``creator_weights``).
        reference_date: "Now" for recency decay (defaults to today UTC; injectable
            for deterministic tests).
        trending_multipliers: OPTIONAL ``item_external_id`` -> trending/scoop
            multiplier map (M3). Passed through to :func:`score_item`. None (the M1/M2
            path) leaves every multiplier neutral, so ranking is unchanged (DoD #4).

    Returns:
        The :class:`ScoredItem`s sorted descending by score (``len`` == ``len(items)``).

    Example:
        >>> from types import SimpleNamespace
        >>> from datetime import date
        >>> config = SimpleNamespace(creator_weights={"UC_hi": 2.0, "UC_lo": 1.0})
        >>> hi = RankableItem("a", "t", "c", "UC_hi", 1000, 50, 5, "20260101")
        >>> lo = RankableItem("b", "t", "c", "UC_lo", 1000, 50, 5, "20260101")
        >>> ranked = derank_items([lo, hi], config, reference_date=date(2026, 1, 1))  # doctest: +SKIP
        >>> ranked[0].item.item_external_id  # higher priority sorts first  # doctest: +SKIP
        'a'
    """
    if not items:
        log.log_info("rerank_completed", item_count=0)
        return []

    creator_baselines = compute_creator_engagement_baselines(items)
    # Reason: the absolute-engagement percentile is computed over the X items ONLY, so a
    # tweet's percentile reflects its rank within the X batch (not diluted by YouTube
    # uploads whose raw counts live on a different scale). YouTube items are absent from
    # the map -> 0.0 absolute term -> YouTube scoring is byte-for-byte unchanged.
    engagement_percentiles = compute_batch_engagement_percentile([item for item in items if is_x_item(item)])
    scored = [
        ScoredItem(
            item=item,
            score=score_item(
                item,
                config,
                creator_baselines=creator_baselines,
                reference_date=reference_date,
                trending_multipliers=trending_multipliers,
                engagement_percentiles=engagement_percentiles,
            ),
        )
        for item in items
    ]
    # Reason: sort descending by score; break ties on item_external_id so the order is
    # stable and deterministic across runs (a flapping order would confuse the user).
    scored.sort(key=lambda scored_item: (-scored_item.score, scored_item.item.item_external_id))

    log.log_info(
        "rerank_completed",
        item_count=len(scored),
        creator_count=len(creator_baselines),
        top_score=round(scored[0].score, 4),
        bottom_score=round(scored[-1].score, 4),
    )
    return scored


def cap_x_items(
    scored_items: list[ScoredItem],
    *,
    cap: int = X_DIGEST_TWEET_CAP,
) -> tuple[list[ScoredItem], int]:
    """Keep only the top-``cap`` X tweets; pass every YouTube item through untouched.

    Top-N virality selection (Phase 8 Sub-phase 3) supersedes density-not-inclusion for
    the X half only: after scoring, only the highest-scoring ``cap`` tweets survive into
    the digest so a noisy follow list cannot flood it. YouTube items are NEVER capped —
    they keep the density-not-inclusion contract.

    ``scored_items`` is assumed already sorted DESCENDING by score (as :func:`derank_items`
    returns it), so the first ``cap`` X items encountered are the top ones. Relative order
    is preserved: kept items come back in the same order they arrived, so the caller can
    hand the result straight to tiering without re-sorting.

    Args:
        scored_items: The full, score-sorted batch (X + YouTube) from :func:`derank_items`.
        cap: Max number of X tweets to keep. Defaults to :data:`X_DIGEST_TWEET_CAP`.

    Returns:
        A ``(kept, dropped_count)`` tuple: ``kept`` is the surviving items (all YouTube +
        the top-``cap`` X items, order preserved); ``dropped_count`` is how many X items
        were cut (0 when the batch holds ``<= cap`` X items).

    Example:
        >>> yt = ScoredItem(RankableItem("y", "t", "c", "UC", 1, None, None, ""), 5.0)
        >>> tw = ScoredItem(
        ...     RankableItem("x", "t", "c", "h", 1, None, None, "", card_url="https://x.com/h/status/x"), 4.0
        ... )
        >>> kept, dropped = cap_x_items([yt, tw], cap=1)  # doctest: +SKIP
        >>> len(kept), dropped  # doctest: +SKIP
        (2, 0)
    """
    kept: list[ScoredItem] = []
    x_kept_count = 0
    dropped_count = 0
    for scored_item in scored_items:
        if is_x_item(scored_item.item):
            if x_kept_count >= cap:
                dropped_count += 1
                continue
            x_kept_count += 1
        kept.append(scored_item)
    log.log_info(
        "x_digest_cap_applied",
        cap=cap,
        x_kept_count=x_kept_count,
        x_cap_dropped_count=dropped_count,
    )
    return kept, dropped_count
