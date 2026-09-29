"""Alternative.me Crypto Fear & Greed Index vendor.

Market-level sentiment as a single 0-100 number (0 = extreme fear, 100 =
extreme greed) with the publisher's own classification. Unlike a per-symbol
social feed, this is one series for the whole crypto market — useful as a
regime backdrop, never as a signal about a specific coin.

The endpoint serves a dated series, so a historical run can be served honestly:
we ask for enough periods to reach the requested date and then trim to it
(TEXT_SOURCES.md §7). No key required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from info_feeds.errors import NoMarketDataError

logger = logging.getLogger(__name__)

_API = "https://api.alternative.me/fng/"

# The index is published once a day at 00:00 UTC; ask for a few extra periods so
# a requested date is inside the returned slice even after the trim.
_DEFAULT_PERIODS = 30
_MAX_PERIODS = 2000


def _parse_ts(raw) -> datetime | None:
    """The index publishes epoch seconds (UTC) — normalize to a UTC datetime."""
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _as_of_date(curr_date: str | None) -> datetime:
    """The analysis instant: ``curr_date`` at 00:00 UTC, else now."""
    if not curr_date:
        return datetime.now(timezone.utc)
    try:
        return datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise NoMarketDataError("fear_greed", "fear_greed",
                                f"invalid curr_date {curr_date!r}") from exc


def fetch_sentiment_rows(
    curr_date: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """The Fear & Greed series, as structured rows, up to ``curr_date``.

    Returns rows the collector can upsert; ``get_sentiment_index`` renders a
    slice of these rather than re-fetching.

    Args:
        curr_date: The analysis date (yyyy-mm-dd). Only values published on or
            before it are served; ``None`` means "as of now".
        limit: How many trailing periods to ask the endpoint for. ``None`` uses
            30. This bounds the request, not the return: every row the endpoint
            serves inside the window is returned.

    Returns:
        Ascending rows of ``{pub_date, value, label}``.

    Raises:
        NoMarketDataError: the endpoint was unreachable, reported an error, or
            ``curr_date`` is malformed. An **empty list** means the index
            answered but published no period on or before that date — a real
            absence, not a failure.
    """
    want = _DEFAULT_PERIODS if limit is None else max(1, int(limit))
    as_of = _as_of_date(curr_date)

    # Reach back far enough that a date in the past is inside the slice.
    days_back = max(0, (datetime.now(timezone.utc).date() - as_of.date()).days)
    requested = min(_MAX_PERIODS, days_back + want)

    try:
        resp = requests.get(_API, params={"limit": requested}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise NoMarketDataError(
            "fear_greed", "fear_greed", f"Alternative.me unavailable: {exc}"
        ) from exc

    error = (payload.get("metadata") or {}).get("error")
    if error:
        raise NoMarketDataError("fear_greed", "fear_greed", f"Alternative.me error: {error}")

    rows = []
    for entry in payload.get("data") or []:
        ts = _parse_ts(entry.get("timestamp"))
        if ts is None or ts.date() > as_of.date():
            continue  # a value published after the analysis date is post-decision
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            continue
        rows.append({"pub_date": ts, "value": value,
                     "label": entry.get("value_classification") or ""})

    rows.sort(key=lambda r: r["pub_date"])
    return rows


def get_sentiment_index(
    curr_date: str | None = None,
    limit: int | None = None,
) -> str:
    """Return the Crypto Fear & Greed series as of ``curr_date``.

    Args:
        curr_date: The analysis date (yyyy-mm-dd). Only values published on or
            before it are served; ``None`` means "as of now".
        limit: Number of most-recent periods to return. ``None`` uses 30.

    Returns:
        A markdown block with the latest value and the trailing series.
    """
    want = _DEFAULT_PERIODS if limit is None else max(1, int(limit))
    rows = fetch_sentiment_rows(curr_date, limit)
    if not rows:
        as_of = _as_of_date(curr_date)
        return (
            f"Crypto Fear & Greed values are withheld for "
            f"{as_of.strftime('%Y-%m-%d')}: the series returned no period "
            f"published on or before that date, so any value would be "
            f"post-decision information."
        )

    as_of = _as_of_date(curr_date)
    series = rows[-want:]

    latest = series[-1]
    tail = ", ".join(
        f"{r['pub_date'].strftime('%Y-%m-%d')}:{r['value']:g}" for r in series
    )
    return (
        f"## Crypto Fear & Greed Index (Alternative.me), as of "
        f"{as_of.strftime('%Y-%m-%d')}\n\n"
        f"Latest: {latest['value']:g} ({latest['label']}) on "
        f"{latest['pub_date'].strftime('%Y-%m-%d')}\n"
        f"Series ({len(series)} periods, oldest -> newest): {tail}\n\n"
        f"Market-level index for crypto as a whole (0 = extreme fear, "
        f"100 = extreme greed) — not a signal about any single coin."
    )
