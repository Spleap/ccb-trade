"""A 类工具：数据工具 —— 读"世界"（ARCHITECTURE §4.3）。

来源分两半：
  * **push 层**（DB 只读）：新闻 / 社媒 / 公告 / 预测市场 / 宏观 / 情绪指数
  * **pull 层**（按需拉）：K 线 / 指标 —— 经 `ctx.candles` 注入，不落库

两条纪律：
  * 所有返回值都带长度上限，且 `limit` 由服务端再夹一次（§4.2 第 3 条）
  * 读到行情数字顺手 `ctx.record(...)`，⑦ Journal 落成快照（§2.4）

一个刻意的区分
--------------
**"没数据"不是"失败"。** "过去 24h 没有新闻"是有效信息，返回一句人话；
只有真正拿不到（无数据源、查询出错）才返回 `DATA_UNAVAILABLE:` 哨兵。
两者混为一谈，系统会把"今天很平静"误判成"数据源挂了"，进而放弃本 tick（§3.5）。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from harness.store import repo
from harness.tools import indicators
from harness.tools.base import Tool, ToolContext, unavailable


def _utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _info_symbol(symbol: str) -> str:
    """把交易口径的符号翻成**信息层口径**：`BTC/USDT` / `BTC-USD` -> `BTC-USD`。

    agent 的 universe 是交易口径（`BTC/USDT`，给交易所用），而 info-feeds 存的
    symbols 是 Yahoo 风格的 `BTC-USD`。两边不翻一下，`get_news` 的 LIKE 与
    `get_sentiment` 的精确匹配都会读回空 —— 而"读回空"会被 LLM 当成"今天很平静"。
    只对加密基础币做映射；非加密的陌生符号原样返回。
    """
    base = re.match(r"[A-Za-z]+", symbol.strip())
    return f"{base.group(0).upper()}-USD" if base else symbol


def _empty(what: str) -> str:
    return f"（无数据：{what}）"


def _limit(value, default: int, hard: int = 200) -> int:
    """服务端再夹一次。签名里的 limit 只是"上限的上限"（§4.2 第 3 条）。"""
    n = default if value in (None, 0) else int(value)
    return max(1, min(n, hard))


def _as_list(value, default: list[str]) -> list[str]:
    if value in (None, "", []):
        return default
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


# ============================================================
# pull 层：K 线 / 指标
# ============================================================


def get_candles(ctx: ToolContext, symbol: str, tf: str = "1h", limit: int | None = None) -> str:
    if ctx.candles is None:
        return unavailable("未接入 K 线来源（pull 层）")
    n = _limit(limit, ctx.cfg.tool_default_limit)
    bars = ctx.candles.fetch(symbol, tf, n, ctx.as_of)
    if not bars:
        return _empty(f"{symbol} {tf} 在 {_utc(ctx.as_of)} 之前没有已完结的 K 线")

    last = bars[-1]
    ctx.record(symbol, tf=tf, last_close=float(last["close"]),
               last_bar_ts=int(last["ts"]), bars_seen=len(bars),
               window_high=max(float(b["high"]) for b in bars),
               window_low=min(float(b["low"]) for b in bars))

    lines = [f"{_utc(b['ts'])}  O{b['open']:.6g} H{b['high']:.6g} "
             f"L{b['low']:.6g} C{b['close']:.6g} V{b.get('volume', 0):.6g}" for b in bars]
    return (f"{symbol} {tf} 最近 {len(bars)} 根（升序，只含 {_utc(ctx.as_of)} 前**已完结**的）：\n"
            + "\n".join(lines))


def get_indicators(ctx: ToolContext, symbol: str, names=None, tf: str = "1h",
                   limit: int | None = None) -> str:
    if ctx.candles is None:
        return unavailable("未接入 K 线来源（pull 层）")
    wanted = _as_list(names, ["rsi14", "atr14"])
    n = _limit(limit, 200)
    bars = ctx.candles.fetch(symbol, tf, n, ctx.as_of)
    if not bars:
        return _empty(f"{symbol} {tf} 在 {_utc(ctx.as_of)} 之前没有已完结的 K 线")

    values = indicators.compute(wanted, bars)
    skipped = values.pop("skipped", [])
    if not values:
        return _empty(f"{symbol} {tf} 样本不足，算不出 {'/'.join(wanted)}")

    ctx.record(symbol, tf=tf, **values)
    body = json.dumps(values, ensure_ascii=False, sort_keys=True)
    tail = f"  ｜ 算不出：{', '.join(skipped)}" if skipped else ""
    return f"{symbol} {tf}（基于 {len(bars)} 根）：{body}{tail}"


# ============================================================
# push 层：DB 只读
# ============================================================


def get_news(ctx: ToolContext, symbol: str | None = None, lookback_hours: int = 24,
             limit: int | None = None) -> str:
    n = _limit(limit, 10, hard=50)
    since = ctx.as_of - int(lookback_hours) * 3600
    syms = [_info_symbol(symbol)] if symbol else None
    rows = repo.recent_news(ctx.conn, ctx.as_of, since, syms, n)
    if not rows:
        scope = symbol or "全市场"
        return _empty(f"{scope} 在过去 {lookback_hours}h 没有新闻")

    lines = []
    for r in rows:
        tags = r["symbols"] or ""
        lines.append(f"- [{_utc(r['ts'])}] ({r['source']}) {r['title']}"
                     + (f"  {tags}" if tags else "")
                     + (f"\n  {r['summary']}" if r.get("summary") else ""))
    return f"新闻（最近 {len(rows)} 条，截止 {_utc(ctx.as_of)}）：\n" + "\n".join(lines)


def get_global_news(ctx: ToolContext, lookback_hours: int = 24,
                    limit: int | None = None) -> str:
    return get_news(ctx, symbol=None, lookback_hours=lookback_hours, limit=limit)


def get_sentiment(ctx: ToolContext, symbol: str, lookback_hours: int = 24,
                  limit: int | None = None, ) -> str:
    n = _limit(limit, 50, hard=200)
    since = ctx.as_of - int(lookback_hours) * 3600
    rows = repo.recent_social(ctx.conn, ctx.as_of, since, _info_symbol(symbol), n)
    if not rows:
        return _empty(f"{symbol} 在过去 {lookback_hours}h 没有社媒样本")

    bull = sum(1 for r in rows if (r["sentiment"] or "") == "Bullish")
    bear = sum(1 for r in rows if (r["sentiment"] or "") == "Bearish")
    engagement = sum(int(r["engagement"] or 0) for r in rows)
    ctx.record(symbol, social_samples=len(rows), social_bull=bull,
               social_bear=bear, social_engagement=engagement)

    samples = "\n".join(f"- ({(r['sentiment'] or 'n/a')[:4]}) {(r['text'] or '')[:120]}"
                        for r in rows[:5])
    return (f"{symbol} 社媒（过去 {lookback_hours}h，样本 {len(rows)}）："
            f"看多 {bull} / 看空 {bear} / 其余 {len(rows) - bull - bear}，互动量 {engagement}\n"
            f"最近样本：\n{samples}")


def get_sentiment_index(ctx: ToolContext, name: str = "fear_greed",
                        limit: int | None = None) -> str:
    n = _limit(limit, 30, hard=180)
    rows = repo.recent_sentiment_index(ctx.conn, name, ctx.as_of, n)
    if not rows:
        return _empty(f"没有 {name} 的历史值")

    ctx.record(f"index:{name}", sentiment_index=rows[-1]["value"],
               sentiment_index_ts=rows[-1]["ts"])
    series = ", ".join(f"{_utc(r['ts'])[5:10]}:{r['value']:.4g}" for r in rows)
    return f"{name}（最新 {rows[-1]['value']:.4g}，共 {len(rows)} 期）\n{series}"


def get_market_events(ctx: ToolContext, symbols=None, lookback_hours: int = 72,
                      limit: int | None = None) -> str:
    n = _limit(limit, 20, hard=50)
    syms = [_info_symbol(s) for s in _as_list(symbols, [])]
    since = ctx.as_of - int(lookback_hours) * 3600
    rows = repo.recent_events(ctx.conn, ctx.as_of, since, syms or None, n)
    if not rows:
        scope = "/".join(syms) if syms else "全市场"
        return _empty(f"{scope} 在过去 {lookback_hours}h 没有公告事件")

    lines = [f"- [{_utc(r['ts'])}] {r['kind']}: {r['title']}" for r in rows]
    return (f"交易所公告（最近 {len(rows)} 条，截止 {_utc(ctx.as_of)}）：\n" + "\n".join(lines))


def get_prediction_market(ctx: ToolContext, topic: str, limit: int | None = None) -> str:
    n = _limit(limit, 50, hard=200)
    rows = repo.recent_prediction(ctx.conn, topic, ctx.as_of, n)
    if not rows:
        return _empty(f"没有和 {topic!r} 相关的预测市场报价")

    latest: dict[str, dict] = {}                     # 降序返回，首次出现即最新
    for r in rows:
        latest.setdefault(r["outcome"], r)
    ctx.record(f"prediction:{topic}", **{f"p_{o}": v["prob"] for o, v in latest.items()})
    lines = [f"- {outcome}: {r['prob']:.4g}（{_utc(r['ts'])}）" for outcome, r in latest.items()]
    return f"预测市场 {topic}（{len(latest)} 个结果）：\n" + "\n".join(lines)


def get_macro(ctx: ToolContext, series_id: str, limit: int | None = None) -> str:
    n = _limit(limit, 12, hard=120)
    rows = repo.recent_macro(ctx.conn, series_id, ctx.as_of, n)
    if not rows:
        return _empty(f"没有 {series_id} 的历史值")

    ctx.record(f"macro:{series_id}", macro=rows[-1]["value"], macro_ts=rows[-1]["ts"])
    series = ", ".join(f"{_utc(r['ts'])[:10]}:{r['value']:.6g}" for r in rows)
    return f"{series_id}（最新 {rows[-1]['value']:.6g}，共 {len(rows)} 期）\n{series}"


# ============================================================
# 注册
# ============================================================

_DATA_TOOLS: list[Tool] = [
    Tool("get_candles", "读某标的的 OHLCV（pull 层，只含已完结的 K 线）",
         {"symbol": "str", "tf": "str=1h", "limit": "int<=200"}, get_candles),
    Tool("get_indicators", "服务端算技术指标，不要自己算",
         {"symbol": "str", "names": "list[str] 如 rsi14,atr14,macd", "tf": "str=1h"}, get_indicators),
    Tool("get_news", "读某标的的相关新闻",
         {"symbol": "str", "lookback_hours": "int=24", "limit": "int<=50"}, get_news),
    Tool("get_global_news", "读全市场新闻（宏观背景）",
         {"lookback_hours": "int=24", "limit": "int<=50"}, get_global_news),
    Tool("get_sentiment", "读某标的的社媒多空分布",
         {"symbol": "str", "lookback_hours": "int=24"}, get_sentiment),
    Tool("get_sentiment_index", "读市场级情绪指数（如 fear_greed）",
         {"name": "str=fear_greed", "limit": "int<=180"}, get_sentiment_index),
    Tool("get_market_events", "读交易所公告：上币 / 下架 / 维护 / 监管",
         {"symbols": "list[str]", "lookback_hours": "int=72"}, get_market_events),
    Tool("get_prediction_market", "读预测市场赔率",
         {"topic": "str", "limit": "int<=200"}, get_prediction_market),
    Tool("get_macro", "读宏观序列（FRED 等）",
         {"series_id": "str", "limit": "int<=120"}, get_macro),
]

DATA_TOOLS = _DATA_TOOLS
