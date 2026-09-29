"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import logging
from datetime import datetime, timezone

import requests

from info_feeds.date_window import get_current_date
from info_feeds.errors import NoMarketDataError

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    response = requests.get(
        f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def fetch_prediction_rows(
    topic: str,
    limit: int | None = None,
    curr_date: str | None = None,
) -> list[dict]:
    """Live prediction-market quotes for an event topic, as structured rows.

    Rows the collector can upsert into ``prediction_quote``; the formatted
    ``get_prediction_markets`` renders these rather than re-fetching.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        curr_date: The analysis date. Polymarket serves only live odds, so a
            date before today withholds them.

    Returns:
        Rows of ``{topic, outcome, prob, volume, pub_date}`` (newest-first by
        traded volume), plus the ``question`` / ``end_date`` / ``week_change``
        the formatted block prints. An **empty list** means the search answered
        but matched no open market — a real absence, not a failure.

    Raises:
        NoMarketDataError: the odds are withheld for a date before today, or the
            search request failed.
    """
    if curr_date and curr_date < get_current_date():
        # Raise rather than return: the router stops at the first vendor that
        # returns, so a returned notice would hide whatever the next vendor can
        # legitimately serve for that window.
        raise NoMarketDataError(
            topic, topic,
            f"Prediction-market odds are withheld for {curr_date}. Polymarket serves "
            f"only live odds on open markets, with no historical vintage, so serving "
            f"them would put post-decision information into a {curr_date} analysis.",
        )
    if limit is None:
        limit = DEFAULT_LIMIT

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        raise NoMarketDataError(
            topic, topic,
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'.",
        ) from e

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    rows = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        rows.append({
            "topic": topic,
            "outcome": outcomes[0] if outcomes else "Yes",
            "prob": prob,
            "volume": float(m.get("volumeNum") or 0),
            "pub_date": now,
            "question": m.get("question") or "",
            "end_date": (m.get("endDate") or "")[:10],
            "week_change": m.get("oneWeekPriceChange"),
        })
    return rows


def get_prediction_markets(topic: str, limit: int | None = None, curr_date: str | None = None) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        curr_date: The analysis date. Polymarket serves only live odds, so a
            date before today withholds them.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move.

    This is the formatted shell over ``fetch_prediction_rows``, which does the
    work.
    """
    try:
        rows = fetch_prediction_rows(topic, limit, curr_date)
    except NoMarketDataError as exc:
        return exc.detail or str(exc)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not rows:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for r in rows:
        wk = r["week_change"]
        wk_str = (
            f", 1-week {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{r['question']}** — {r['outcome']} {r['prob']:.0%} "
            f"(${r['volume']:,.0f} volume, resolves {r['end_date']}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"
