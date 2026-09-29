"""采集进程的存取层：连库、幂等 upsert、水位线、健康度。

**幂等是硬要求**（ARCHITECTURE §2.2）：全部走 ``INSERT … ON CONFLICT DO UPDATE``，
进程重启 / 重试都不会写出重复行。冲突键就用 schema 里已经定好的唯一约束：
新闻 ``(source, external_id)``、社媒 ``(platform, external_id)``、
预测 ``(topic, outcome, ts)``、宏观 ``(series_id, ts)``、情绪 ``(name, ts)``。

本模块只认「已经排好列」的记录：把 vendor 的行映射成表列是 ``sources.py`` 的事，
这里不掺业务判断。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")


def to_epoch(dt: datetime | None) -> int | None:
    """A UTC-aware datetime as Unix seconds; a naive value is assumed UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open the shared DB and make sure the information-layer tables exist.

    Idempotent by construction: the schema is all ``IF NOT EXISTS``, so it is
    safe to run whether the collector or the harness started first. WAL plus a
    busy timeout lets the two processes read and write the same file without the
    harness's reads blocking the collector's writes.
    """
    path = db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# ── 幂等写入 ────────────────────────────────────────────────
# 每条 SQL 的列顺序就是下面 _columns() 拼参数元组的顺序，改动要成对。

_NEWS = """
INSERT INTO news_items (source, external_id, ts, title, summary, url, symbols, sentiment)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, external_id) DO UPDATE SET
    ts=excluded.ts, title=excluded.title, summary=excluded.summary,
    url=excluded.url, symbols=excluded.symbols, sentiment=excluded.sentiment
"""

_SOCIAL = """
INSERT INTO social_items (platform, symbol, external_id, ts, text, sentiment, engagement)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(platform, external_id) DO UPDATE SET
    symbol=excluded.symbol, ts=excluded.ts, text=excluded.text,
    sentiment=excluded.sentiment, engagement=excluded.engagement
"""

_EVENTS = """
INSERT INTO market_events (source, external_id, ts, kind, symbols, title, body)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, external_id) DO UPDATE SET
    ts=excluded.ts, kind=excluded.kind, symbols=excluded.symbols,
    title=excluded.title, body=excluded.body
"""

_PREDICTIONS = """
INSERT INTO prediction_quote (topic, outcome, ts, prob, volume)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(topic, outcome, ts) DO UPDATE SET
    prob=excluded.prob, volume=excluded.volume
"""

_MACRO = """
INSERT INTO macro_series (series_id, ts, value)
VALUES (?, ?, ?)
ON CONFLICT(series_id, ts) DO UPDATE SET value=excluded.value
"""

_SENTIMENT = """
INSERT INTO sentiment_index (name, ts, value)
VALUES (?, ?, ?)
ON CONFLICT(name, ts) DO UPDATE SET value=excluded.value
"""


def _write(conn: sqlite3.Connection, sql: str, columns: tuple[str, ...], records) -> int:
    """Upsert ``records`` (dicts keyed by ``columns``) and return how many were written."""
    if not records:
        return 0
    params = [tuple(r.get(c) for c in columns) for r in records]
    with conn:  # one transaction: either the whole batch lands or none of it
        conn.executemany(sql, params)
    return len(params)


def insert_news(conn, records) -> int:
    return _write(conn, _NEWS, ("source", "external_id", "ts", "title",
                                "summary", "url", "symbols", "sentiment"), records)


def insert_social(conn, records) -> int:
    return _write(conn, _SOCIAL, ("platform", "symbol", "external_id", "ts",
                                  "text", "sentiment", "engagement"), records)


def insert_events(conn, records) -> int:
    return _write(conn, _EVENTS, ("source", "external_id", "ts", "kind",
                                  "symbols", "title", "body"), records)


def insert_predictions(conn, records) -> int:
    return _write(conn, _PREDICTIONS, ("topic", "outcome", "ts", "prob", "volume"), records)


def insert_macro(conn, records) -> int:
    return _write(conn, _MACRO, ("series_id", "ts", "value"), records)


def insert_sentiment(conn, records) -> int:
    return _write(conn, _SENTIMENT, ("name", "ts", "value"), records)


def dumps_symbols(symbols) -> str | None:
    """A symbol list as the JSON text the ``symbols`` column holds."""
    return json.dumps(list(symbols)) if symbols else None


# ── 水位线（collector_state）────────────────────────────────

def get_watermark(conn: sqlite3.Connection, source: str) -> int | None:
    row = conn.execute(
        "SELECT last_watermark FROM collector_state WHERE source = ?", (source,)
    ).fetchone()
    return row["last_watermark"] if row else None


def set_watermark(conn: sqlite3.Connection, source: str, watermark: int | None) -> None:
    """Advance a source's watermark; never move it backwards."""
    if watermark is None:
        return
    with conn:
        conn.execute(
            """
            INSERT INTO collector_state (source, last_watermark, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_watermark=MAX(COALESCE(collector_state.last_watermark, 0),
                                   excluded.last_watermark),
                updated_at=excluded.updated_at
            """,
            (source, watermark, int(time.time())),
        )


# ── 健康度（source_health）──────────────────────────────────

def record_ok(conn: sqlite3.Connection, source: str, watermark: int | None = None) -> None:
    """Mark a source healthy and clear its failure streak.

    ``watermark`` is the newest event time the successful run observed; a
    snapshot source with no event clock passes None and only clears the health.
    """
    now = int(time.time())
    with conn:
        conn.execute(
            """
            INSERT INTO source_health (source, last_ok_ts, consecutive_failures, last_error)
            VALUES (?, ?, 0, NULL)
            ON CONFLICT(source) DO UPDATE SET
                last_ok_ts=excluded.last_ok_ts, consecutive_failures=0, last_error=NULL
            """,
            (source, now),
        )
    set_watermark(conn, source, watermark)


def record_failure(conn: sqlite3.Connection, source: str, error: str) -> int:
    """Record one failure and return the resulting consecutive-failure count."""
    with conn:
        conn.execute(
            """
            INSERT INTO source_health (source, last_ok_ts, consecutive_failures, last_error)
            VALUES (?, NULL, 1, ?)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures=source_health.consecutive_failures + 1,
                last_error=excluded.last_error
            """,
            (source, error),
        )
    row = conn.execute(
        "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return row["consecutive_failures"] if row else 1
