"""Telegram public-channel web preview fetcher.

Reads the keyless ``t.me/s/{channel}`` page — the same HTML a browser gets
without logging in — for the channel's newest messages (about 20 per page).
This is how project/listing/maintenance announcements reach the agents before
they show up in news.

A fetch that fails is reported as ``<unavailable>``, never as "no messages":
the two are different claims, and passing a rate-limited page off as silence
hands the agents a signal that was never observed (same rule as ``reddit.py``).

No archive: the preview serves only the newest page, so a dated window is
withheld rather than filtered (TEXT_SOURCES.md §7). It is a direct-call module
like ``stocktwits.py`` — its input is a channel list, not a ticker, so it has no
place in a ticker-keyed router method.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_PREVIEW = "https://t.me/s/{channel}"
# Telegram serves the public preview to browser-like clients; a bare bot token
# gets the login wall instead.
_UA = "Mozilla/5.0 (compatible; tradingagents/0.2; +https://github.com/TauricResearch/TradingAgents)"

# Each message is one <div class="tgme_widget_message …" data-post="{channel}/{id}">;
# splitting on the marker yields that message's slice of the document.
_MSG = re.compile(r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"')
_TIME = re.compile(r'<time datetime="([^"]+)"')
_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")

_SCREEN_CHARS = 1000  # of a message's text sent for screening


def _strip_html(fragment: str) -> str:
    """Reduce a message's HTML body to plain text, keeping line breaks."""
    if not fragment:
        return ""
    text = _BR.sub("\n", fragment)
    text = _TAG.sub(" ", text)
    return "\n".join(line.strip() for line in html.unescape(text).splitlines() if line.strip())


def _at(raw: str | None) -> datetime | None:
    """Parse a message's ``<time datetime>`` (ISO 8601) to a UTC datetime."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _parse(page: str) -> list[dict]:
    """Pull ``(post_id, published_at, text)`` out of one channel page, newest last."""
    parts = _MSG.split(page)
    messages = []
    # split() yields [pre, id, slice, id, slice, …]; each slice runs to the next message.
    for post_id, chunk in zip(parts[1::2], parts[2::2], strict=False):
        text_div = _TEXT.search(chunk)
        text = _strip_html(text_div.group(1) if text_div else "")
        if not text:
            continue  # photo/video-only post carries no headline to read
        time_el = _TIME.search(chunk)
        messages.append({
            "post_id": post_id,
            "published_at": _at(time_el.group(1) if time_el else None),
            "text": text,
        })
    return messages


def _fetch_channel(channel: str, timeout: float) -> list[dict] | None:
    """One channel's newest messages; ``None`` when the fetch failed."""
    url = _PREVIEW.format(channel=channel)
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except (OSError, HTTPError, UnicodeDecodeError) as exc:
        logger.warning("Telegram preview fetch failed for %s: %s", channel, exc)
        return None
    if "tgme_widget_message" not in page:
        logger.warning("Telegram preview for %s carried no message markup", channel)
        return None
    return _parse(page)


def fetch_telegram_messages(
    channels: Iterable[str],
    *,
    limit_per_channel: int = 20,
    timeout: float = 10.0,
    start_date: str | None = None,
    end_date: str | None = None,
    screen=None,
) -> str:
    """Fetch recent messages from public Telegram channels as a plaintext block.

    Args:
        channels: Channel handles, with or without a leading ``@``.
        limit_per_channel: Messages kept per channel (the preview serves ~20).
        timeout: Per-request timeout in seconds.
        start_date / end_date: A dated window. The preview has no archive, so any
            window is reported as unavailable rather than served from the present.
        screen: Optional callable taking the message texts and returning a keep
            flag per message plus a note line that heads the block.

    Returns:
        A formatted plaintext block, or an ``<unavailable …>`` placeholder — the
        caller never has to special-case None or exceptions.
    """
    channels = [c.strip().lstrip("@") for c in channels if c and c.strip()]
    if not channels:
        return "<Telegram unavailable: no channels configured>"

    if start_date and end_date:
        return (
            f"<Telegram preview unavailable for {start_date}..{end_date}: the public "
            f"preview serves only the newest page, so this is not an absence of "
            f"announcements>"
        )

    blocks = []
    for channel in channels:
        messages = _fetch_channel(channel, timeout)
        if messages is None:
            blocks.append(f"@{channel}: <Telegram unavailable: fetch failed; this is not an absence of announcements>")
            continue

        if screen:
            keep, note = screen([m["text"][:_SCREEN_CHARS] for m in messages])
            kept = [m for m, k in zip(messages, keep, strict=True) if k]
            if not kept:
                blocks.append(f"@{channel}: {note}\n<none of the {len(messages)} recent messages was kept>")
                continue
            messages = kept
            if note:
                blocks.insert(0, note)

        messages = messages[-limit_per_channel:] if limit_per_channel else messages
        lines = [f"@{channel} — {len(messages)} recent messages:"]
        for m in messages:
            stamp = m["published_at"].strftime("%Y-%m-%d %H:%M") if m["published_at"] else "?"
            body = m["text"].replace("\n", " ").strip()
            if len(body) > 280:
                body = body[:280] + "…"
            lines.append(f"  [{stamp}] {body}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
