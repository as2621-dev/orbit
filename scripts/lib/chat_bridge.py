"""Orbit chat bridge — pure string builders for the Gmail-connector chat link (issue #7).

"One tap from the email opens a Claude conversation with today's digest loaded." The
mechanism (final, after two dead designs — see
``docs/solutions/architecture-patterns/headless-artifact-publish-spike-go-nogo.md``):

  1. The digest email BODY carries the full ``digest.md`` markdown (built here by
     :func:`build_email_body`), under a stable, searchable subject
     (:func:`build_digest_subject`).
  2. A ``https://claude.ai/new?q=<prompt>`` link (:func:`build_chat_link`) prefills a
     Claude chat with a prompt telling it to find that email via the owner's own Gmail
     connector and read the digest out of the body. GO check passed 2026-07-18.

Everything in this module is PURE (deterministic, no I/O, no network — Rule 5): link
construction is string-building only. The archive push (the slice's one network step)
lives in :mod:`lib.archive`; the send path stays in :mod:`lib.deliver`.
"""

from __future__ import annotations

from datetime import date
from typing import Optional
from urllib.parse import quote

# The claude.ai new-chat prefill endpoint. The ``q`` value is consumed client-side by the
# SPA after login; the prompt rides entirely inside it.
CLAUDE_NEW_CHAT_BASE_URL: str = "https://claude.ai/new?q="

# The stable subject prefix is the CONTRACT between the email and the chat prompt: the
# prompt searches Gmail for this exact prefix, so the two must never drift apart. Both
# the subject builder and the Gmail search query derive from this one constant.
DIGEST_SUBJECT_PREFIX: str = "Orbit Digest"

# The Gmail search the prefilled chat runs. Derived from the prefix so subject and query
# cannot drift. ``newer_than:2d`` keeps "most recent" cheap and tolerant of a late-night
# read; it targets INBOX MAIL — never ``in:draft`` (the GO check showed drafts are
# invisible to thread search; production digests are ordinary inbox messages).
GMAIL_SEARCH_QUERY: str = f'subject:"{DIGEST_SUBJECT_PREFIX}" newer_than:2d'


def build_digest_subject(digest_date: date, tldr: str) -> str:
    """Compose the stable, searchable email subject: ``Orbit Digest — YYYY-MM-DD: <TL;DR>``.

    The prefix + ISO date lead so the Gmail search in :func:`build_chat_prompt` (which
    matches on ``subject:"Orbit Digest"``) always finds the digest; the TL;DR trails as
    human context. A blank TL;DR degrades to prefix + date — never an empty subject.

    Args:
        digest_date: The digest's date (the caller supplies today; injectable in tests).
        tldr: The one-line delivery TL;DR (may be blank on a quiet day).

    Returns:
        The composed subject line (unsanitized — the send path owns header hygiene).

    Example:
        >>> build_digest_subject(date(2026, 7, 18), "Orbit: 3 new items")
        'Orbit Digest — 2026-07-18: Orbit: 3 new items'
    """
    subject_lead = f"{DIGEST_SUBJECT_PREFIX} — {digest_date.isoformat()}"
    tldr_text = tldr.strip()
    if not tldr_text:
        return subject_lead
    return f"{subject_lead}: {tldr_text}"


def build_chat_prompt() -> str:
    """Compose the fixed prompt the ``claude.ai/new?q=`` link prefills.

    Tells Claude to use the owner's Gmail connector, find the most recent digest email
    by the stable subject prefix (:data:`GMAIL_SEARCH_QUERY`), read its full body (which
    IS the digest markdown), and start the discussion. Deliberately date-free: the query
    already picks the most recent digest, so today's link never goes stale mid-read.

    Returns:
        The plain-text (un-encoded) prompt string.
    """
    return (
        "Using your Gmail tools, find my most recent email with a subject starting "
        f"'{DIGEST_SUBJECT_PREFIX}' (search query: {GMAIL_SEARCH_QUERY}). Read its full "
        "body — it is my Orbit daily digest in markdown. Then give me a quick overview "
        "of the top items and discuss any item I ask about."
    )


