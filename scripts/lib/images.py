"""Build-time image fetching + base64 inlining for the Orbit Tiles digest (Phase 7).

The digest is fully self-contained — opened straight off disk it must render with NO
CDN fetch at view time (design brief §image decisions). So every image (YouTube
thumbnail, X profile avatar) is fetched ONCE on the run machine at build time,
base64-encoded into a ``data:`` URI, and embedded directly in the HTML.

This module is the build-time fetch layer:

  * :func:`derive_youtube_thumb_url` / :func:`derive_avatar_url` — pure URL builders
    (no network) that turn a ``video_id`` / ``handle`` into the source image URL.
  * :func:`fetch_and_inline` — fetches a URL via the stdlib ``urllib`` (this project
    deliberately ships ZERO third-party HTTP deps), validates it is a real image
    under the size cap, base64-encodes it to a ``data:`` URI, and **disk-caches** the
    result keyed by a hash of the URL so re-runs never re-fetch.

Rule 12 (fail loud everywhere EXCEPT here): an image is decorative, never load-bearing
— a missing thumbnail must degrade to the hatched ``.ph`` placeholder, NEVER crash the
whole digest. So :func:`fetch_and_inline` is the one deliberately fail-SOFT surface:
ANY error (404, timeout, non-image content-type, oversize, malformed) returns ``None``
and logs ``image_inline_failed`` with a ``fix_suggestion``; no exception escapes.

Rule 5: no LLM here — pure deterministic fetch + encode.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import urllib.request
from pathlib import Path

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.images`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors rerank.py / html_render.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib import log  # noqa: E402  (import must follow the sys.path inserts above)

# --- Tunable constants -------------------------------------------------------

# Default per-image size cap. A digest inlines ~14 images; a runaway multi-MB image
# would bloat the self-contained HTML, so anything over the cap is rejected (fail
# soft). mqdefault YouTube thumbs (~10-20KB) and unavatar avatars sit well under this.
DEFAULT_MAX_IMAGE_BYTES: int = 150_000

# Network timeout (seconds) for a single image fetch. Kept short — a slow image must
# not stall the whole build; it just falls back to the placeholder.
FETCH_TIMEOUT_SECONDS: float = 10.0

# A browser-shaped User-Agent. Some image hosts (ytimg, unavatar) return 403 / an HTML
# error page to a bare ``Python-urllib`` agent, which would silently break inlining.
BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def derive_youtube_thumb_url(video_id: str) -> str:
    """Build the YouTube ``mqdefault`` thumbnail URL for a video id (pure, no network).

    Uses the ``i.ytimg.com`` static thumbnail host. ``mqdefault`` (320x180) is the
    sweet spot for the tile layout — sharp enough for a hero tile, small enough
    (~10-20KB) to inline many per digest.

    Args:
        video_id: The YouTube video id (the ``RankableItem.item_external_id`` for a
            YouTube upload).

    Returns:
        The fully-qualified thumbnail URL.

    Example:
        >>> derive_youtube_thumb_url("dQw4w9WgXcQ")
        'https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg'
    """
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def derive_avatar_url(handle: str) -> str:
    """Build the unavatar.io profile-picture URL for an X handle (pure, no network).

    unavatar.io resolves a Twitter/X handle to its current avatar without us needing
    the X API. A leading ``@`` is stripped so both ``@alice`` and ``alice`` resolve
    identically.

    Args:
        handle: The X account handle, with or without a leading ``@``.

    Returns:
        The unavatar.io avatar URL for the handle.

    Example:
        >>> derive_avatar_url("@alice")
        'https://unavatar.io/twitter/alice'
        >>> derive_avatar_url("bob")
        'https://unavatar.io/twitter/bob'
    """
    normalized_handle = handle.lstrip("@")
    return f"https://unavatar.io/twitter/{normalized_handle}"


def _cache_dir() -> Path:
    """Return the on-disk image-cache directory, creating it if needed.

    Honors ``XDG_CACHE_HOME`` (falling back to ``~/.cache``) so the cache lives in the
    user's standard cache location and survives across runs — a fetched thumbnail is
    re-used on every subsequent digest build, never re-downloaded.

    Returns:
        The ``<cache root>/orbit/images`` directory path (guaranteed to exist).
    """
    import os

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache_root = Path(xdg_cache_home) if xdg_cache_home else Path.home() / ".cache"
    image_cache_dir = cache_root / "orbit" / "images"
    image_cache_dir.mkdir(parents=True, exist_ok=True)
    return image_cache_dir


def _cache_path_for_url(url: str) -> Path:
    """Map a source URL to its deterministic cache-file path (hash of the URL).

    The filename is the SHA-256 hex digest of the URL so two different URLs never
    collide and the same URL always resolves to the same file (cache hit on re-run).
    The cached file stores the finished ``data:`` URI as UTF-8 text.

    Args:
        url: The source image URL.

    Returns:
        The cache-file path for this URL (the file may or may not exist yet).
    """
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{url_hash}.datauri"


def fetch_and_inline(url: str, *, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES) -> str | None:
    """Fetch an image URL and return it as a base64 ``data:`` URI, or ``None`` on any error.

    The build-time inlining primitive that makes the digest self-contained. On the
    first call for a URL it fetches over the network (browser User-Agent), validates
    the response is a real image (``Content-Type`` starts with ``image/``) within
    ``max_bytes``, base64-encodes the body into ``data:{mime};base64,{payload}``, and
    writes that URI to the disk cache. On any subsequent call for the SAME URL it
    returns the cached URI WITHOUT touching the network (re-runs are free).

    Fail-soft contract (Rule 12 inverted — an image is decorative, never load-bearing):
    ANY failure (404, connection error, timeout, non-image content-type, oversize body,
    malformed response) returns ``None`` and logs ``image_inline_failed`` with a
    ``fix_suggestion``. No exception ever escapes — the caller falls back to the hatched
    ``.ph`` placeholder so a flaky image host can never break the whole digest.

    Args:
        url: The source image URL (from :func:`derive_youtube_thumb_url` /
            :func:`derive_avatar_url`).
        max_bytes: Reject any image whose body exceeds this many bytes (default
            :data:`DEFAULT_MAX_IMAGE_BYTES`). Keeps the inlined HTML from bloating.

    Returns:
        A ``data:{mime};base64,...`` URI string, or ``None`` if the image could not be
        fetched/validated.

    Example:
        >>> uri = fetch_and_inline("https://i.ytimg.com/vi/abc/mqdefault.jpg")  # doctest: +SKIP
        >>> uri.startswith("data:image/")  # doctest: +SKIP
        True
    """
    # Cache check FIRST so a re-run never re-fetches (and tests assert the underlying
    # urllib mock is called exactly once across two calls).
    cache_path = _cache_path_for_url(url)
    try:
        if cache_path.exists():
            cached_uri = cache_path.read_text(encoding="utf-8")
            log.log_debug("image_inline_cache_hit", url=url, cache_path=str(cache_path))
            return cached_uri
    except OSError as cache_read_error:
        # Reason: a corrupt/unreadable cache entry must not abort the fetch — fall
        # through and re-fetch rather than failing the image.
        log.log_warning(
            "image_cache_read_failed",
            url=url,
            error_message=str(cache_read_error),
            fix_suggestion="Cache file unreadable; re-fetching. Delete it if this recurs.",
        )

    try:
        request = urllib.request.Request(url, headers={"User-Agent": BROWSER_USER_AGENT})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                log.log_error(
                    "image_inline_failed",
                    url=url,
                    reason="non_image_content_type",
                    content_type=content_type or "<missing>",
                    fix_suggestion=(
                        "URL did not return an image (likely a 404/HTML error page). "
                        "Verify the video id / handle is correct; falling back to placeholder."
                    ),
                )
                return None
            # Read one byte past the cap so an exactly-at-cap image still passes while
            # an oversize one is detected without buffering the whole giant body.
            image_bytes = response.read(max_bytes + 1)
    except Exception as fetch_error:  # noqa: BLE001 — fail soft on ANY error (Rule 12 inverted)
        log.log_error(
            "image_inline_failed",
            url=url,
            reason="fetch_error",
            error_type=type(fetch_error).__name__,
            error_message=str(fetch_error),
            fix_suggestion=(
                "Image fetch failed (timeout / connection / HTTP error). "
                "Check network connectivity; falling back to placeholder."
            ),
        )
        return None

    if len(image_bytes) > max_bytes:
        log.log_error(
            "image_inline_failed",
            url=url,
            reason="oversize",
            max_bytes=max_bytes,
            fix_suggestion=(
                f"Image exceeds the {max_bytes}-byte cap and was rejected to keep the "
                "self-contained HTML small; falling back to placeholder."
            ),
        )
        return None

    encoded_payload = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{content_type};base64,{encoded_payload}"

    try:
        cache_path.write_text(data_uri, encoding="utf-8")
    except OSError as cache_write_error:
        # Reason: a failed cache write is non-fatal — we still return the URI this run;
        # only the re-run optimization is lost.
        log.log_warning(
            "image_cache_write_failed",
            url=url,
            error_message=str(cache_write_error),
            fix_suggestion="Could not persist image cache; re-runs will re-fetch this image.",
        )

    log.log_debug("image_inline_succeeded", url=url, byte_count=len(image_bytes), mime=content_type)
    return data_uri
