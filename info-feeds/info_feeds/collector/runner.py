"""常驻采集循环：按各源自己的节奏唤醒，逐个跑，互不传染。

单线程 + 每源一个「下次到期时刻」。选它而不是「每源一个线程」是因为这些源
都是每分钟级的小请求，串行足够、没有并发写库的麻烦，也符合我们一贯的轻量取向。
真出现某个源把循环拖慢的情况，再按 §2.2 的原样给每源开线程即可。
"""

from __future__ import annotations

import logging
import sqlite3
import time

from . import config, store
from .sources import SOURCES, Collected, Source

logger = logging.getLogger(__name__)

# 单次休眠的上限：源之间最长间隔 12h，但循环仍应每分钟醒一次，
# 这样 Ctrl-C 和「有新源到期」都能被及时看到。
_IDLE_SLEEP = 60.0


def collect_one(conn: sqlite3.Connection, source: Source) -> Collected | None:
    """Run one source's round, recording its outcome in ``source_health``.

    Returns the result, or None when the round failed. Any exception counts as a
    failure — including ``NoMarketDataError``, whose vendors raise both for an
    outage and for a genuinely empty window. For a collector the two are the same
    operational fact ("this source produced nothing this round"), so both are
    surfaced rather than silently swallowed.
    """
    try:
        result = source.collect(conn)
    except Exception as exc:  # noqa: BLE001 - a bad source must not stop the loop
        failures = store.record_failure(conn, source.name, f"{type(exc).__name__}: {exc}")
        logger.warning("%s: round failed (%d in a row): %s", source.name, failures, exc)
        return None
    store.record_ok(conn, source.name, result.watermark)
    logger.info("%s: +%d rows (watermark=%s)", source.name, result.written, result.watermark)
    return result


def run_once(conn: sqlite3.Connection, sources: tuple[Source, ...] = SOURCES) -> dict[str, int | None]:
    """One pass over every source, in order. ``{name: rows written or None}``."""
    written: dict[str, int | None] = {}
    for source in sources:
        result = collect_one(conn, source)
        written[source.name] = None if result is None else result.written
    return written


def run_forever(db_path: str | None = None, sources: tuple[Source, ...] = SOURCES) -> None:
    """Collect on each source's own cadence until interrupted."""
    conn = store.connect(db_path)
    next_due = {source.name: 0.0 for source in sources}  # everything is due at start
    logger.info(
        "collector started: %d sources -> %s", len(sources), db_path or config.DB_PATH
    )
    try:
        while True:
            now = time.monotonic()
            for source in sources:
                if next_due[source.name] <= now:
                    collect_one(conn, source)
                    next_due[source.name] = time.monotonic() + source.interval
            wait = min(next_due.values()) - time.monotonic()
            time.sleep(max(1.0, min(wait, _IDLE_SLEEP)))
    except KeyboardInterrupt:
        logger.info("collector stopped")
    finally:
        conn.close()
