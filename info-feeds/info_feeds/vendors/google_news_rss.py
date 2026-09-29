"""Google News RSS vendor.

Aggregates publisher headlines for a query in two locales (``en-US`` and
``zh-CN``), so one call covers English and Chinese coverage of the same subject
— the feed is locale-scoped, so the same query in two locales returns two
different publisher sets.

Google serves title + publisher + publish time only: no summary, and the item
link is a Google redirect rather than the article URL. Treat it as a coverage /
fallback source, not a substitute for a vendor that returns article text.

No archive: the feed is the *current* search result set, so a window that does
not reach the present is withheld (TEXT_SOURCES.md §7) instead of leaking
present-day headlines into a historical analysis. No key required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import requests

from info_feeds.config import get_config
from info_feeds.date_window import coverage_gap, get_current_date, in_window
from info_feeds.errors import NoMarketDataError
from info_feeds.symbols import crypto_base

logger = logging.getLogger(__name__)

_FEED = "https://news.google.com/rss/search"

# Descriptive UA: the feed is public and needs no browser spoofing.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

REQUEST_TIMEOUT = 15

# Google News caps a single feed at about 100 items; a body far beyond that means
# something other than the feed is answering.
_MAX_FEED_BYTES = 8 * 1024 * 1024

# The locales queried for every subject. en-US first so English coverage leads.
_LOCALES = (
    {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh"},
)

# Chinese-language search terms for the major crypto assets. Querying "BTC" in
# the zh-CN locale returns mostly English-language results, so the zh locale only
# earns its keep with the Chinese name of the asset.
_ZH_NAMES = {
    "BTC": "比特币", "ETH": "以太坊", "SOL": "索拉纳", "XRP": "瑞波币",
    "DOGE": "狗狗币", "ADA": "艾达币", "BNB": "币安币", "LTC": "莱特币",
    "BCH": "比特现金", "DOT": "波卡", "AVAX": "雪崩币", "LINK": "Chainlink",
}

# Chinese-language queries for the global/macro sweep, alongside the configured
# English queries. Without them the zh-CN locale would be decorative: Google News
# matches a Chinese query to Chinese publishers far better than an English one.
_ZH_GLOBAL_QUERIES = (
    "比特币 加密货币",
    "美联储 利率 通胀",
    "加密货币 监管 政策",
)


def _query_for(ticker: str) -> str:
    """A search query the feed can match: crypto pairs become their base.

    ``BTC-USD`` is a Yahoo spelling no publisher uses; ``BTC`` is what the feed
    is indexed on.
    """
    return crypto_base(ticker) or ticker


def _published_at(raw: str | None) -> datetime | None:
    """Parse an RSS ``pubDate`` (RFC 822) to a UTC-aware datetime, or None."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _headline(title: str, publisher: str) -> str:
    """Google News titles are ``"Headline - Publisher"``; drop the redundant tail."""
    suffix = f" - {publisher}"
    if publisher and title.endswith(suffix):
        return title[: -len(suffix)]
    return title


def _fetch(query: str, locale: dict, timeout: float) -> list[dict] | None:
    """One locale's feed for ``query``.

    Returns ``[]`` when the feed ran and matched nothing, and ``None`` when the
    fetch itself failed, so an outage is never rendered as "no news".
    """
    params = {"q": query, **locale}
    url = f"{_FEED}?{urlencode(params)}"
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
        if len(resp.content) > _MAX_FEED_BYTES:
            logger.warning("Google News feed for %r exceeded %d bytes", query, _MAX_FEED_BYTES)
            return None
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        logger.warning("Google News fetch failed for %r (%s): %s", query, locale["ceid"], exc)
        return None

    items = []
    for item in root.iter("item"):
        source_el = item.find("source")
        publisher = (source_el.text if source_el is not None else "") or "Google News"
        title = _headline(item.findtext("title") or "", publisher)
        if not title:
            continue
        items.append({
            "title": title,
            "publisher": publisher,
            "link": item.findtext("link") or "",
            "pub_date": _published_at(item.findtext("pubDate")),
            "locale": locale["ceid"],
        })
    return items


def _collect(pairs: list[tuple[dict, str]], timeout: float) -> tuple[list[dict], list[str]]:
    """Fetch every ``(locale, query)`` pair; return the items and the failed locales.

    A failure in one locale does not discard another's items — partial coverage
    beats none — but a total failure is the caller's to report as unavailable.
    """
    items: list[dict] = []
    failed: list[str] = []
    answered = False
    for locale, query in pairs:
        fetched = _fetch(query, locale, timeout)
        if fetched is None:
            failed.append(locale["ceid"])
        else:
            answered = True
            items.extend(fetched)
    return (items, [] if answered else failed)


def _within_window(items, start_date, end_date):
    """Trim items to ``[start_date, end_date]`` (look-ahead safe)."""
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return [i for i in items if in_window(i["pub_date"], start_dt, end_dt)]


