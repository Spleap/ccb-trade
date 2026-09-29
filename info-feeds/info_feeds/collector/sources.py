"""采集源清单：每个源 = 一个节奏 + 一次「取数 → 映射成表列 → 幂等写库」。

分层：``vendors/`` 负责取到结构化行，``store`` 负责把排好列的记录写进库，
这里夹在中间做**业务映射**——把某个源的字段名字翻成某张表的列名，并决定
它写进 ``news_items`` 还是 ``social_items`` 等等。

一个源出问题不影响别的源（ARCHITECTURE §2.2 硬要求 3）：``runner`` 逐个源
捕获异常、单独记 ``source_health``，绝不因为一个源挂了就停整个采集循环。
逐标的的源（新闻 / Reddit / StockTwits / 宏观）再往下做一层隔离：某个标的
取数失败只丢它自己，剩下的照写；**只有整轮颗粒无收时**才把错误抛给 runner。

第一轮只做 5 个类别（新闻 / 社媒情绪 / 情绪指数 / 宏观 / 预测市场）。
「交易所公告 / 上币」还要先把 ``telegram_preview`` 拆出结构化出口，留待下一轮。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Iterator

from info_feeds.date_window import get_current_date
from info_feeds.vendors import (
    alternative_me,
    cryptocurrency_cv,
    fred,
    google_news_rss,
    polymarket,
    reddit,
    stocktwits,
)

from . import config, store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Collected:
    """一次采集的结果：写了几行，以及这轮见过的最大事件时刻（水位线）。"""

    written: int
    watermark: int | None


@dataclass(frozen=True)
class Source:
    """一个采集源：名字既是 ``source_health`` 的主键，也是数据的归属名。"""

    name: str
    interval: int  # 两轮之间的秒数
    collect: Callable[[sqlite3.Connection], Collected]
    description: str


# ── 通用工具 ────────────────────────────────────────────────

def _watermark(records: Iterable[dict]) -> int | None:
    stamps = [r["ts"] for r in records if r.get("ts") is not None]
    return max(stamps) if stamps else None


def _hashed_id(*parts: str) -> str:
    """A stable id from content, for feeds that serve no id of their own."""
    joined = "\x1f".join(p or "" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:20]


def _news_id(row: dict) -> str:
    """The dedup key for a news row: its link, else a hash of its headline.

    Google News and cryptocurrency.cv serve no item id, so the article URL is the
    identity; a row with no link falls back to the headline, which is also what
    those feeds dedupe on internally.
    """
    link = (row.get("link") or "").strip()
    if link:
        return link
    return _hashed_id((row.get("title") or "").strip().casefold())


def _fan_out_tagged(
    source: str, targets: Iterable[str], fetch: Callable[[str], list[dict]]
) -> list[tuple[str, dict]]:
    """Fetch each target, tagging every row with the target it came from.

    One failing target drops only its own rows; the rest still land. The first
    error is re-raised only when *nothing* answered, so a total outage registers
    in ``source_health`` while a single flaky symbol stays quiet.
    """
    tagged: list[tuple[str, dict]] = []
    errors: list[tuple[str, Exception]] = []
    for target in targets:
        try:
            tagged.extend((target, row) for row in fetch(target))
        except Exception as exc:  # noqa: BLE001 - isolation is the point here
            errors.append((target, exc))
            logger.warning("%s: target %r failed: %s", source, target, exc)
    if not tagged and errors:
        raise errors[0][1]
    return tagged


def _news_window() -> tuple[str, str]:
    """The live news window: today back ``NEWS_LOOKBACK_DAYS``."""
    start = (date.today() - timedelta(days=config.NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    return start, get_current_date()


# ── 新闻（news_items）──────────────────────────────────────

def _news_records(source: str, items: Iterable[tuple[dict, list[str] | None]]) -> list[dict]:
    """Map ``(row, symbols)`` pairs onto ``news_items`` columns.

    Rows for the same article (same link/headline) collapse into one record whose
    ``symbols`` is the union — so a story that mentions both BTC and ETH is one
    news item tagged with both, not two rows fighting over the ``symbols`` column.
    """
    merged: dict[str, dict] = {}
    for row, symbols in items:
        ts = store.to_epoch(row.get("pub_date"))
        title = (row.get("title") or "").strip()
        if ts is None or not title:
            continue  # ts/title are NOT NULL; an undated or untitled item can't be stored
        ext = _news_id(row)
        record = merged.get(ext)
        if record is None:
            merged[ext] = {
                "source": source,
                "external_id": ext,
                "ts": ts,
                "title": title,
                "summary": (row.get("summary") or "").strip() or None,
                "url": (row.get("link") or "").strip() or None,
                "symbols": set(symbols or ()),
                "sentiment": None,
            }
        else:
            record["symbols"] |= set(symbols or ())
            if not record["summary"]:
                record["summary"] = (row.get("summary") or "").strip() or None

    records = []
    for record in merged.values():
        record["symbols"] = store.dumps_symbols(sorted(record["symbols"]))
        records.append(record)
    return records


def collect_market_news(conn: sqlite3.Connection) -> Collected:
    """Market-wide crypto headlines (cryptocurrency.cv)."""
    rows = cryptocurrency_cv.fetch_global_news_rows(get_current_date())
    records = _news_records("cryptocurrency_cv", ((row, None) for row in rows))
    return Collected(store.insert_news(conn, records), _watermark(records))


def collect_symbol_news(conn: sqlite3.Connection) -> Collected:
    """Per-watchlist-symbol news (Google News, en-US + zh-CN)."""
    start, end = _news_window()
    tagged = _fan_out_tagged(
        "google_news", config.WATCHLIST,
        lambda ticker: google_news_rss.fetch_news_rows(ticker, start, end),
    )
    records = _news_records("google_news", ((row, [ticker]) for ticker, row in tagged))
    return Collected(store.insert_news(conn, records), _watermark(records))


def collect_global_news(conn: sqlite3.Connection) -> Collected:
    """Global/macro news (Google News, en-US + zh-CN)."""
    rows = google_news_rss.fetch_global_news_rows(get_current_date())
    records = _news_records("google_news_global", ((row, None) for row in rows))
    return Collected(store.insert_news(conn, records), _watermark(records))


# ── 社媒情绪（social_items）────────────────────────────────

def collect_reddit(conn: sqlite3.Connection) -> Collected:
    """Reddit discussion per watchlist symbol."""
    tagged = _fan_out_tagged("reddit", config.WATCHLIST, reddit.fetch_reddit_rows)
    records = []
    for ticker, row in tagged:
        ts = store.to_epoch(row.get("pub_date"))
        external_id = (row.get("external_id") or "").strip()
        if ts is None or not external_id:
            continue
        text = "\n".join(p for p in (row.get("title"), row.get("body")) if p)
        records.append({
            "platform": "reddit",
            "symbol": ticker,
            "external_id": external_id,
            "ts": ts,
            "text": text or None,
            "sentiment": None,
            "engagement": None,
        })
    return Collected(store.insert_social(conn, records), _watermark(records))


def collect_stocktwits(conn: sqlite3.Connection) -> Collected:
    """StockTwits messages per watchlist symbol (carries the user's own tag)."""
    tagged = _fan_out_tagged("stocktwits", config.WATCHLIST, stocktwits.fetch_stocktwits_rows)
    records = []
    for ticker, row in tagged:
        ts = store.to_epoch(row.get("pub_date"))
        body = (row.get("body") or "").strip()
        if ts is None or not body:
            continue
        records.append({
            "platform": "stocktwits",
            "symbol": ticker,
            # StockTwits serves no message id, so identity is content + author + time.
            "external_id": _hashed_id(
                row.get("username") or "", row.get("created_at") or "", body
            ),
            "ts": ts,
            "text": body,
            "sentiment": row.get("sentiment"),
            "engagement": None,
        })
    return Collected(store.insert_social(conn, records), _watermark(records))


# ── 情绪指数（sentiment_index）─────────────────────────────

def collect_sentiment(conn: sqlite3.Connection) -> Collected:
    """Crypto Fear & Greed index (Alternative.me) — one series for the market."""
    rows = alternative_me.fetch_sentiment_rows()
    records = []
    for row in rows:
        ts = store.to_epoch(row.get("pub_date"))
        value = row.get("value")
        if ts is None or value is None:
            continue
        records.append({"name": "fear_greed", "ts": ts, "value": float(value)})
    return Collected(store.insert_sentiment(conn, records), _watermark(records))


# ── 宏观（macro_series）────────────────────────────────────

def collect_macro(conn: sqlite3.Connection) -> Collected:
    """FRED macro series (needs ``FRED_API_KEY`` or the source fails visibly)."""
    today = get_current_date()
    tagged = _fan_out_tagged(
        "macro", config.MACRO_SERIES, lambda alias: fred.fetch_macro_rows(alias, today)
    )
    records = []
    for _alias, row in tagged:
        ts = store.to_epoch(row.get("pub_date"))
        series_id = row.get("series_id")
        if ts is None or series_id is None or row.get("value") is None:
            continue
        records.append({"series_id": series_id, "ts": ts, "value": float(row["value"])})
    return Collected(store.insert_macro(conn, records), _watermark(records))


# ── 预测市场（prediction_quote）────────────────────────────

def collect_prediction(conn: sqlite3.Connection) -> Collected:
    """Polymarket live odds for the configured topics."""
    tagged = _fan_out_tagged(
        "polymarket", config.PREDICTION_TOPICS, polymarket.fetch_prediction_rows
    )
    records = []
    for topic, row in tagged:
        ts = store.to_epoch(row.get("pub_date"))
        prob = row.get("prob")
        if ts is None or prob is None:
            continue
        records.append({
            # ``topic`` stays the query keyword, because that is what the agent
            # passes to ``get_prediction_market`` and what the reader filters on.
            "topic": row.get("topic") or topic,
            # ``outcome`` has to identify the *market*, not the side: one topic
            # matches many markets, all fetched in the same second, and the
            # reader groups by outcome. Mapping the vendor's "Yes"/"No" label
            # here would collapse every market of a topic into one row.
            "outcome": row.get("question") or row.get("outcome") or "Yes",
            "ts": ts,
            "prob": float(prob),
            "volume": float(row["volume"]) if row.get("volume") is not None else None,
        })
    return Collected(store.insert_predictions(conn, records), _watermark(records))


# ── 注册表 ─────────────────────────────────────────────────
# 节奏依据 ARCHITECTURE §2.2 的采集节奏表：快讯分钟级、社媒十几分钟、
# 预测市场 5 分钟、情绪指数 1 小时、宏观 12 小时。

SOURCES: tuple[Source, ...] = (
    Source("cryptocurrency_cv", 180, collect_market_news, "加密市场快讯（cryptocurrency.cv）"),
    Source("google_news", 300, collect_symbol_news, "逐标的新闻（Google News en/zh）"),
    Source("google_news_global", 300, collect_global_news, "宏观 / 市场新闻（Google News en/zh）"),
    Source("reddit", 600, collect_reddit, "Reddit 讨论（逐标的）"),
    Source("stocktwits", 900, collect_stocktwits, "StockTwits 情绪（逐标的）"),
    Source("polymarket", 300, collect_prediction, "预测市场赔率（Polymarket）"),
    Source("fear_greed", 3600, collect_sentiment, "加密恐惧贪婪指数（Alternative.me）"),
    Source("macro", 43200, collect_macro, "宏观序列（FRED，需 API key）"),
)
