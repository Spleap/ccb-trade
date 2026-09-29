"""cryptocurrency.cv aggregator vendor.

A keyless crypto/macro headline aggregator spanning 100+ publishers (CoinDesk,
The Block, Decrypt, Cointelegraph … plus Chinese outlets such as Odaily,
BlockTempo and Chain Catcher). One ``/api/news`` call returns the newest
headlines, filterable by ``category`` and ``source``.

Verified free-tier behaviour (2026-09-27), which bounds what this module may
claim:

* ``/api/news`` answers, but the free tier returns **at most 3 articles per
  call** regardless of ``limit``, and it has **no per-symbol query** — there is
  no parameter that filters by ticker. So this vendor serves market-wide news
  only; it is deliberately **not** registered for ``get_news``, where it would
  return market headlines while pretending to be about the requested symbol.
* ``/api/search`` and ``/api/breaking`` and ``/api/narratives`` answer
  ``HTTP 402`` — they are behind x402 micropayments now.
* ``/api/archive`` accepts ``start_date``/``end_date``/``ticker`` but returns
  ``count: 0`` for every range, including ones inside its advertised archive.

With no working archive, a window that does not reach today is withheld
(TEXT_SOURCES.md §7) instead of being served from the present. No key required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from info_feeds.config import get_config
from info_feeds.date_window import coverage_gap, get_current_date, in_window
from info_feeds.errors import NoMarketDataError

logger = logging.getLogger(__name__)

_BASE = "https://cryptocurrency.cv"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
REQUEST_TIMEOUT = 20

# Free tier caps a response at 3 articles, so a single query is too thin to be
# useful; several categories are merged to reach a normal headline count.
_CATEGORIES = ("general", "bitcoin", "ethereum", "defi", "macro")


def _published_at(raw: str | None) -> datetime | None:
    """Parse an ISO 8601 ``pubDate`` (``2026-09-27T05:30:46.000Z``) to UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _fetch(category: str, timeout: float) -> list[dict] | None:
    """One category's newest headlines; ``None`` when the fetch failed."""
    try:
        resp = requests.get(
            f"{_BASE}/api/news",
            params={"category": category},
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("cryptocurrency.cv fetch failed for %r: %s", category, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("articles") or []


def fetch_global_news_rows(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Market-wide crypto headlines as structured rows.

    Same selection as the formatted block (category round-robin, look-ahead
    filtered), but returning the rows themselves so the collector can upsert
    them; ``get_global_news`` renders these rather than re-fetching.

    Args:
        curr_date: Current date in yyyy-mm-dd format. A date before today is
            withheld — the free tier has no working archive.
        look_back_days: Days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Newest-first rows of ``{title, summary, publisher, link, pub_date}``.

    Raises:
        NoMarketDataError: no category answered, or nothing fell inside the
            requested window.
    """
    if curr_date < get_current_date():
        # Raise rather than return: the router stops at the first vendor that
        # returns, so a returned notice would hide whatever the next vendor can
        # legitimately serve for that window.
        raise NoMarketDataError(
            "global news", "global news",
            f"cryptocurrency.cv news is withheld for {curr_date}: its search and "
            f"archive endpoints are paywalled (HTTP 402) or return nothing, so the "
            f"only servable feed is the present one",
        )

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    start_date = (
        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")

    by_category: dict[str, list[dict]] = {}
    answered = False
    for category in _CATEGORIES:
        articles = _fetch(category, REQUEST_TIMEOUT)
        if articles is None:
            continue
        answered = True
        by_category[category] = articles

    if not answered:
        raise NoMarketDataError("global news", "global news",
                                "cryptocurrency.cv unavailable (no category answered)")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # Bucket by category before selecting. The free tier returns at most 3
    # articles per call, and a single category can be dominated by one publisher
    # (the macro feed is mostly Federal Reserve notes that share a timestamp);
    # flattening the categories and sorting by date would let that one source
    # fill the whole response and push the crypto coverage out.
    seen: set[str] = set()
    buckets: dict[str, list[dict]] = {}
    for category, articles in by_category.items():
        rows = []
        for article in articles:
            title = (article.get("title") or "").strip()
            if not title or title.casefold() in seen:
                continue
            pub_date = _published_at(article.get("pubDate"))
            if not in_window(pub_date, start_dt, end_dt):
                continue
            seen.add(title.casefold())
            rows.append({
                "title": title,
                "summary": (article.get("description") or "").strip(),
                "publisher": article.get("source") or "cryptocurrency.cv",
                "link": article.get("link") or "",
                "pub_date": pub_date,
            })
        if rows:
            rows.sort(
                key=lambda a: a["pub_date"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            buckets[category] = rows

    if not buckets:
        # Every category answered but nothing fell inside the window: the vendor is
        # healthy, so this is a real absence — report it so a later vendor with
        # better coverage of the window still gets a turn.
        gap = coverage_gap(
            (_published_at(a.get("pubDate"))
             for rows in by_category.values() for a in rows),
            start_date, curr_date, "cryptocurrency.cv", "market news",
        )
        raise NoMarketDataError(
            "global news", "global news",
            gap or f"no article within {start_date}..{curr_date}",
        )

    # Round-robin one article per category per pass (newest first within each),
    # so every category is represented before any category takes a second slot.
    in_window_news: list[dict] = []
    depth = max(len(rows) for rows in buckets.values())
    for i in range(depth):
        for rows in buckets.values():
            if i < len(rows):
                in_window_news.append(rows[i])
                if len(in_window_news) >= limit:
                    break
        if len(in_window_news) >= limit:
            break

    return in_window_news


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Retrieve market-wide crypto headlines from cryptocurrency.cv.

    Args:
        curr_date: Current date in yyyy-mm-dd format. A date before today is
            withheld — the free tier has no working archive.
        look_back_days: Days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles.
    """
    rows = fetch_global_news_rows(curr_date, look_back_days, limit)
    if look_back_days is None:
        look_back_days = get_config()["global_news_lookback_days"]
    start_date = (
        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")

    blocks = []
    for article in rows:
        block = f"### {article['title']} (source: {article['publisher']})"
        if article["summary"]:
            block += f"\n{article['summary']}"
        if article["link"]:
            block += f"\nLink: {article['link']}"
        blocks.append(block)
    return (
        f"## Global Market News (cryptocurrency.cv), from {start_date} to {curr_date}:"
        f"\n\n" + "\n\n".join(blocks)
    )
