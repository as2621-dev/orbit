"""Keyless web search — the bounded external cross-search egress (Phase 5 / Stage 5b).

This is the ONLY external-egress module in the phase. It performs a light, KEYLESS
web search (NO API key, NO secret, NO new pip dependency) to answer one question:
"does this internal-trending topic ALSO have signal OUTSIDE the user's network?"
Sub-phase 3's :func:`lib.external_trending.tag_external_corroboration` consumes the RESULT
COUNT to tag each item ``corroborated`` (big outside the network too) vs ``scoop``
(your people first — little external signal).

Design — the network boundary is INJECTABLE (the critical seam):

    Orbit has no ``lib.http`` helper (unlike the reference this is lifted from). So
    rather than importing a fetcher, the network call is funnelled through ONE
    injectable callable — a ``page_fetcher: (url: str) -> str`` that returns raw
    HTML. The default :func:`default_page_fetcher` uses the Python STDLIB
    (``urllib.request``) so there is NO new dependency. Tests inject a fake fetcher
    (or a fake ``search_fn``) and NEVER touch the network — the DoD requires no live
    web call. :func:`keyless_search` is the high-level entry point trending injects.

Two layers, both replaceable in tests:
  * :func:`keyless_search(query, *, count, page_fetcher) -> list[SearchResult]` —
    the high-level ``search_fn`` trending takes. Never raises; returns ``[]`` on any
    failure (Rule 12 — a degraded search must not crash the digest).
  * :func:`default_page_fetcher(url) -> str` — the stdlib network boundary. The ONLY
    place a live request happens. Tests replace it.

The result shape is a tiny dataclass (:class:`SearchResult`: title/url/snippet) —
deliberately simpler than the reference's dict, because trending only needs the
COUNT and (for logging) the titles, not the full grounding contract.

Rule 5 — this module does NO classification (that is trending's deterministic
threshold). It only fetches + parses. Rule 12 — never raises. CSO — keyless, no
key, no secret; the only thing sent out is the public query string.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

# Make ``lib`` importable whether imported as the package member
# ``lib.web_search_keyless`` (via orbit.py's sys.path insert of the scripts dir) or
# run from the scripts dir directly. Mirrors trending.py / cluster.py's header.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)

# Keyless DuckDuckGo HTML endpoint — no key, no instance to maintain. The reference
# this is lifted from uses the same endpoint; Orbit keeps only the DDG rung (no
# SearXNG config) to stay zero-config and keyless.
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# A short, polite timeout — a slow search must not stall the whole digest. The
# fetch is best-effort: on timeout we return no results and the item degrades to its
# default tag (Rule 12).
_FETCH_TIMEOUT_SECONDS: float = 8.0

# A plain UA so the keyless endpoint serves the HTML page (some endpoints 403 an
# empty UA). No cookies, no auth header — nothing secret is ever sent.
_USER_AGENT: str = "Mozilla/5.0 (compatible; OrbitDigest/1.0; +keyless-web-search)"

_TAG_RE = re.compile(r"<[^>]+>")
_RESULT_A_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SearchResult:
    """One external web-search hit (the keyless cross-search unit).

    Deliberately minimal — trending needs the COUNT (corroboration vs scoop) and the
    title/url for logging/debugging, nothing more.

    Attributes:
        result_title: The result's title text (HTML-stripped). May be empty.
        result_url: The result's destination URL (http/https only).
        result_snippet: A short text snippet (HTML-stripped). May be empty.
    """

    result_title: str
    result_url: str
    result_snippet: str


# A search function is any callable ``(query: str) -> list[SearchResult]``. This is
# the seam :func:`lib.external_trending.tag_external_corroboration` injects — production wires
# :func:`keyless_search`; tests wire a fake returning canned results.
SearchFn = Callable[[str], list[SearchResult]]

# A page fetcher is any callable ``(url: str) -> str`` returning raw HTML (or "" on
# failure). The ONLY network boundary — :func:`default_page_fetcher` is the stdlib
# implementation; tests inject a fake so no live request is made.
PageFetcher = Callable[[str], str]


def _strip_html(fragment: str) -> str:
    """Strip tags and unescape entities from an HTML fragment.

    Args:
        fragment: A raw HTML fragment (may be empty/None-ish).

    Returns:
        The plain-text content, trimmed.
    """
    return html.unescape(_TAG_RE.sub("", fragment or "")).strip()


def _unwrap_ddg_redirect(href: str) -> str:
    """Resolve DuckDuckGo's ``//duckduckgo.com/l/?uddg=<encoded>`` redirect wrapper.

    DDG HTML results wrap the real destination in a redirect link; unwrap it to the
    actual target so the result url is the real page (and the http filter works).

    Args:
        href: The raw ``href`` from a result anchor.

    Returns:
        The unwrapped destination URL, or the input when there is nothing to unwrap.
    """
    if "uddg=" not in href:
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return f"https:{href}"
        return href
    try:
        query = urlparse(href if href.startswith("http") else f"https:{href}").query
        target = parse_qs(query).get("uddg", [""])[0]
        return target or href
    except (ValueError, AttributeError):
        return href


def default_page_fetcher(url: str) -> str:
    """Fetch a URL's raw text via the Python STDLIB (the live network boundary).

    This is the ONE place a real request happens. It uses ``urllib.request`` (NO new
    pip dependency, NO API key, NO secret header — only a plain User-Agent and the
    public query in the URL). Never raises: any network/HTTP/decoding error returns
    ``""`` so the caller degrades to "no external signal" rather than crashing the
    digest (Rule 12). Tests inject a fake fetcher and never call this.

    Args:
        url: The fully-formed request URL (query already encoded by the caller).

    Returns:
        The response body decoded as UTF-8 (errors replaced), or ``""`` on any
        failure.

    Example:
        >>> default_page_fetcher("https://html.duckduckgo.com/html/?q=test")  # doctest: +SKIP
        '<html>...'
    """
    try:
        request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html"})
        with urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 (https URL, no user-controlled scheme)
            raw_bytes = response.read()
        return raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 (best-effort egress — never propagate)
        # Reason: a degraded external search must not crash the digest; the item just
        # keeps its default tag. No URL query is logged as secret — it is a public
        # topic string — but we still avoid echoing it to keep logs terse.
        log.log_warning(
            "keyless_fetch_failed",
            error_type=type(exc).__name__,
            fix_suggestion="External search is best-effort; check network connectivity if corroboration tags look off.",
        )
        return ""


def _parse_ddg_html(page_html: str, count: int) -> list[SearchResult]:
    """Parse DuckDuckGo HTML into up to ``count`` :class:`SearchResult`s.

    Associates each result's snippet by POSITION (the window between this anchor and
    the next), not by a parallel index — some anchors (video/news modules) carry no
    snippet, so a global zip would shift every later snippet onto the wrong result.

    Args:
        page_html: The raw DDG HTML page (possibly empty).
        count: Maximum number of results to return.

    Returns:
        Up to ``count`` parsed results (http/https targets only). ``[]`` on empty
        input or no matches.
    """
    if not page_html:
        return []
    results: list[SearchResult] = []
    matches = list(_RESULT_A_RE.finditer(page_html))
    for index, match in enumerate(matches):
        if len(results) >= count:
            break
        target = _unwrap_ddg_redirect(match.group("href"))
        if not target.startswith("http"):
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(page_html)
        window = page_html[match.end():next_start]
        snippet_match = _SNIPPET_RE.search(window)
        snippet = _strip_html(snippet_match.group("snippet")) if snippet_match else ""
        title = _strip_html(match.group("title"))
        results.append(SearchResult(result_title=title, result_url=target, result_snippet=snippet))
    return results


def keyless_search(
    query: str,
    *,
    count: int = 5,
    page_fetcher: PageFetcher = default_page_fetcher,
) -> list[SearchResult]:
    """Run one keyless web search and return up to ``count`` results. Never raises.

    The high-level ``search_fn`` :func:`lib.external_trending.tag_external_corroboration`
    injects. Builds the keyless DDG query URL, pulls the HTML through the injected
    ``page_fetcher`` (the network boundary — stdlib by default, faked in tests), and
    parses it. KEYLESS: no API key, no secret; the only thing sent out is the public
    ``query`` string. Returns ``[]`` on an empty/blank query or any failure (Rule 12
    — a degraded external search degrades the tag, never the digest).

    Args:
        query: The public topic/title string to search for. Blank/whitespace -> ``[]``.
        count: Maximum number of results to return (the corroboration count is
            naturally capped here, so a viral topic and a mega-viral one both read as
            "corroborated" without an unbounded parse).
        page_fetcher: The injectable network boundary ``(url) -> html``. Defaults to
            :func:`default_page_fetcher` (stdlib). Tests inject a fake so NO live web
            call is made.

    Returns:
        Up to ``count`` :class:`SearchResult`s, or ``[]`` on blank query / fetch
        failure / no matches.

    Example:
        >>> fake = lambda url: '<a class="result__a" href="https://x.test">Hit</a>'
        >>> keyless_search("orbit digest", count=3, page_fetcher=fake)
        [SearchResult(result_title='Hit', result_url='https://x.test', result_snippet='')]
    """
    safe_query = (query or "").strip()
    if not safe_query:
        # Reason: a blank query is a no-op, not an error — never send an empty search.
        log.log_debug("keyless_search_skipped_blank_query")
        return []
    if count <= 0:
        return []
    url = f"{_DDG_HTML_URL}?{urlencode({'q': safe_query})}"
    try:
        page_html = page_fetcher(url)
    except Exception as exc:  # noqa: BLE001 (an injected fetcher misbehaving must not crash the digest)
        log.log_warning(
            "keyless_search_fetcher_raised",
            error_type=type(exc).__name__,
            fix_suggestion="The injected page_fetcher raised; it should return '' on failure, not raise.",
        )
        return []
    results = _parse_ddg_html(page_html, count)
    log.log_debug("keyless_search_completed", result_count=len(results), requested_count=count)
    return results
