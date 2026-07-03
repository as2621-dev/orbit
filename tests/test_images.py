"""DoD tests for build-time image inlining (Phase 7 / Sub-phase 1).

Per Rule 9, each test encodes WHY the behavior matters, not merely what it does. The
image layer makes the digest self-contained (base64-inlined thumbnails/avatars, no CDN
at open) AND must fail soft — an image is decorative, never load-bearing, so a flaky
host can never break the whole digest. The tests assert exactly those intents:

  1. Happy path: a real image response → a ``data:image/jpeg;base64,...`` URI (the
     inlining the self-contained digest depends on).
  2. Fail-soft on a 404/timeout → ``None`` + an ``image_inline_failed`` log, NOT a
     crash (a dead image host must degrade to the placeholder, not abort the build).
  3. Fail-soft on a non-image content-type (an HTML error page) and on an oversize body
     → ``None`` (an HTML payload in an <img> would be a stored-XSS vector; an oversize
     image would bloat the self-contained HTML).
  4. Disk cache: a second inline of the SAME URL re-uses the cached URI WITHOUT a
     second network fetch (re-runs must be free — DoD).
  5. ``safe_img_src`` allows a base64 image data URI but rejects ``data:text/html`` and
     ``javascript:`` to ``""`` (the <img src> sink must never carry a script payload).
  6. The ``derive_*`` URL builders produce the EXACT documented URLs, and a
     ``RankableItem`` round-trip sets the right image_url per source.

NO test hits the network — ``urllib.request.urlopen`` is mocked everywhere, and the
disk cache is redirected to a tmp dir via ``XDG_CACHE_HOME`` so the real cache is never
touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Make ``scripts`` importable so ``from lib import ...`` resolves regardless of the
# working directory. Mirrors tests/test_rerank.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import images  # noqa: E402
from lib.html_render import safe_img_src  # noqa: E402
from lib.rerank import RankableItem  # noqa: E402
from lib.youtube_yt import Upload  # noqa: E402


class _FakeResponse:
    """A minimal stand-in for the urllib response context manager used by fetch_and_inline."""

    def __init__(self, *, content_type: str, body: bytes) -> None:
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


def _patch_urlopen(*, content_type: str, body: bytes) -> MagicMock:
    """Build a urlopen mock that returns one fresh _FakeResponse per call."""
    mock_urlopen = MagicMock(side_effect=lambda *a, **k: _FakeResponse(content_type=content_type, body=body))
    return mock_urlopen


# --- derive_* URL builders (pure, exact-format) ------------------------------


def test_derive_youtube_thumb_url_exact_format() -> None:
    """The thumbnail URL must be the exact i.ytimg mqdefault form the tile layout fetches.

    WHY: the renderer fetches THIS exact URL; a drift (wrong host/size) would silently
    break every YouTube thumbnail or fetch the wrong resolution.
    """
    assert images.derive_youtube_thumb_url("dQw4w9WgXcQ") == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"


def test_derive_avatar_url_strips_leading_at() -> None:
    """The avatar URL must be the unavatar form, with a leading @ stripped.

    WHY: handles persist with or without a leading @; both must resolve to the SAME
    avatar URL or a tweet tile would 404 its profile pic.
    """
    assert images.derive_avatar_url("@alice") == "https://unavatar.io/twitter/alice"
    assert images.derive_avatar_url("bob") == "https://unavatar.io/twitter/bob"


# --- fetch_and_inline: happy path + fail-soft + cache ------------------------


def test_fetch_and_inline_happy_path_returns_data_uri(tmp_path, monkeypatch) -> None:
    """A real image response inlines to a data:image/jpeg;base64 URI (self-contained digest).

    WHY: the whole self-contained design rests on this — the image must come back as a
    base64 data URI carrying the right mime, never a remote link.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mock_urlopen = _patch_urlopen(content_type="image/jpeg", body=b"\xff\xd8\xff\xe0jpegbytes")

    with patch("lib.images.urllib.request.urlopen", mock_urlopen):
        result = images.fetch_and_inline("https://i.ytimg.com/vi/abc/mqdefault.jpg")

    assert result is not None
    assert result.startswith("data:image/jpeg;base64,")
    mock_urlopen.assert_called_once()