def build_chat_link(prompt: Optional[str] = None) -> str:
    """Build the ``claude.ai/new?q=`` prefill link for ``prompt`` (default: the digest prompt).

    The ENTIRE prompt is percent-encoded with ``quote(prompt, safe="")`` — spike #5 AC4:
    a raw ``&`` / ``#`` / ``?`` inside ``?q=`` silently truncates the prefill (the tail
    leaks out as separate query params or a fragment). Encoding everything makes
    truncation structurally impossible regardless of prompt content.

    Args:
        prompt: The plain-text prompt to prefill; None uses :func:`build_chat_prompt`.

    Returns:
        The full ``https://claude.ai/new?q=<encoded prompt>`` link.

    Example:
        >>> build_chat_link("A & B #1").endswith("A%20%26%20B%20%231")
        True
    """
    prompt_text = build_chat_prompt() if prompt is None else prompt
    return f"{CLAUDE_NEW_CHAT_BASE_URL}{quote(prompt_text, safe='')}"


# Gmail clips message bodies over ~102KB (~102,000 bytes) with NO error — the digest tail
# would silently vanish from what the chat bridge reads. The guard truncates BELOW that
# line (100,000 bytes, not 100*1024=102,400 which would leave no margin) and says so out
# loud (Rule 12: fail loud, never silently degrade).
EMAIL_BODY_MAX_BYTES: int = 100_000
TRUNCATION_NOTICE: str = (
    "[Digest truncated here to stay under Gmail's clipping limit — "
    "the full digest is attached as HTML and archived.]"
)


def build_email_body(tldr: str, digest_markdown: str) -> str:
    """Assemble the digest email body: TL;DR lead -> chat link -> full digest markdown.

    The body IS the fetchable store the chat prompt reads via the Gmail connector, so
    the full ``digest.md`` markdown rides below the fold. If the assembled body would
    exceed :data:`EMAIL_BODY_MAX_BYTES` (Gmail's silent-clip threshold), the markdown
    section is cut at a UTF-8-safe boundary and :data:`TRUNCATION_NOTICE` is appended —
    Gmail must never be the one doing the cutting. An empty ``digest_markdown`` (the
    twin failed to render — PRD story #19) degrades to TL;DR + link + a pointer at the
    HTML attachment.

    Args:
        tldr: The one-line delivery TL;DR (leads the body; blank falls back).
        digest_markdown: The full ``digest.md`` text ("" when unavailable).

    Returns:
        The composed plain-text body, guaranteed under the Gmail clip threshold.
    """
    tldr_text = tldr.strip() or "Your Orbit digest is ready."
    chat_line = f"Chat about this digest: {build_chat_link()}"
    header = f"{tldr_text}\n\n{chat_line}\n"

    markdown_text = digest_markdown.strip()
    if not markdown_text:
        return f"{header}\nYour full Orbit digest is attached — open it in any browser."

    body = f"{header}\n---\n\n{markdown_text}"
    body_bytes = body.encode("utf-8")
    if len(body_bytes) <= EMAIL_BODY_MAX_BYTES:
        return body

    # Truncate the MARKDOWN section only — the TL;DR + chat link always survive. The cut
    # lands on a byte budget, then decodes with errors="ignore" so a multibyte character
    # split at the boundary is dropped whole rather than emitted as mojibake.
    tail = f"\n\n{TRUNCATION_NOTICE}"
    # max(0, ...): a pathologically long TL;DR must yield an EMPTY markdown section, not a
    # negative slice (bytes[:-n] keeps almost everything and would bust the size promise).
    markdown_byte_budget = max(
        0, EMAIL_BODY_MAX_BYTES - len(f"{header}\n---\n\n".encode()) - len(tail.encode("utf-8"))
    )
    truncated_markdown = markdown_text.encode("utf-8")[:markdown_byte_budget].decode("utf-8", errors="ignore")
    return f"{header}\n---\n\n{truncated_markdown}{tail}"
