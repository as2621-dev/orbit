"""The Final ledger digest renderer — ranked rows with three timestamped sections.

This is the Phase-9 replacement for the Tiles masonry (:mod:`lib.tiles`). The Final
design drops density tiering from the LAYOUT entirely: every YouTube item renders as the
same ranked row — rank, thumbnail, title, then its three ``<timestamp> <summary>``
section lines from :mod:`lib.sections`. Ranking still decides ORDER and inclusion; it no
longer decides a tile's size.

Two layouts, one data model:

  - :func:`render_web_document`    — the "Orbit - Final" desktop ledger (1080px, a
    140x79 thumb per row, channel/handle chip bars, three-column grid rows).
  - :func:`render_mobile_document` — the "1B" mobile bulletin (430px, rank + 77x44
    thumb + title/channel, sections on a red rail, no chip bars).

Both are single-scroll: the Final design has no page 2, so the Tiles spill/pagination
concept does not apply here. Both are self-contained — fonts arrive base64-inlined via
:func:`lib.html_render.wrap_page` (never the design's Google Fonts ``<link>``, which an
email client would strip) and images arrive as data URIs from :mod:`lib.images`.

Every user-controlled string goes through :func:`lib.html_render.escape`, and every URL
through :func:`lib.html_render.safe_href` / :func:`lib.html_render.safe_img_src`, so a
creator-controlled title or link can never inject markup (the same XSS posture as Tiles).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple, Sequence

# Make ``lib`` importable whether this module is imported as the package member
# ``lib.ledger`` (via orbit.py's sys.path insert of the scripts dir) or run from the
# scripts dir directly. Mirrors tiles.py / render.py's pattern.
_LIB_DIR = Path(__file__).parent.resolve()
_SCRIPTS_DIR = _LIB_DIR.parent.resolve()
for _candidate_dir in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_candidate_dir) not in sys.path:
        sys.path.insert(0, str(_candidate_dir))

from lib.html_render import escape, safe_href, safe_img_src, wrap_page  # noqa: E402

# --- Palette + type, lifted verbatim from the Final design -------------------------
# Named so a future tweak happens in ONE place rather than across ~40 inline styles.
_PAGE_BG: str = "#DCD4C2"  # the paper the sheet sits on
_SHEET_BG: str = "#EDE7DA"  # the digest sheet itself
_PANEL_BG: str = "#F7F3EA"  # stat bars / row hover
_INK: str = "#1F1B16"  # headline + rule ink
_INK_SOFT: str = "#2a251e"  # title text
_INK_BODY: str = "#4a4336"  # section summary text
_INK_MUTED: str = "#5a5240"  # section labels
_INK_FAINT: str = "#8a7f6c"  # rank / meta / footer
_ACCENT: str = "#B7472A"  # timestamps, chat button, arrows
_ON_ACCENT: str = "#F7F3EA"  # text on the accent block
_THUMB_BG: str = "#e3dccc"  # thumbnail placeholder
_RULE: str = "rgba(31,27,22,.25)"  # sheet border
_RULE_SOFT: str = "rgba(31,27,22,.2)"  # panel border / row divider
_RULE_FAINT: str = "rgba(31,27,22,.18)"  # hairline after a section label
_RULE_INNER: str = "rgba(31,27,22,.15)"  # stat-bar inner divider
_CHIP_BG: str = "rgba(31,27,22,.05)"
_CHIP_BORDER: str = "rgba(31,27,22,.12)"
_RAIL: str = "rgba(183,71,42,.35)"  # mobile section rail

_SERIF: str = "'Newsreader',serif"
_DISPLAY: str = "'Fraunces',serif"
_MONO: str = "'JetBrains Mono',monospace"

# The design's `style-hover=` attribute is a design-canvas affordance, not real HTML.
# These classes reproduce it in a single <style> block prepended to the body, so the
# page stays self-contained and an email client that ignores :hover degrades cleanly.
_HOVER_CSS: str = (
    "<style>"
    f".lg-row:hover{{background:{_PANEL_BG}}}"
    f".lg-pt:hover,.lg-post:hover{{color:{_ACCENT}}}"
    f".lg-chat:hover{{background:{_INK}}}"
    "</style>"
)


class LedgerPoint(NamedTuple):
    """One rendered section line: a timestamp that deep-links into the video.

    Attributes:
        timestamp_label: The display label (``"4:20"``).
        deep_link: The ``watch?v=ID&t=Ns`` URL.
        summary_text: What that stretch of the video covers.
    """

    timestamp_label: str
    deep_link: str
    summary_text: str


class LedgerVideo(NamedTuple):
    """One ranked YouTube row.

    Attributes:
        rank: 1-based position; rendered zero-padded (``01``).
        title: The video title.
        channel_name: The creator name (shown on mobile, not on the web row).
        card_url: The video permalink.
        thumb_src: A data-URI (or URL) thumbnail, or "" for the placeholder block.
        points: The video's section lines — three when sections resolved, possibly
            fewer or none when the transcript or the split was unavailable.
    """

    rank: int
    title: str
    channel_name: str
    card_url: str
    thumb_src: str
    points: Sequence[LedgerPoint]


class LedgerPost(NamedTuple):
    """One ranked X row.

    Attributes:
        rank: 1-based position; rendered zero-padded.
        handle: The author handle (``@name``), shown on mobile.
        excerpt: The post's first non-empty line, already trimmed to length.
        card_url: The x.com permalink.
        avatar_src: A data-URI (or URL) avatar, or "" for the placeholder circle.
    """

    rank: int
    handle: str
    excerpt: str
    card_url: str
    avatar_src: str


class LedgerChannel(NamedTuple):
    """One channel chip in the web YouTube stat bar.

    Attributes:
        monogram: Up to two initials rendered in the dark square.
        display_name: The shortened channel name.
        item_count: How many of the channel's items are in today's digest.
    """

    monogram: str
    display_name: str
    item_count: int


class LedgerHandle(NamedTuple):
    """One handle chip in the web X stat bar.

    Attributes:
        handle: The author handle (``@name``).
        item_count: How many of the handle's posts are in today's digest.
        avatar_src: A data-URI (or URL) avatar, or "" for the placeholder circle.
    """

    handle: str
    item_count: int
    avatar_src: str


class LedgerCounts(NamedTuple):
    """The masthead / footer tallies.

    Attributes:
        tracked_total: Every source Orbit watches.
        posted_count: How many of them posted into today's digest.
        item_count: Total items rendered (YouTube + X).
    """

    tracked_total: int
    posted_count: int
    item_count: int


def format_rank(rank: int) -> str:
    """Zero-pad a rank to at least two digits, as the design renders it.

    Args:
        rank: The 1-based rank.

    Returns:
        The padded string.

    Example:
        >>> format_rank(1), format_rank(12), format_rank(120)
        ('01', '12', '120')
    """
    return f"{max(0, int(rank)):02d}"


def channel_monogram(channel_name: str) -> str:
    """Derive a 1-2 letter monogram from a channel name (the design's dark square).

    Mirrors the design's ``mono()``: strip non-alphanumerics, take the first letter of
    the first two words. Falls back to ``?`` for a name with no usable characters.

    Args:
        channel_name: The raw channel name.

    Returns:
        The uppercase monogram.

    Example:
        >>> channel_monogram("AI Engineer")
        'AE'
        >>> channel_monogram("Fireship")
        'F'
        >>> channel_monogram("")
        '?'
    """
    cleaned = "".join(character if character.isalnum() else " " for character in channel_name)
    words = cleaned.split()
    if not words:
        return "?"
    monogram = words[0][0] + (words[1][0] if len(words) > 1 else "")
    return monogram.upper()


def shorten_channel_name(channel_name: str, max_chars: int = 26) -> str:
    """Shorten a channel name for its chip, as the design's ``nice()`` does.

    Takes the part before the first ``|`` (channels often suffix a tagline there) and
    ellipsises past ``max_chars``.

    Args:
        channel_name: The raw channel name.
        max_chars: The length past which the name is ellipsised.

    Returns:
        The shortened name.

    Example:
        >>> shorten_channel_name("AI NEWS & STRATEGY DAILY | NATE B JONES")
        'AI NEWS & STRATEGY DAILY'
    """
    head = channel_name.split("|")[0].strip()
    if len(head) > max_chars:
        return head[: max_chars - 2] + "…"
    return head


def _thumb_img(thumb_src: str, width: int, height: int) -> str:
    """Render a fixed-size cover image, or "" so the caller's placeholder block shows.

    Args:
        thumb_src: The image data URI / URL, or "".
        width: Rendered width in px.
        height: Rendered height in px.

    Returns:
        The ``<img>`` markup, or "".
    """
    if not thumb_src:
        return ""
    return (
        f'<img src="{safe_img_src(thumb_src)}" alt="" '
        f'style="width:{width}px;height:{height}px;object-fit:cover;display:block;">'
    )


def _avatar_img(avatar_src: str, size: int) -> str:
    """Render a round avatar at ``size`` px, or "" for the placeholder circle.

    Args:
        avatar_src: The image data URI / URL, or "".
        size: Rendered diameter in px.

    Returns:
        The ``<img>`` markup, or "".
    """
    if not avatar_src:
        return ""
    return (
        f'<img src="{safe_img_src(avatar_src)}" alt="" '
        f'style="width:{size}px;height:{size}px;object-fit:cover;display:block;">'
    )


def _chat_button(chat_href: str, video_count: int, post_count: int, *, compact: bool) -> str:
    """Render the accent "CHAT ABOUT TODAY" block.

    Args:
        chat_href: The claude.ai prefilled-prompt URL. Empty renders nothing.
        video_count: Videos in today's digest (named in the sub-label).
        post_count: X posts in today's digest (named in the sub-label).
        compact: True for the mobile masthead (no sub-label, full-width block).

    Returns:
        The anchor markup, or "" when there is no link.
    """
    if not chat_href:
        return ""
    if compact:
        return (
            f'<a class="lg-chat" href="{safe_href(chat_href)}" '
            f'style="display:block;margin-top:12px;background:{_ACCENT};color:{_ON_ACCENT};'
            f'padding:11px 16px 10px;box-shadow:3px 3px 0 {_INK};">'
            f'<span style="display:block;font-family:{_MONO};font-size:12px;font-weight:700;'
            f'letter-spacing:.08em;">✳ CHAT ABOUT TODAY →</span></a>'
        )
    return (
        f'<a class="lg-chat" href="{safe_href(chat_href)}" '
        f'style="flex:none;display:block;background:{_ACCENT};color:{_ON_ACCENT};'
        f'padding:12px 20px 11px;box-shadow:3px 3px 0 {_INK};">'
        f'<span style="display:block;font-family:{_MONO};font-size:12px;font-weight:700;'
        f'letter-spacing:.08em;">✳ CHAT ABOUT TODAY →</span>'
        f'<span style="display:block;font-family:{_MONO};font-size:8.5px;letter-spacing:.06em;'
        f'opacity:.75;margin-top:3px;">ASK CLAUDE ABOUT ALL {video_count} VIDEOS &amp; '
        f"{post_count} POSTS</span></a>"
    )


def _section_label(label_text: str) -> str:
    """Render a section label followed by its hairline rule (web layout).

    Args:
        label_text: E.g. ``"From your YouTube · ranked"``.

    Returns:
        The label row markup.
    """
    return (
        f'<div style="display:flex;align-items:center;gap:12px;margin:26px 0 0;">'
        f'<div style="font-family:{_MONO};font-size:11px;letter-spacing:.18em;color:{_INK_MUTED};'
        f'text-transform:uppercase;">{escape(label_text)}</div>'
        f'<div style="flex:1;height:1px;background:{_RULE_FAINT};"></div></div>'
    )


def _stat_pair(count: int, label_html: str) -> str:
    """Render one big-numeral + label pair for a web stat bar.

    Args:
        count: The numeral.
        label_html: The two-line label markup (already safe; contains a ``<br>``).

    Returns:
        The pair markup.
    """
    return (
        f'<div><div style="font-family:{_DISPLAY};font-weight:900;font-size:30px;line-height:1;">'
        f"{count}</div>"
        f'<div style="font-family:{_MONO};font-size:9px;letter-spacing:.1em;color:{_INK_FAINT};'
        f'margin-top:4px;">{label_html}</div></div>'
    )


def _web_stat_bar(stat_pairs_html: str, chips_html: str) -> str:
    """Assemble a web stat bar: big numerals on the left, chips filling the right.

    Args:
        stat_pairs_html: The concatenated :func:`_stat_pair` blocks.
        chips_html: The concatenated chip spans.

    Returns:
        The stat bar markup.
    """
    return (
        f'<div style="display:flex;gap:0;border:1px solid {_RULE_SOFT};background:{_PANEL_BG};'
        f'margin-top:14px;">'
        f'<div style="flex:none;display:flex;gap:26px;padding:16px 22px;'
        f'border-right:1px solid {_RULE_INNER};">{stat_pairs_html}</div>'
        f'<div style="flex:1;padding:12px 16px;display:flex;flex-wrap:wrap;gap:5px;'
        f'align-content:flex-start;">{chips_html}</div></div>'
    )


def _chip_shell(badge_html: str, label_text: str, item_count: int) -> str:
    """Render one chip: a leading badge, a name, and a red ``×N`` count.

    Args:
        badge_html: The monogram square or avatar circle markup.
        label_text: The channel / handle name.
        item_count: The item count for the ``×N``.

    Returns:
        The chip markup.
    """
    return (
        f'<span style="display:inline-flex;align-items:center;gap:6px;font-family:{_MONO};'
        f'font-size:9.5px;color:{_INK_SOFT};background:{_CHIP_BG};border:1px solid {_CHIP_BORDER};'
        f'padding:3px 8px 3px 3px;">{badge_html}{escape(label_text)} '
        f'<b style="color:{_ACCENT};">×{item_count}</b></span>'
    )


def render_channel_chip(channel: LedgerChannel) -> str:
    """Render a YouTube channel chip (dark monogram square + name + count).

    Args:
        channel: The channel view model.

    Returns:
        The chip markup.

    Example:
        >>> "×3" in render_channel_chip(LedgerChannel("AE", "AI Engineer", 3))
        True
    """
    badge_html = (
        f'<span style="width:16px;height:16px;background:{_INK};color:{_ON_ACCENT};'
        f'display:inline-flex;align-items:center;justify-content:center;font-size:8px;'
        f'font-weight:700;">{escape(channel.monogram)}</span>'
    )
    return _chip_shell(badge_html, channel.display_name, channel.item_count)


def render_handle_chip(handle: LedgerHandle) -> str:
    """Render an X handle chip (round avatar + handle + count).

    Args:
        handle: The handle view model.

    Returns:
        The chip markup.
    """
    badge_html = (
        f'<span style="display:inline-block;width:16px;height:16px;border-radius:50%;'
        f'background:{_THUMB_BG};overflow:hidden;">{_avatar_img(handle.avatar_src, 16)}</span>'
    )
    return _chip_shell(badge_html, handle.handle, handle.item_count)


def render_web_masthead(dateline: str, counts: LedgerCounts, chat_href: str, video_count: int, post_count: int) -> str:
    """Render the web masthead: wordmark, tagline, tallies, and the chat block.

    Args:
        dateline: The formatted date line (``"SUNDAY · 19 JUL 2026"``).
        counts: The tracked / posted / item tallies.
        chat_href: The claude.ai chat URL ("" renders no button).
        video_count: Videos in today's digest.
        post_count: X posts in today's digest.

    Returns:
        The masthead markup.
    """
    return (
        f'<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:24px;'
        f'padding:30px 0 14px;border-bottom:3px double {_INK};">'
        f'<div><div style="font-family:{_DISPLAY};font-weight:900;font-size:44px;line-height:.9;'
        f'letter-spacing:-.02em;">/Orbit</div>'
        f'<div style="font-family:{_MONO};font-size:10.5px;letter-spacing:.18em;color:{_INK_FAINT};'
        f'text-transform:uppercase;margin-top:6px;">EVERYTHING YOU FOLLOW - DISTILLED</div></div>'
        f'<div style="flex:1;display:flex;justify-content:flex-end;align-items:flex-end;gap:22px;">'
        f'<div style="text-align:right;font-family:{_MONO};font-size:10.5px;letter-spacing:.07em;'
        f'color:{_INK_MUTED};line-height:1.7;"><div>{escape(dateline)}</div>'
        f'<div style="color:{_INK};font-weight:700;">{counts.tracked_total} TRACKED · '
        f"{counts.posted_count} POSTED · {counts.item_count} ITEMS</div></div>"
        f"{_chat_button(chat_href, video_count, post_count, compact=False)}</div></div>"
    )


def render_web_video_row(video: LedgerVideo) -> str:
    """Render one web YouTube row: rank, 140x79 thumb, title, and its section lines.

    Args:
        video: The video view model.

    Returns:
        The row markup.
    """
    point_lines = "".join(
        f'<a class="lg-pt" href="{safe_href(point.deep_link)}" '
        f'style="display:flex;gap:9px;align-items:baseline;min-width:0;">'
        f'<span style="flex:none;width:36px;text-align:right;font-family:{_MONO};font-size:9px;'
        f'font-weight:700;color:{_ACCENT};">{escape(point.timestamp_label)}</span>'
        f'<span style="flex:1;min-width:0;font-size:12px;line-height:1.45;color:{_INK_BODY};">'
        f"{escape(point.summary_text)}</span></a>"
        for point in video.points
    )
    return (
        f'<div class="lg-row" style="display:grid;grid-template-columns:30px 140px 1fr;gap:12px;'
        f'align-items:start;padding:9px 4px;border-bottom:1px dotted {_RULE_SOFT};">'
        f'<span style="font-family:{_MONO};font-size:10px;color:{_INK_FAINT};padding-top:2px;">'
        f"{format_rank(video.rank)}</span>"
        f'<a href="{safe_href(video.card_url)}" style="display:block;width:140px;height:79px;'
        f'background:{_THUMB_BG};">{_thumb_img(video.thumb_src, 140, 79)}</a>'
        f'<span style="min-width:0;display:block;">'
        f'<span style="display:flex;align-items:baseline;gap:10px;">'
        f'<a href="{safe_href(video.card_url)}" style="flex:1;min-width:0;font-size:14.5px;'
        f'font-weight:600;line-height:1.2;color:{_INK_SOFT};overflow:hidden;white-space:nowrap;'
        f'text-overflow:ellipsis;">{escape(video.title)}</a></span>'
        f'<span style="display:flex;flex-direction:column;gap:3px;margin-top:6px;">'
        f"{point_lines}</span></span></div>"
    )


def render_web_post_row(post: LedgerPost) -> str:
    """Render one web X row: rank, avatar, single-line excerpt, arrow.

    Args:
        post: The post view model.

    Returns:
        The row markup.
    """
    return (
        f'<a class="lg-post lg-row" href="{safe_href(post.card_url)}" '
        f'style="display:grid;grid-template-columns:30px 24px 1fr 20px;gap:12px;align-items:center;'
        f'padding:7px 4px;border-bottom:1px dotted {_RULE_SOFT};">'
        f'<span style="font-family:{_MONO};font-size:10px;color:{_INK_FAINT};">'
        f"{format_rank(post.rank)}</span>"
        f'<span style="display:block;width:22px;height:22px;border-radius:50%;background:{_THUMB_BG};'
        f'overflow:hidden;">{_avatar_img(post.avatar_src, 22)}</span>'
        f'<span style="font-size:14px;line-height:1.3;color:{_INK_SOFT};overflow:hidden;'
        f'white-space:nowrap;text-overflow:ellipsis;">{escape(post.excerpt)}</span>'
        f'<span style="font-family:{_MONO};font-size:11px;color:{_ACCENT};">→</span></a>'
    )


def _footer(counts: LedgerCounts, *, centered: bool) -> str:
    """Render the closing accounted-for line under a double rule.

    Args:
        counts: The tallies quoted in the line.
        centered: True for the mobile layout (centered, two lines).

    Returns:
        The footer markup.
    """
    if centered:
        return (
            f'<div style="margin-top:20px;padding-top:10px;border-top:3px double {_INK};'
            f'font-family:{_MONO};font-size:8.5px;letter-spacing:.05em;color:{_INK_FAINT};'
            f'line-height:1.6;text-align:center;">{counts.item_count} ITEMS FROM '
            f"{counts.posted_count} OF {counts.tracked_total} TRACKED CHANNELS<br>"
            f"EVERY POST READ &amp; RANKED</div>"
        )
    return (
        f'<div style="margin-top:24px;padding-top:12px;border-top:3px double {_INK};'
        f'font-family:{_MONO};font-size:10px;letter-spacing:.06em;color:{_INK_FAINT};">'
        f"{counts.item_count} ITEMS FROM {counts.posted_count} OF {counts.tracked_total} "
        f"TRACKED CHANNELS — EVERY POST READ &amp; RANKED</div>"
    )


def render_web_document(
    *,
    dateline: str,
    counts: LedgerCounts,
    videos: Sequence[LedgerVideo],
    channels: Sequence[LedgerChannel],
    posts: Sequence[LedgerPost],
    handles: Sequence[LedgerHandle],
    chat_href: str = "",
    title: str = "Orbit",
) -> str:
    """Render the complete "Orbit - Final" web ledger as a self-contained page.

    Args:
        dateline: The formatted date line.
        counts: The tracked / posted / item tallies.
        videos: The ranked YouTube rows, in render order.
        channels: The channel chips for the YouTube stat bar.
        posts: The ranked X rows, in render order.
        handles: The handle chips for the X stat bar.
        chat_href: The claude.ai chat URL ("" renders no button).
        title: The page ``<title>``.

    Returns:
        The complete ``<!DOCTYPE html>...`` page, fonts inlined.

    Example:
        >>> page = render_web_document(  # doctest: +SKIP
        ...     dateline="SUNDAY · 19 JUL 2026",
        ...     counts=LedgerCounts(802, 25, 43),
        ...     videos=[], channels=[], posts=[], handles=[],
        ... )
        >>> page.startswith("<!DOCTYPE html>")  # doctest: +SKIP
        True
    """
    youtube_stats = _stat_pair(len(channels), "CHANNELS<br>POSTED") + _stat_pair(
        len(videos), "VIDEOS<br>ANALYZED"
    )
    x_stats = _stat_pair(len(handles), "HANDLES<br>POSTED") + _stat_pair(len(posts), "ORIGINAL<br>POSTS")

    body_html = (
        f"{_HOVER_CSS}"
        f'<div style="min-height:100vh;padding:40px 24px 60px;font-family:{_SERIF};color:{_INK};'
        f'background:{_PAGE_BG};">'
        f'<div style="max-width:1080px;margin:0 auto;background:{_SHEET_BG};'
        f'border:1px solid {_RULE};padding:0 36px 44px;">'
        f"{render_web_masthead(dateline, counts, chat_href, len(videos), len(posts))}"
        f"{_section_label('From your YouTube · ranked')}"
        f"{_web_stat_bar(youtube_stats, ''.join(render_channel_chip(c) for c in channels))}"
        f'<div style="margin-top:16px;border-top:2px solid {_INK};">'
        f"{''.join(render_web_video_row(v) for v in videos)}</div>"
        f"{_section_label('From your X · originals only')}"
        f"{_web_stat_bar(x_stats, ''.join(render_handle_chip(h) for h in handles))}"
        f'<div style="margin-top:16px;border-top:2px solid {_INK};">'
        f"{''.join(render_web_post_row(p) for p in posts)}</div>"
        f"{_footer(counts, centered=False)}"
        f"</div></div>"
    )
    return wrap_page(title, body_html)


def render_mobile_masthead(dateline: str, counts: LedgerCounts, chat_href: str) -> str:
    """Render the 1B mobile masthead: centered wordmark, one-line tallies, chat block.

    Args:
        dateline: The formatted date line.
        counts: The tracked / posted / item tallies.
        chat_href: The claude.ai chat URL ("" renders no button).

    Returns:
        The masthead markup.
    """
    return (
        f'<div style="padding:22px 0 14px;border-bottom:3px double {_INK};text-align:center;">'
        f'<div style="font-family:{_DISPLAY};font-weight:900;font-size:40px;line-height:.9;'
        f'letter-spacing:-.02em;">/Orbit</div>'
        f'<div style="font-family:{_MONO};font-size:8.5px;letter-spacing:.18em;color:{_INK_FAINT};'
        f'text-transform:uppercase;margin-top:6px;">EVERYTHING YOU FOLLOW - DISTILLED</div>'
        f'<div style="font-family:{_MONO};font-size:9px;letter-spacing:.06em;color:{_INK_MUTED};'
        f'margin-top:8px;">{escape(dateline)} · <b style="color:{_INK};">{counts.tracked_total} '
        f"TRACKED · {counts.posted_count} POSTED · {counts.item_count} ITEMS</b></div>"
        f"{_chat_button(chat_href, 0, 0, compact=True)}</div>"
    )


def _mobile_section_label(label_text: str, right_html: str) -> str:
    """Render a mobile section label with its right-aligned counts.

    Args:
        label_text: E.g. ``"Your YouTube · ranked"``.
        right_html: The pre-built counts markup on the right.

    Returns:
        The label row markup.
    """
    return (
        f'<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;'
        f'margin:18px 0 0;">'
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:.16em;color:{_INK_MUTED};'
        f'text-transform:uppercase;">{escape(label_text)}</div>'
        f'<div style="font-family:{_MONO};font-size:9px;color:{_INK_FAINT};">{right_html}</div></div>'
    )


def render_mobile_video_row(video: LedgerVideo) -> str:
    """Render one 1B mobile row: rank, 77x44 thumb, title/channel, railed section lines.

    Args:
        video: The video view model.

    Returns:
        The row markup.
    """
    point_lines = "".join(
        f'<a class="lg-pt" href="{safe_href(point.deep_link)}" '
        f'style="display:block;padding:3px 0;font-size:13.5px;line-height:1.45;color:{_INK_BODY};">'
        f'<b style="font-family:{_MONO};font-size:10px;font-weight:700;color:{_ACCENT};">'
        f"{escape(point.timestamp_label)}</b> — {escape(point.summary_text)}</a>"
        for point in video.points
    )
    return (
        f'<div style="padding:12px 0;border-bottom:1px dotted {_RULE_SOFT};">'
        f'<a href="{safe_href(video.card_url)}" style="display:flex;gap:10px;align-items:flex-start;">'
        f'<span style="flex:none;font-family:{_DISPLAY};font-weight:900;font-size:19px;'
        f'color:{_ACCENT};line-height:1;margin-top:2px;">{format_rank(video.rank)}</span>'
        f'<span style="flex:none;display:block;width:77px;height:44px;background:{_THUMB_BG};">'
        f"{_thumb_img(video.thumb_src, 77, 44)}</span>"
        f'<span style="flex:1;min-width:0;display:block;">'
        f'<span style="display:block;font-size:15.5px;font-weight:600;line-height:1.25;'
        f'color:{_INK_SOFT};">{escape(video.title)}</span>'
        f'<span style="display:block;font-family:{_MONO};font-size:8.5px;letter-spacing:.05em;'
        f'color:{_INK_FAINT};text-transform:uppercase;margin-top:3px;">'
        f"{escape(video.channel_name)}</span></span></a>"
        f'<span style="display:block;margin-top:8px;border-left:2px solid {_RAIL};'
        f'padding-left:11px;margin-left:4px;">{point_lines}</span></div>'
    )


def render_mobile_post_row(post: LedgerPost) -> str:
    """Render one 1B mobile X row: rank, handle, and a wrapping excerpt.

    Args:
        post: The post view model.

    Returns:
        The row markup.
    """
    return (
        f'<a class="lg-post" href="{safe_href(post.card_url)}" '
        f'style="display:flex;gap:10px;align-items:baseline;padding:10px 0;'
        f'border-bottom:1px dotted {_RULE_SOFT};">'
        f'<span style="flex:none;font-family:{_DISPLAY};font-weight:900;font-size:16px;'
        f'color:{_ACCENT};line-height:1;">{format_rank(post.rank)}</span>'
        f'<span style="flex:1;min-width:0;display:block;">'
        f'<span style="display:block;font-family:{_MONO};font-size:9px;color:{_INK_FAINT};">'
        f"{escape(post.handle)}</span>"
        f'<span style="display:block;font-size:14px;line-height:1.4;color:{_INK_SOFT};'
        f'margin-top:2px;">{escape(post.excerpt)}</span></span></a>'
    )


def render_mobile_document(
    *,
    dateline: str,
    counts: LedgerCounts,
    videos: Sequence[LedgerVideo],
    channel_count: int,
    posts: Sequence[LedgerPost],
    handle_count: int,
    chat_href: str = "",
    title: str = "Orbit",
) -> str:
    """Render the complete "1B" mobile bulletin as a self-contained page.

    The 1B layout drops the chip bars, so channels and handles arrive as plain COUNTS
    rather than chip view models.

    Args:
        dateline: The formatted date line.
        counts: The tracked / posted / item tallies.
        videos: The ranked YouTube rows, in render order.
        channel_count: How many distinct channels posted (shown as ``N CH``).
        posts: The ranked X rows, in render order.
        handle_count: How many distinct handles posted.
        chat_href: The claude.ai chat URL ("" renders no button).
        title: The page ``<title>``.

    Returns:
        The complete ``<!DOCTYPE html>...`` page, fonts inlined.
    """
    youtube_counts = (
        f'<b style="color:{_INK};">{channel_count}</b> CH · '
        f'<b style="color:{_INK};">{len(videos)}</b> VIDEOS'
    )
    x_counts = (
        f'<b style="color:{_INK};">{handle_count}</b> HANDLES · '
        f'<b style="color:{_INK};">{len(posts)}</b> POSTS'
    )

    body_html = (
        f"{_HOVER_CSS}"
        f'<div style="min-height:100vh;padding:0;font-family:{_SERIF};color:{_INK};'
        f'background:{_PAGE_BG};">'
        f'<div style="max-width:430px;margin:0 auto;background:{_SHEET_BG};'
        f'border:1px solid {_RULE};padding:0 18px 32px;">'
        f"{render_mobile_masthead(dateline, counts, chat_href)}"
        f"{_mobile_section_label('Your YouTube · ranked', youtube_counts)}"
        f'<div style="margin-top:10px;border-top:2px solid {_INK};">'
        f"{''.join(render_mobile_video_row(v) for v in videos)}</div>"
        f"{_mobile_section_label('Your X · originals only', x_counts)}"
        f'<div style="margin-top:10px;border-top:2px solid {_INK};">'
        f"{''.join(render_mobile_post_row(p) for p in posts)}</div>"
        f"{_footer(counts, centered=True)}"
        f"</div></div>"
    )
    return wrap_page(title, body_html)