def test_fetch_and_inline_404_returns_none_and_logs(tmp_path, monkeypatch, capsys) -> None:
    """A network error (404/timeout) yields None + an image_inline_failed log, never a crash.

    WHY: a dead image host must degrade to the placeholder, not abort the digest build
    (Rule 12 inverted — images are decorative). We assert the soft return AND the
    structured error event so the failure is observable.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mock_urlopen = MagicMock(side_effect=TimeoutError("connection timed out"))

    with patch("lib.images.urllib.request.urlopen", mock_urlopen):
        result = images.fetch_and_inline("https://i.ytimg.com/vi/dead/mqdefault.jpg")

    assert result is None
    assert "image_inline_failed" in capsys.readouterr().out


def test_fetch_and_inline_non_image_content_type_returns_none(tmp_path, monkeypatch, capsys) -> None:
    """An HTML error page (text/html) is rejected to None (never inlined into an <img>).

    WHY: a non-image body in a data: URI under an <img src> is a stored-XSS / corruption
    vector — the content-type sniff must reject anything that is not image/*.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mock_urlopen = _patch_urlopen(content_type="text/html", body=b"<html>404 not found</html>")

    with patch("lib.images.urllib.request.urlopen", mock_urlopen):
        result = images.fetch_and_inline("https://unavatar.io/twitter/ghost")

    assert result is None
    assert "image_inline_failed" in capsys.readouterr().out


def test_fetch_and_inline_oversize_returns_none(tmp_path, monkeypatch, capsys) -> None:
    """An image over the size cap is rejected to None (keeps the self-contained HTML small).

    WHY: the digest inlines ~14 images; a runaway multi-MB image would bloat the file,
    so the byte cap must reject it rather than embed it.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    oversize_body = b"\xff\xd8" + b"x" * 50
    mock_urlopen = _patch_urlopen(content_type="image/jpeg", body=oversize_body)

    with patch("lib.images.urllib.request.urlopen", mock_urlopen):
        result = images.fetch_and_inline("https://i.ytimg.com/vi/big/mqdefault.jpg", max_bytes=10)

    assert result is None
    assert "image_inline_failed" in capsys.readouterr().out


def test_fetch_and_inline_second_call_hits_disk_cache(tmp_path, monkeypatch) -> None:
    """A re-fetch of the same URL reads the disk cache — the network is hit only ONCE.

    WHY: re-runs (the digest is rebuilt daily) must not re-download every image. The
    second call must return the SAME URI from cache with the underlying urlopen mock
    NOT called again — that is the cache's whole purpose (DoD).
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mock_urlopen = _patch_urlopen(content_type="image/jpeg", body=b"\xff\xd8cached")
    url = "https://i.ytimg.com/vi/cacheme/mqdefault.jpg"

    with patch("lib.images.urllib.request.urlopen", mock_urlopen):
        first = images.fetch_and_inline(url)
        second = images.fetch_and_inline(url)

    assert first is not None
    assert first == second
    mock_urlopen.assert_called_once()


# --- safe_img_src allowlist (the <img src> sink) -----------------------------


def test_safe_img_src_allows_base64_image_data_uri() -> None:
    """A base64 image data URI passes through (the inlined thumbnails/avatars must render)."""
    assert safe_img_src("data:image/png;base64,x") == "data:image/png;base64,x"


def test_safe_img_src_allows_remote_https_url() -> None:
    """A plain https URL passes (the renderer may also pass a remote src as fallback)."""
    url = "https://i.ytimg.com/vi/abc/mqdefault.jpg"
    assert safe_img_src(url) == url


def test_safe_img_src_rejects_data_text_html() -> None:
    """data:text/html is rejected to '' — an HTML payload in an <img src> is stored XSS."""
    assert safe_img_src("data:text/html,<script>alert(1)</script>") == ""


def test_safe_img_src_rejects_javascript_scheme() -> None:
    """javascript: is rejected to '' — it must never reach the src sink."""
    assert safe_img_src("javascript:alert(1)") == ""


# --- RankableItem round-trip (image_url wired per source) --------------------


def test_from_parts_sets_youtube_thumb_image_url() -> None:
    """from_parts derives the ytimg thumbnail from the video id (YouTube tile thumbnail).

    WHY: the YouTube tile thumbnail comes from RankableItem.image_url; a regression that
    left it empty (or used the wrong URL) would drop every YouTube thumbnail to the
    placeholder.
    """
    upload = Upload(
        video_id="vid123",
        title="A talk",
        description="",
        upload_date="20260101",
        view_count=1000,
        like_count=50,
        comment_count=5,
        duration=1800,
        channel_name="Some Channel",
    )

    item = RankableItem.from_parts(upload, None, [], creator_external_id="UC1")

    assert item.image_url == "https://i.ytimg.com/vi/vid123/mqdefault.jpg"
    assert item.summary == ""


def test_from_tweet_sets_unavatar_image_url() -> None:
    """from_tweet derives the unavatar avatar from the handle (the tweet-tile avatar extension).

    WHY: tweet tiles carry the author's avatar (a deliberate design extension); it is
    sourced from RankableItem.image_url derived from the handle.
    """
    tweet = SimpleNamespace(
        tweet_id="123",
        text="a sharp take",
        handle="alice",
        created_at="2026-06-18T00:00:00Z",
        like_count=10,
        retweet_count=5,
        reply_count=2,
        quote_count=1,
    )

    item = RankableItem.from_tweet(tweet)

    assert item.image_url == "https://unavatar.io/twitter/alice"
