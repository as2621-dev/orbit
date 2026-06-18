"""Orbit's local scoring signals for trending (Phase 5 / Stage 5a).

Lifted in SHAPE (not imported) from last30days/signals.py — the reference's
``log1p_safe`` compression, its per-actor reference-normalization IDEA (the
``_VOTE_LOG_REFERENCE`` pattern: divide a log-compressed value by a per-actor
reference so values are comparable across actors of different scale), and its
``normalize`` (min-max to ``[0.0, 1.0]``). The reference's signals are STATIC
per-item — they have NO velocity, NO baseline, NO time dimension. Those are NEW
here (see :mod:`lib.trending`); this module supplies only the reusable primitives
trending is BUILT on.

Why these primitives and not raw counts: a creator with millions of followers and
a creator with thousands operate on totally different engagement scales. Raw
counts let the big creator dominate purely by scale. ``log1p_safe`` compresses the
scale; per-creator reference-normalization (:func:`baseline_relative_ratio`) then
expresses "how far above THIS creator's own normal" — the core Stage-5 distinction
(baseline-relative, NOT raw popularity).

Rule 5 — 100% deterministic math. NO LLM, NO network, NO new pip dependency
(stdlib + :func:`lib.rerank.log1p_safe` only).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``lib`` importable whether imported as the package member ``lib.signals``
# (via orbit.py's sys.path insert of the scripts dir) or run from the scripts dir
# directly. Mirrors rerank.py / cluster.py / fusion.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

# Reason: reuse rerank.py's log1p_safe verbatim rather than re-deriving it — the
# brief explicitly prefers importing the existing helper over duplication, and a
# second copy would risk the two drifting apart.
from lib.rerank import log1p_safe  # noqa: E402  (import must follow the sys.path inserts above)

__all__ = ["log1p_safe", "baseline_relative_ratio", "normalize"]


# How far below 1.0 a "no historical baseline" creator's spike ratio is treated
# as. A creator with no history can't be judged above-or-below its own normal, so
# it degrades to exactly the neutral value (Rule 12 — no crash, no false spike).
NEUTRAL_SPIKE_RATIO: float = 1.0

# The smallest baseline we will divide by. A creator whose historical engagement
# blend is ~0 (e.g. a brand-new creator whose only prior posts had no engagement)
# would otherwise make every new post look like an infinite spike. Flooring the
# denominator caps that so a near-zero baseline degrades to neutral rather than
# exploding. log1p_safe(1) == log1p(1) ≈ 0.693 is a deliberately small, non-zero
# floor (one unit of engagement).
_BASELINE_FLOOR: float = 1e-3


def baseline_relative_ratio(current_blend: float, baseline_blend: float | None) -> float:
    """Express current engagement as a ratio against the creator's OWN baseline.

    This is the per-actor reference-normalization idea lifted from the reference's
    ``_VOTE_LOG_REFERENCE`` (divide a value by a per-actor reference so it is
    comparable across actors of wildly different scale), adapted to the trending
    case: the "reference" is the creator's OWN historical engagement baseline, so
    the ratio answers "how many times the creator's normal level is this?" — NOT
    "how big is this in absolute terms". A ratio of ``5.0`` means five times the
    creator's normal; ``1.0`` means exactly normal; ``< 1.0`` means below normal.

    Both inputs are expected to already be ``log1p_safe``-compressed engagement
    blends (see :func:`lib.rerank.engagement_blend`) — so this is a ratio of
    log-space magnitudes, which keeps a 10×-raw spike from a huge creator and a
    10×-raw spike from a tiny creator on a comparable footing.

    Args:
        current_blend: The item's current log-space engagement blend (``>= 0``).
        baseline_blend: The creator's historical baseline log-space blend, or None
            when the creator has no history at all.

    Returns:
        The baseline-relative ratio. ``1.0`` (neutral) when there is no usable
        baseline (None, non-positive, or below the floor), so a creator without
        history is never spuriously flagged as spiking (Rule 12).

    Example:
        >>> baseline_relative_ratio(10.0, 2.0)
        5.0
        >>> baseline_relative_ratio(5.0, None)
        1.0
        >>> baseline_relative_ratio(5.0, 0.0)
        1.0
    """
    if baseline_blend is None or baseline_blend <= _BASELINE_FLOOR:
        return NEUTRAL_SPIKE_RATIO
    return current_blend / baseline_blend


def normalize(values: list[float | None]) -> list[float | None]:
    """Min-max scale a list of values to ``[0.0, 1.0]`` (None passes through).

    Lifted in shape from the reference's ``normalize`` (which scaled to ``[0,100]``
    ints); here we keep floats in ``[0.0, 1.0]`` because the trending pipeline
    composes these as multiplicative/additive factors, not display percentages.
    When all valid values are equal, each maps to ``0.5`` (a neutral midpoint) so a
    single-item or all-tied batch does not collapse to all-zero.

    Args:
        values: Values to scale; None entries pass through as None.

    Returns:
        The scaled values in the same order; None where the input was None.

    Example:
        >>> normalize([0.0, 5.0, 10.0])
        [0.0, 0.5, 1.0]
        >>> normalize([3.0, 3.0])
        [0.5, 0.5]
        >>> normalize([None, 2.0, 4.0])
        [None, 0.0, 1.0]
    """
    valid = [value for value in values if value is not None]
    if not valid:
        return [None for _ in values]
    low = min(valid)
    high = max(valid)
    if high <= low:
        return [None if value is None else 0.5 for value in values]
    span = high - low
    return [None if value is None else (value - low) / span for value in values]