def _dedupe(items) -> list[dict]:
    """One entry per headline, newest first (the same story arrives via several feeds)."""
    seen: set[str] = set()
    unique = []
    for item in sorted(items, key=lambda i: i["pub_date"] or datetime.min.replace(tzinfo=timezone.utc),
                       reverse=True):
        key = item["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _render(items, start_date, end_date, header) -> str:
    blocks = []
    for item in items:
        block = f"### {item['title']} (source: {item['publisher']})"
        if item["link"]:
            block += f"\nLink: {item['link']}"
        blocks.append(block)
    return f"## {header}, from {start_date} to {end_date}:\n\n" + "\n\n".join(blocks)


def _withhold(label: str, as_of: str) -> str:
    return (
        f"Google News is withheld for {as_of}. Its RSS feed serves only the current "
        f"search results, with no archive or historical vintage, so serving them "
        f"would put post-decision information into a {as_of} analysis of {label}."
    )


def _withhold_error(symbol: str, canonical: str, label: str, as_of: str) -> NoMarketDataError:
    """Raise-shape of the withholding rule.

    A chain member has to *raise* rather than return: the router stops at the
    first vendor that returns, so a returned notice would hide whatever the next
    vendor can legitimately serve for that window.
    """
    return NoMarketDataError(symbol, canonical, _withhold(label, as_of))


def fetch_news_rows(
    ticker: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """News for ``ticker`` from Google News (en-US + zh-CN) as structured rows.

    Same fetch, dedupe and look-ahead window as the formatted block; returns the
    rows so the collector can upsert them, and ``get_news`` renders these
    instead of re-fetching.

    Args:
        ticker: Stock/crypto symbol (e.g. ``"NVDA"``, ``"BTC-USD"``).
        start_date: Start date in yyyy-mm-dd format.
        end_date: End date in yyyy-mm-dd format. A window that does not reach
            today is withheld: the feed has no archive.

    Returns:
        Newest-first rows of ``{title, publisher, link, pub_date, locale}``.

    Raises:
        NoMarketDataError: no feed answered, or nothing fell inside the window.
    """
    if end_date < get_current_date():
        raise _withhold_error(ticker, _query_for(ticker), ticker, end_date)

    query = _query_for(ticker)
    pairs = [(locale, _ZH_NAMES.get(query.upper(), query) if locale["ceid"].endswith(":zh") else query)
             for locale in _LOCALES]

    fetched, failed = _collect(pairs, REQUEST_TIMEOUT)
    if failed and not fetched:
        raise NoMarketDataError(
            ticker, query, f"Google News unavailable (no feed answered among {failed})"
        )

    items = _dedupe(_within_window(fetched, start_date, end_date))
    if not items:
        # Raise, not return: a returned "nothing found" would stop the router
        # here and hide the next vendor's coverage of the same symbol/window.
        gap = coverage_gap(
            (i["pub_date"] for i in fetched), start_date, end_date,
            "Google News", f"news for {ticker}",
        )
        raise NoMarketDataError(
            ticker, query,
            gap or f"No news found for {ticker} between {start_date} and {end_date}",
        )
    return items


def get_news(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """Retrieve news for ``ticker`` from Google News, English and Chinese.

    Args:
        ticker: Stock/crypto symbol (e.g. ``"NVDA"``, ``"BTC-USD"``).
        start_date: Start date in yyyy-mm-dd format.
        end_date: End date in yyyy-mm-dd format. A window that does not reach
            today is withheld: the feed has no archive.

    Returns:
        Formatted string containing news articles.
    """
    rows = fetch_news_rows(ticker, start_date, end_date)
    limit = get_config()["news_article_limit"]
    return _render(rows[:limit], start_date, end_date, f"{ticker} News (Google News)")


def fetch_global_news_rows(
    curr_date: str,
    look_back_days: int | None = None,
) -> list[dict]:
    """Global/macro news from Google News (en-US + zh-CN) as structured rows.

    Same multi-query sweep, dedupe and look-ahead window as the formatted block;
    returns the rows so the collector can upsert them.

    Args:
        curr_date: Current date in yyyy-mm-dd format.
        look_back_days: Days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.

    Returns:
        Newest-first rows of ``{title, publisher, link, pub_date, locale}``.

    Raises:
        NoMarketDataError: no feed answered, or nothing fell inside the window.
    """
    if curr_date < get_current_date():
        raise _withhold_error("global news", "global news", "market news", curr_date)

    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]

    start_date = (
        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")

    queries = list(config["global_news_queries"])
    pairs = [(_LOCALES[0], q) for q in queries] + [(_LOCALES[1], q) for q in _ZH_GLOBAL_QUERIES]
    fetched, failed = _collect(pairs, REQUEST_TIMEOUT)
    if failed and not fetched:
        raise NoMarketDataError(
            "global news", "global news",
            f"Google News unavailable (no feed answered among {sorted(set(failed))})",
        )

    items = _dedupe(_within_window(fetched, start_date, curr_date))
    if not items:
        # Raise, not return: a returned "nothing found" would stop the router
        # here and hide the next vendor's coverage of the same window.
        gap = coverage_gap(
            (i["pub_date"] for i in fetched), start_date, curr_date,
            "Google News", "market news",
        )
        raise NoMarketDataError(
            "global news", "global news",
            gap or f"No global news found between {start_date} and {curr_date}",
        )
    return items


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Retrieve global/macro news from Google News, English and Chinese.

    Args:
        curr_date: Current date in yyyy-mm-dd format.
        look_back_days: Days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles.
    """
    rows = fetch_global_news_rows(curr_date, look_back_days)
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    start_date = (
        datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    ).strftime("%Y-%m-%d")
    return _render(rows[:limit], start_date, curr_date, "Global Market News (Google News)")
