"""Lexical near-duplicate / similarity primitives for Orbit clustering (Phase 5).

Lifted in shape (NOT imports) from last30days/dedupe.py and adapted to Orbit:

  * The reference's ``from . import cjk, schema`` dependencies are REMOVED. Orbit
    has no CJK segmentation module, so tokenization is a plain whitespace split of
    the normalized text (good enough for the English-first YouTube+X stream Orbit
    serves; a CJK segmenter is a future enhancement, not an M3 requirement — Rule 2).
  * Operates on raw strings (an item's ``title`` / tweet text), never on a
    ``schema.SourceItem`` — Orbit's unit is :class:`lib.rerank.RankableItem` and the
    cluster driver passes its title text in directly.

This module owns the pure similarity machinery — text normalization, char-trigram
n-grams, stopword-filtered token sets, the Jaccard primitive, the cached
:class:`PreparedText`, and :func:`prepared_similarity` / :func:`hybrid_similarity`.
:mod:`lib.cluster` consumes these for the greedy single-leader pass. Rule 5: pure,
deterministic lexical math — NO embedding model, NO network, NO LLM.
"""

from __future__ import annotations

import re

# Tokens too common to signal a shared topic. Lifted verbatim from the reference's
# base STOPWORDS (the CJK stopword union is dropped — see the module docstring).
STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "for",
        "how",
        "is",
        "in",
        "of",
        "on",
        "and",
        "with",
        "from",
        "by",
        "at",
        "this",
        "that",
        "it",
        "what",
        "are",
        "do",
        "can",
    }
)


def normalize_text(text: str) -> str:
    """Lower-case, strip punctuation to spaces, and collapse whitespace.

    The shared normalization step every similarity primitive runs first so that
    ``"Apple's M5 chip!"`` and ``"apple m5 chip"`` compare equal at the lexical
    level. Punctuation becomes whitespace (not removed) so word boundaries survive.

    Args:
        text: Any raw text (an item title or tweet body).

    Returns:
        The normalized text: lower-cased, punctuation-as-space, single-spaced,
        stripped. Empty input returns ``""``.

    Example:
        >>> normalize_text("Apple's M5 chip!!")
        'apple s m5 chip'
    """
    lowered = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _ngrams_of_normalized(normalized: str, n: int = 3) -> set[str]:
    """Return the set of length-``n`` character n-grams over already-normalized text.

    Args:
        normalized: Text already passed through :func:`normalize_text`.
        n: The n-gram length (default 3 — char-trigrams, the reference's choice).

    Returns:
        The set of character n-grams. For text shorter than ``n`` the whole string
        is the single n-gram (or an empty set when the text is empty).
    """
    if len(normalized) < n:
        return {normalized} if normalized else set()
    return {normalized[index : index + n] for index in range(len(normalized) - n + 1)}


def get_ngrams(text: str, n: int = 3) -> set[str]:
    """Normalize ``text`` then return its character n-gram set.

    Args:
        text: Raw text to n-gram.
        n: The n-gram length (default 3).

    Returns:
        The character n-gram set of the normalized text.

    Example:
        >>> sorted(get_ngrams("abcd"))
        ['abc', 'bcd']
    """
    return _ngrams_of_normalized(normalize_text(text), n)


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    """Jaccard index ``|A ∩ B| / |A ∪ B|`` of two sets (0.0 when either is empty).

    Args:
        left: First set (n-grams or tokens).
        right: Second set.

    Returns:
        The Jaccard similarity in ``[0.0, 1.0]``. ``0.0`` when either set is empty.

    Example:
        >>> jaccard_similarity({"a", "b"}, {"b", "c"})
        0.3333333333333333
    """
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _tokenize(normalized: str) -> frozenset[str]:
    """Split normalized text into a stopword-filtered token set (len > 1 tokens).

    Plain whitespace split (no CJK segmentation — see the module docstring). Tokens
    of length <= 1 and :data:`STOPWORDS` are dropped so common filler does not inflate
    similarity between unrelated items.

    Args:
        normalized: Text already passed through :func:`normalize_text`.

    Returns:
        The frozenset of significant tokens.
    """
    return frozenset(token for token in normalized.split() if len(token) > 1 and token not in STOPWORDS)


def token_jaccard(text_a: str, text_b: str) -> float:
    """Jaccard similarity over the two texts' stopword-filtered token sets.

    Args:
        text_a: First raw text.
        text_b: Second raw text.

    Returns:
        Token-level Jaccard similarity in ``[0.0, 1.0]``.

    Example:
        >>> round(token_jaccard("apple m5 chip", "apple m5 launch"), 3)
        0.5
    """
    tokens_a = _tokenize(normalize_text(text_a))
    tokens_b = _tokenize(normalize_text(text_b))
    return jaccard_similarity(set(tokens_a), set(tokens_b))


def hybrid_similarity(text_a: str, text_b: str) -> float:
    """Max of char-trigram Jaccard and stopword-filtered token Jaccard.

    The reference's core similarity score: the char-trigram pass catches shared
    sub-word structure (typos, inflections) while the token pass catches shared
    keywords even when surrounding wording differs. Taking the MAX means either
    signal alone can flag overlap.

    Args:
        text_a: First raw text.
        text_b: Second raw text.

    Returns:
        The hybrid similarity in ``[0.0, 1.0]``.

    Example:
        >>> hybrid_similarity("apple m5 chip", "apple m5 chip") == 1.0
        True
    """
    return max(
        jaccard_similarity(get_ngrams(text_a), get_ngrams(text_b)),
        token_jaccard(text_a, text_b),
    )


class PreparedText:
    """Pre-computed n-gram + token representations for fast repeated similarity.

    Clustering compares each item against every existing cluster leader, so the
    expensive normalization/tokenization is done ONCE per item and cached here, then
    cheap set operations run in the inner loop. Lifted from the reference's
    ``_PreparedText`` (renamed public — :mod:`lib.cluster` imports it).

    Attributes:
        ngrams: The char-trigram set of the normalized text.
        tokens: The stopword-filtered token frozenset of the normalized text.
    """

    __slots__ = ("ngrams", "tokens")

    def __init__(self, raw: str) -> None:
        """Pre-compute the n-gram and token sets for ``raw``.

        Args:
            raw: The raw text (an item title or tweet body) to prepare.
        """
        normalized = normalize_text(raw)
        self.ngrams: set[str] = _ngrams_of_normalized(normalized)
        self.tokens: frozenset[str] = _tokenize(normalized)


def prepared_similarity(left: PreparedText, right: PreparedText) -> float:
    """Hybrid similarity over two :class:`PreparedText`s (max of n-gram / token Jaccard).

    The cached-input form of :func:`hybrid_similarity` used in the clustering inner
    loop, so each item's n-gram/token sets are computed once not per comparison.

    Args:
        left: First prepared text.
        right: Second prepared text.

    Returns:
        The hybrid similarity in ``[0.0, 1.0]``.

    Example:
        >>> a = PreparedText("apple m5 chip launch")
        >>> b = PreparedText("apple m5 chip launch")
        >>> prepared_similarity(a, b) == 1.0
        True
    """
    return max(
        jaccard_similarity(left.ngrams, right.ngrams),
        jaccard_similarity(set(left.tokens), set(right.tokens)),
    )
