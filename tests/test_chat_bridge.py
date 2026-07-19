"""Tests for lib.chat_bridge — the pure Gmail-bridge string builders (issue #7).

The chat bridge is the "one tap from the email opens a Claude conversation" mechanism:
the digest email body carries the full markdown, and a ``claude.ai/new?q=`` prefill link
instructs Claude to find that email via the owner's Gmail connector and read it. Every
function here is PURE (no I/O, no network) — the only network step in the slice is the
archive push, which lives in ``lib.archive``.

Why these tests matter (Rule 9 — encode WHY, not just WHAT):

  * The subject prefix is the CONTRACT between the email and the chat prompt: the prompt
    searches Gmail by ``subject:"Orbit Digest"``. If either side drifts, the bridge
    silently finds nothing — so the prefix/query coupling is pinned here.
  * Spike #5 AC4: a raw ``&`` / ``#`` in a ``?q=`` prompt SILENTLY truncates the prefill.
    The encoding tests feed hostile prompt content and assert the round-trip survives.
  * The GO check (2026-07-18) showed a *draft* only surfaces via the drafts list, not
    thread search — the prompt must target inbox mail, never ``in:draft``.
  * Gmail clips message bodies over ~102KB with no error; the body guard must truncate
    LOUDLY (explicit notice line) before Gmail ever clips silently.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote

# Make the skill's scripts dir importable so ``from lib import chat_bridge`` resolves.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import chat_bridge  # noqa: E402


# --- build_digest_subject (the stable, searchable subject) -----------------------


def test_build_digest_subject_has_stable_prefix_date_and_tldr() -> None:
    """Subject is ``Orbit Digest — YYYY-MM-DD: <TL;DR>`` — prefix first, always.

    WHY: the chat prompt finds the digest by searching Gmail for this exact subject
    prefix. A subject that leads with the raw TL;DR (the pre-#7 behavior) is
    unsearchable; the stable prefix IS the bridge's address.
    """
    subject = chat_bridge.build_digest_subject(date(2026, 7, 18), "Orbit: 3 new items — top: Big News")

    assert subject == "Orbit Digest — 2026-07-18: Orbit: 3 new items — top: Big News"
    assert subject.startswith(chat_bridge.DIGEST_SUBJECT_PREFIX)


def test_build_digest_subject_blank_tldr_still_searchable() -> None:
    """A blank TL;DR degrades to prefix + date, never a bare/empty subject.

    WHY: the bridge must survive a quiet day — the Gmail search matches on the prefix,
    so the subject must carry it even when there is nothing to summarize.
    """
    subject = chat_bridge.build_digest_subject(date(2026, 7, 18), "   ")

    assert subject.startswith("Orbit Digest — 2026-07-18")
    assert not subject.rstrip().endswith(":"), "no dangling separator when the TL;DR is blank"


# --- build_chat_prompt (what the prefilled Claude chat is told to do) -------------


def test_chat_prompt_searches_inbox_by_subject_prefix_never_drafts() -> None:
    """The prompt carries the stable Gmail query and never targets drafts.

    WHY: the GO check showed a *draft* only surfaces via the drafts list, not thread
    search — production digests are inbox messages, so the prompt must search by the
    stable subject prefix (``subject:"Orbit Digest" newer_than:2d``) and must never
    say ``in:draft``. This is the coupling test: the query embeds the SAME prefix
    constant the subject builder uses, so the two cannot drift apart silently.
    """
    prompt = chat_bridge.build_chat_prompt()

    assert chat_bridge.GMAIL_SEARCH_QUERY in prompt, "the prompt carries the pinned Gmail query verbatim"
    assert 'subject:"Orbit Digest" newer_than:2d' == chat_bridge.GMAIL_SEARCH_QUERY
    assert chat_bridge.DIGEST_SUBJECT_PREFIX in chat_bridge.GMAIL_SEARCH_QUERY
    assert "in:draft" not in prompt, "drafts are invisible to thread search — never target them"
    assert "Gmail" in prompt, "the prompt must invoke the Gmail connector by name"


# --- build_chat_link (the claude.ai/new?q= prefill — spike #5 AC4 governs) --------


def test_chat_link_percent_encodes_the_entire_prompt() -> None:
    """The whole prompt is percent-encoded and round-trips losslessly.

    WHY: spike #5 AC4 — a raw ``&`` or ``#`` inside ``?q=`` silently truncates the
    prefill (the tail leaks out as separate query params / a fragment). The only safe
    posture is encoding the ENTIRE prompt with no safe characters, so the query part
    may contain nothing but ``%``-escapes and unreserved characters.
    """
    link = chat_bridge.build_chat_link()
    assert link.startswith("https://claude.ai/new?q=")

    query_value = link.removeprefix("https://claude.ai/new?q=")
    for forbidden_character in ('&', "#", "?", '"', "'", " ", "\n"):
        assert forbidden_character not in query_value, f"raw {forbidden_character!r} would truncate the prefill"
    assert unquote(query_value) == chat_bridge.build_chat_prompt(), "encoding must be lossless"


def test_chat_link_survives_hostile_prompt_content() -> None:
    """A prompt carrying quotes, ampersands, ``#`` and newlines still round-trips intact.

    WHY: the AC pins the encoding edge cases — TL;DR/date-shaped content with URL
    metacharacters must yield a fully percent-encoded, UNtruncated link. This drives
    the builder with the hostile string directly, proving the encoder is total (no
    character class survives raw).
    """
    hostile_prompt = 'Digest "2026-07-18" — tops: A&B #1 100% legit?\nline two & #tag'

    link = chat_bridge.build_chat_link(hostile_prompt)

    query_value = link.removeprefix("https://claude.ai/new?q=")
    for forbidden_character in ("&", "#", "?", '"', "'", " ", "\n"):
        assert forbidden_character not in query_value, f"raw {forbidden_character!r} leaked into the query"
    assert unquote(query_value) == hostile_prompt, "nothing was truncated or lost"


def test_chat_link_stays_under_the_safe_url_ceiling() -> None:
    """The default link stays well under the ~2,048-char cross-browser ceiling.

    WHY: spike #5 measured the safe cross-browser/CDN URL ceiling at ~2,048 chars; the
    design keeps the prompt short (fetch-on-open via Gmail, never the digest inlined).
    If someone grows the prompt past the ceiling, the bridge dies silently on some
    clients — this test makes that growth loud instead.
    """
    assert len(chat_bridge.build_chat_link()) < 2048


# --- build_email_body (TL;DR -> chat link -> full digest markdown) ----------------


def test_email_body_is_tldr_then_chat_link_then_full_markdown() -> None:
    """Happy path: the body leads with the TL;DR, then the chat link, then the markdown.

    WHY: the body IS the fetchable store — the chat prompt reads the digest out of this
    email. Order matters: the TL;DR + link must sit above the fold (a phone preview),
    with the full markdown below for Claude (and the human) to read.
    """
    digest_markdown = "# Orbit Digest\n\n## Big News\nA thing happened.\n"

    body = chat_bridge.build_email_body("Orbit: 3 new items", digest_markdown)

    tldr_position = body.index("Orbit: 3 new items")
    link_position = body.index(chat_bridge.build_chat_link())
    markdown_position = body.index(digest_markdown.strip())
    assert tldr_position < link_position < markdown_position
    assert "Chat about this digest" in body, "the link is labeled, not bare"


def test_email_body_truncates_loudly_before_gmail_clips_silently() -> None:
    """A body that would exceed ~100KB is truncated WITH an explicit notice line.

    WHY: Gmail clips message bodies over ~102KB with no error — the digest tail would
    silently vanish from what the chat bridge reads. The guard truncates the markdown
    section first and appends a loud notice, so a clipped digest is visible to both the
    human and Claude. Multibyte content proves the cut never splits a UTF-8 sequence.
    """
    oversized_markdown = ("naïve café ☕ digest line — " * 8000) + "SENTINEL-TAIL"

    body = chat_bridge.build_email_body("Orbit: 1 new item", oversized_markdown)

    assert len(body.encode("utf-8")) <= chat_bridge.EMAIL_BODY_MAX_BYTES, "the body must fit under the clip line"
    assert chat_bridge.TRUNCATION_NOTICE in body, "truncation must be loud, never silent"
    assert "SENTINEL-TAIL" not in body, "the tail was actually cut"
    assert chat_bridge.build_chat_link() in body, "the chat link always survives truncation"
    body.encode("utf-8").decode("utf-8")  # a split multibyte char would have raised above already


def test_email_body_small_digest_is_never_truncated() -> None:
    """Boundary: a body comfortably under the cap carries the FULL markdown, no notice.

    WHY: the guard exists for the pathological day; on a normal day nothing may be cut
    and no notice may appear, or every digest would look damaged.
    """
    digest_markdown = "# Orbit Digest\n\nShort and sweet.\n"

    body = chat_bridge.build_email_body("Orbit: 1 new item", digest_markdown)

    assert digest_markdown.strip() in body
    assert chat_bridge.TRUNCATION_NOTICE not in body


def test_email_body_without_markdown_still_carries_the_chat_link() -> None:
    """Fail-soft: an empty markdown (digest.md unwritable) still yields a useful body.

    WHY: PRD story #19 — the pipeline ships the email without chat-bridge extras if a
    fail-soft step fails. The markdown twin failing to render must not strip the TL;DR
    or the chat link from the email; the digest is still attached as HTML.
    """
    body = chat_bridge.build_email_body("Orbit: 2 new items", "")

    assert "Orbit: 2 new items" in body
    assert chat_bridge.build_chat_link() in body
    assert "attached" in body.lower(), "the body points at the HTML attachment when markdown is absent"
