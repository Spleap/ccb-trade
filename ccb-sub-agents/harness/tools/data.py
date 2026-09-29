"""A 类工具：数据工具 —— 读"世界"（ARCHITECTURE §4.3）。

来源分两半：
  * **push 层**（DB 只读）：新闻 / 社媒 / 公告 / 预测市场 / 宏观 / 情绪指数
  * **pull 层**（按需拉）：K 线 / 指标 / 派生品指标 ——
    K 线与指标经 `ctx.candles` 注入，派生品直接打 Bitget（`derivatives.py`），都不落库

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
from harness.tools import derivatives, indicators
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


def _periods(value, default: int = 12) -> int:
    """资金费率的期数。一期 8 小时，12 期 = 4 天。服务端再夹一次。"""
    try:
        n = default if value in (None, 0) else int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, derivatives.MAX_PERIODS))


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


def get_derivatives(ctx: ToolContext, symbol: str, periods: int = 12) -> str:
    """永续合约的派生指标：OI / 资金费率（含序列）/ 标记价 / 指数价 / 基差。

    这是杠杆策略的"必看项"：**费率是持仓成本，也是多空拥挤度的直接读数；
    OI 是这个方向上有多少钱在下注。** 只看 K 线的杠杆策略是在闭着眼睛付费。

    降级策略是**分段的**，不是全有全无：OI 与价格来自一次 tickers 调用，
    拿不到就整体失败（那是硬数据）；资金费率的结算节奏与历史序列是加分项，
    单独失败时**在文本里明说哪一段取不到**，而不是悄悄少一段
    —— 少一段会让 LLM 以为"费率为 0 / 没有拥挤"。
    """
    n = _periods(periods)
    tic = derivatives.ticker(symbol)

    def num(key):                                                # Bitget 的数值全是字符串
        try:
            return float(tic[key])
        except (KeyError, TypeError, ValueError):
            return None

    last, mark, index = num("lastPrice"), num("markPrice"), num("indexPrice")
    oi, rate = num("openInterest"), num("fundingRate")
    basis = (mark - index) / index if (mark and index) else None
    oi_notional = oi * last if (oi and last) else None

    if oi is None and rate is None:
        return _empty(f"{symbol} 在 {derivatives.PERP} 没有持仓量/资金费率（可能不是永续合约）")

    # ── 加分项 1：结算节奏与下次结算时间 ──────────────────────
    notes, pace, cur = [], "", {}
    try:
        cur = derivatives.funding_now(symbol)
        if cur.get("next_ts"):
            left = (cur["next_ts"] - ctx.as_of) / 3600.0
            pace = f"，下次结算 {_utc(cur['next_ts'])}（还有 {left:.1f}h）"
        if cur.get("interval_h"):
            pace = f" / {cur['interval_h']:g}h" + pace
    except RuntimeError as exc:
        cur = {}
        notes.append(f"结算节奏取不到（{exc}）")

    # ── 加分项 2：资金费率历史序列 ────────────────────────────
    series, series_line = [], ""
    try:
        series = derivatives.funding_history(symbol, n, as_of=ctx.as_of)
    except RuntimeError as exc:
        notes.append(f"费率历史取不到（{exc}）")

    if series:
        rates = [r["rate"] for r in series]
        avg = sum(rates) / len(rates)
        pos = sum(1 for r in rates if r > 0)
        shown = ", ".join(f"{r * 100:+.4f}%" for r in rates)
        series_line = (f"近 {len(series)} 期（最早→最新）：{shown}\n"
                       f"          均值 {avg * 100:+.4f}%，正值 {pos}/{len(series)} 期")

    # ── 快照：这些数字必须被记下来，"你当时看到了什么"（§2.4）──
    ctx.record(symbol, oi=oi, oi_notional=oi_notional, funding_rate=rate,
               mark_price=mark, index_price=index, basis=basis,
               funding_next_ts=cur.get("next_ts"))

    def _yi(x):                                                  # 亿 U，给大数字用
        return "—" if x is None else f"{x / 1e8:.2f} 亿 U"

    lines = [f"{symbol} 永续（{derivatives.PERP}）", ""]
    lines.append(f"持仓量 OI：{'—' if oi is None else f'{oi:,.1f}'}"
                 f"（名义约 {_yi(oi_notional)}）")
    lines.append("          ⚠ Bitget 不提供 OI 历史序列，你看到的是**某一刻的存量**，"
                 "不能当趋势看 —— 想知道 OI 在放大还是缩小，只能靠自己多轮对比。")
    lines.append(f"资金费率：当期 {rate * 100:+.4f}%{pace}" if rate is not None
                 else "资金费率：—")
    if series_line:
        lines.append(f"          {series_line}")
    lines.append("          正值 = 多头付空头（多头拥挤、做多要付费）；"
                 "连续为负 = 空头拥挤、做空要付费。")
    lines.append(f"价格口径：标记价 {mark if mark is not None else '—'} ｜ "
                 f"指数价 {index if index is not None else '—'} ｜ "
                 f"基差 {f'{basis * 100:+.4f}%' if basis is not None else '—'}"
                 f"{'（标记 < 指数）' if basis is not None and basis < 0 else ''}")
    if last is not None:
        pct = num("price24hPcnt")
        lines.append(f"最新价 {last} ｜ 24h {f'{pct * 100:+.3f}%' if pct is not None else '—'} ｜ "
                     f"24h 成交额 {_yi(num('turnover24h'))}")
    if notes:
        lines.append("（部分数据取不到：" + "；".join(notes) + "）")

    return "\n".join(lines)


# ============================================================
# 注册
# ============================================================

_DATA_TOOLS: list[Tool] = [
    Tool("get_candles", "读某标的的 OHLCV（pull 层，只含已完结的 K 线）",
         {"symbol": "str", "tf": "str=1h", "limit": "int<=200"}, get_candles),
    Tool("get_indicators", "服务端算技术指标，不要自己算",
         {"symbol": "str", "names": "list[str] 如 rsi14,atr14,macd", "tf": "str=1h"}, get_indicators),
    Tool("get_derivatives",
         "读永续合约的派生指标：持仓量 OI、资金费率（当期 + 最近若干期）、标记价/指数价/基差。"
         "做杠杆前必看 —— 费率是持仓成本与多空拥挤度，OI 是这个方向上的下注存量。"
         "注意：OI 只有当前值，Bitget 没有 OI 历史序列。",
         {"symbol": "str", "periods": "int<=100"}, get_derivatives),
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
