"""S3 验收：工具返回带上限、失败返回哨兵、行情自动落快照（ARCHITECTURE §10.3）。

直接跑：`python tests/test_s3_tools.py`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import tools                                   # noqa: E402
from harness.config import Config                            # noqa: E402
from harness.tools import data as data_tools                 # noqa: E402
from harness.tools import derivatives                        # noqa: E402
from harness.tools.base import Tool, ToolContext, unavailable  # noqa: E402
from harness.store import db, repo                           # noqa: E402

T0 = 1_700_000_000
SYM = "BTC/USDT"
HOUR = 3600


def make_bars(end_ts: int, n: int, step: float = 10.0) -> list[dict]:
    """n 根 1h bar：**最后一根的 ts 正好等于 end_ts**，也就是在 end_ts 这一刻它还没走完。"""
    start = end_ts - (n - 1) * HOUR
    base = 60_000.0
    return [{"ts": start + i * HOUR, "open": base + i * step, "high": base + i * step + 50,
             "low": base + i * step - 50, "close": base + i * step, "volume": 100 + i}
            for i in range(n)]


def last_closed_close(end_ts: int, n: int, step: float = 10.0) -> float:
    """n 根里能用的最后一根 = 倒数第二根。"""
    return 60_000.0 + (n - 2) * step


def make_ctx(candles=None, as_of: int = T0, cfg: Config | None = None) -> ToolContext:
    conn = db.connect(":memory:")
    repo.create_agent(conn, "a1", "测试员", "test", 3600, 1000.0, T0)
    return ToolContext(conn=conn, agent_id="a1", as_of=as_of, candles=candles,
                       cfg=cfg or Config())


# ── 上限：行数夹紧 + 字符硬截断 ──────────────────────────────


def test_row_limit_is_clamped_server_side():
    ctx = make_ctx()
    _seed_news(ctx.conn, 60)
    out = data_tools.get_news(ctx, lookback_hours=24, limit=9999)
    assert "最近 50 条" in out, "limit 必须被服务端夹到硬上限 50"
    ctx.conn.close()


def test_char_limit_hard_truncates():
    ctx = make_ctx(cfg=Config(tool_max_chars=120))
    _seed_news(ctx.conn, 10)
    out = tools.full_registry().run("get_news", ctx, lookback_hours=24, limit=10)
    assert len(out) < 300 and "已截断" in out, "超长返回必须被硬截断"
    ctx.conn.close()


# ── 哨兵：失败必须说话，且与"没数据"分开 ────────────────────


def test_failure_returns_sentinel_and_is_logged():
    def boom(ctx):
        raise RuntimeError("源挂了")

    ctx = make_ctx()
    registry = tools.Registry([Tool("boom", "总失败", {}, boom)])
    out = registry.run("boom", ctx)
    assert tools.is_unavailable(out) and "源挂了" in out
    assert ctx.failures(), "绝不静默失败：失败必须进调用日志（§3.5）"
    assert not ctx.calls[0]["ok"]
    ctx.conn.close()


def test_empty_result_is_not_a_failure():
    """「过去 24h 没新闻」是有效信息，不是数据源故障 —— 前提是采集源本身是活的。"""
    ctx = make_ctx()
    _seed_health(ctx.conn, "google_news", T0 - 60)      # 源健康 -> 空就是真的空
    out = data_tools.get_news(ctx, lookback_hours=24)
    assert not tools.is_unavailable(out), out
    assert "无数据" in out
    assert ctx.failures() == []
    ctx.conn.close()


def test_dead_source_is_not_reported_as_quiet():
    """采集中断 ≠ 今天很平静 —— 这两句必须长得完全不一样（§2.2）。

    这是最危险的一种静默失败：源停了三天，LLM 读到"没有新闻"，
    会把它解释成"市场很安静"然后放心下单。
    """
    ctx = make_ctx()
    for name in ("cryptocurrency_cv", "google_news", "google_news_global"):
        _seed_health(ctx.conn, name, T0 - 24 * 3600)    # 一整天没成功过
    out = data_tools.get_news(ctx, lookback_hours=24)

    assert "数据源停摆" in out, out
    assert "无数据" not in out, "停摆不能被说成'无数据'，那是两种相反的事实"
    assert not tools.is_unavailable(out), "这不是取数失败，是数据不可信 —— 三者互不相同"
    ctx.conn.close()


def test_partially_dead_source_is_not_declared_down():
    """三个新闻源里活了一个，就不能断言'数据断了' —— 空更可能是这轮确实没事。"""
    ctx = make_ctx()
    _seed_health(ctx.conn, "google_news", T0 - 60)
    _seed_health(ctx.conn, "cryptocurrency_cv", T0 - 24 * 3600)

    out = data_tools.get_news(ctx, lookback_hours=24)
    assert "数据源停摆" not in out and "无数据" in out, out
    ctx.conn.close()


def test_table_without_a_collector_says_so():
    """交易所公告还没有采集源 —— 必须如实说'尚未接入'，不许假装'没有事件'。"""
    ctx = make_ctx()
    out = data_tools.get_market_events(ctx)
    assert "尚未接入采集" in out and "无数据" not in out, out
    ctx.conn.close()


def test_unknown_tool_returns_sentinel():
    ctx = make_ctx()
    out = tools.full_registry().run("get_orderbook", ctx)
    assert tools.is_unavailable(out) and "未知工具" in out
    ctx.conn.close()


# ── 防前视：as_of 是硬上界 ──────────────────────────────────


def test_news_respects_as_of():
    ctx = make_ctx(as_of=T0)
    _seed_news(ctx.conn, 3, ts=T0 - 60, prefix="窗口内")            # 窗口内
    _seed_news(ctx.conn, 4, ts=T0 + 60, prefix="未来")              # 决策之后才写入的
    out = data_tools.get_news(ctx, lookback_hours=24, limit=50)
    assert "窗口内" in out
    assert "未来" not in out, "绝不能让 LLM 看到决策之后才发生的事"
    ctx.conn.close()


def test_candles_exclude_unfinished_bar():
    ctx = make_ctx(candles=tools.ListCandleSource({SYM: make_bars(T0, 10)}, tf="1h"))
    bars = ctx.candles.fetch(SYM, "1h", 100, T0)

    assert len(bars) == 9, "10 根里最后一根还没走完，只能用 9 根"
    assert bars[-1]["ts"] + HOUR <= T0, "最后一根未完结的必须被剔除（§7.2）"
    assert tools.last_close(ctx.candles, SYM, "1h", T0) == last_closed_close(T0, 10)

    out = data_tools.get_candles(ctx, SYM, tf="1h")
    assert not tools.is_unavailable(out), out
    assert f"C{last_closed_close(T0, 10):.6g}" in out
    assert ctx.snapshot[SYM]["last_close"] == last_closed_close(T0, 10)
    assert ctx.snapshot[SYM]["bars_seen"] == 9
    ctx.conn.close()


# ── 指标：服务端算，并落进快照 ──────────────────────────────


def test_indicators_are_computed_and_recorded():
    ctx = make_ctx(candles=tools.ListCandleSource({SYM: make_bars(T0, 60)}, tf="1h"))
    out = data_tools.get_indicators(ctx, SYM, names=["rsi14", "atr14", "macd"], tf="1h")
    assert not tools.is_unavailable(out), out
    snap = ctx.snapshot[SYM]
    assert isinstance(snap["rsi14"], float) and 0 <= snap["rsi14"] <= 100
    assert snap["atr14"] > 0
    assert set(snap["macd"]) == {"macd", "signal", "hist"}

    # 样本不足时给的是"无数据"，不是崩溃
    short = make_ctx(candles=tools.ListCandleSource({SYM: make_bars(T0, 3)}, tf="1h"))
    out = data_tools.get_indicators(short, SYM, names=["rsi14"], tf="1h")
    assert not tools.is_unavailable(out) and "无数据" in out
    ctx.conn.close()
    short.conn.close()


def test_snapshot_is_flushed_to_db():
    ctx = make_ctx(candles=tools.ListCandleSource({SYM: make_bars(T0, 60)}, tf="1h"))
    data_tools.get_indicators(ctx, SYM, names=["rsi14", "atr14"], tf="1h")
    data_tools.get_candles(ctx, SYM, tf="1h")

    snapshot_id = tools.flush_snapshot(ctx, decision_id="d1")
    assert snapshot_id
    row = ctx.conn.execute("SELECT * FROM market_snapshot WHERE snapshot_id = ?",
                           (snapshot_id,)).fetchone()
    assert row["agent_id"] == "a1" and row["decision_id"] == "d1"
    assert SYM in row["payload"] and "rsi14" in row["payload"]
    assert tools.flush_snapshot(make_ctx()) is None, "没读到东西就不该产生快照行"
    ctx.conn.close()


# ── 派生品指标：OI 与资金费率（只能来自永续合约）──────────────

_TICKERS = [{"lastPrice": "83840.7", "markPrice": "83836.5", "indexPrice": "83880.8",
             "fundingRate": "0.000005", "openInterest": "31451.6",
             "price24hPcnt": "0.00354", "turnover24h": "2603534020.0"}]
_FUND_NOW = [{"fundingRate": "0.000005", "fundingRateInterval": "8",
              "nextUpdate": str((T0 + 3 * 3600) * 1000)}]
# 接口给的是**倒序**，且故意混进一根"未来"的结算 —— 两者都必须被纠正/剔除
_FUND_HIST = [{"fundingTime": str((T0 + 8 * 3600) * 1000), "fundingRate": "0.0009"},
              {"fundingTime": str(T0 * 1000), "fundingRate": "-0.0002"},
              {"fundingTime": str((T0 - 8 * 3600) * 1000), "fundingRate": "0.0001"}]


def _patch(routes: dict):
    """把 HTTP 层换成预置数据。routes: path -> data 或要抛的异常。"""
    orig = derivatives._get_json

    def fake(path, params, timeout=10.0):
        if path not in routes:
            raise AssertionError(f"测试没有预置这个请求：{path}")
        value = routes[path]
        if isinstance(value, Exception):
            raise value
        return value

    derivatives._get_json = fake
    return orig


def test_derivatives_formats_records_and_admits_oi_has_no_history():
    ctx = make_ctx()
    orig = _patch({"/api/v3/market/tickers": _TICKERS,
                   "/api/v2/mix/market/current-fund-rate": _FUND_NOW,
                   "/api/v2/mix/market/history-fund-rate": _FUND_HIST})
    try:
        out = data_tools.get_derivatives(ctx, SYM)
    finally:
        derivatives._get_json = orig

    assert not tools.is_unavailable(out), out
    assert "持仓量 OI" in out and "31,451.6" in out
    # ★ 这条是防"OI 历史陷阱"的行为保证：不许让 LLM 以为它看到的是趋势
    assert "不提供 OI 历史序列" in out, out
    # 结算节奏
    assert "下次结算" in out and "还有 3.0h" in out

    # 序列：倒序被排回升序，未来那根（+0.0900%）被剔除
    assert "+0.0100%, -0.0200%" in out, out
    assert "0.0900" not in out, "决策之后才结算的费率绝不能出现在上下文里"
    assert "均值 -0.0050%" in out

    # 基差 = (标记 - 指数) / 指数
    assert "-0.0528%" in out and "标记 < 指数" in out

    # 数字必须落进快照 —— "你当时看到了什么"
    snap = ctx.snapshot[SYM]
    assert snap["oi"] == 31451.6 and snap["funding_rate"] == 0.000005
    assert snap["mark_price"] == 83836.5 and snap["index_price"] == 83880.8
    assert snap["funding_next_ts"] == T0 + 3 * 3600
    ctx.conn.close()


def test_derivatives_degrade_visibly_not_silently():
    """tickers 是硬数据（挂了就整体失败）；费率序列是加分项（挂了要明说，不许静默少一段）。"""
    ctx = make_ctx()
    orig = _patch({"/api/v3/market/tickers": _TICKERS,
                   "/api/v2/mix/market/current-fund-rate": RuntimeError("boom"),
                   "/api/v2/mix/market/history-fund-rate": RuntimeError("boom")})
    try:
        out = data_tools.get_derivatives(ctx, SYM)
    finally:
        derivatives._get_json = orig

    assert not tools.is_unavailable(out), "加分项挂了不该让整个工具失败"
    assert "持仓量 OI" in out, "OI 是硬数据，必须还在"
    assert "部分数据取不到" in out and "取不到" in out, "少一段必须说出来，不能静默"
    ctx.conn.close()


def test_derivatives_sentinel_when_no_perp():
    """tickers 拿不到（比如给了一个没有永续的符号）→ 走哨兵，不是空字符串。"""
    ctx = make_ctx()
    orig = _patch({"/api/v3/market/tickers": RuntimeError("没有这个品种")})
    try:
        out = tools.full_registry().run("get_derivatives", ctx, symbol=SYM)
    finally:
        derivatives._get_json = orig

    assert tools.is_unavailable(out) and "没有这个品种" in out
    assert ctx.failures(), "绝不静默失败（§3.5）"
    ctx.conn.close()


def test_funding_history_sorted_ascending_and_clamped():
    orig = _patch({"/api/v2/mix/market/history-fund-rate": _FUND_HIST})
    try:
        rows = derivatives.funding_history(SYM, 12, as_of=T0)
    finally:
        derivatives._get_json = orig
    assert [r["ts"] for r in rows] == [T0 - 8 * 3600, T0], "必须排回升序"
    assert [r["rate"] for r in rows] == [0.0001, -0.0002]

    # 服务端再夹一次期数，别信 LLM 填的
    assert data_tools._periods(9999) == derivatives.MAX_PERIODS
    assert data_tools._periods(None) == 12 and data_tools._periods(0) == 12


def test_registry_is_prunable_by_strategy():
    ctx = make_ctx()
    lean = tools.full_registry().subset(["get_candles", "get_indicators"])
    assert lean.names() == ["get_candles", "get_indicators"]
    assert tools.is_unavailable(lean.run("get_news", ctx))
    ctx.conn.close()


def test_tool_spec_is_valid_json_schema():
    """工具签名必须是合法的 JSON Schema —— 它是 LLM 唯一能看到的"说明书"。"""
    spec = {t["name"]: t for t in tools.full_registry().spec()}
    candles = spec["get_candles"]["parameters"]
    assert candles["type"] == "object"
    assert candles["properties"]["tf"]["default"] == "1h"
    assert candles["properties"]["limit"]["maximum"] == 200
    assert candles["required"] == ["symbol"], "有默认值的参数不该进 required"

    news = spec["get_news"]["parameters"]
    assert news["properties"]["limit"]["maximum"] == 50
    assert news["properties"]["symbol"]["type"] == "string"

    names = spec["get_indicators"]["parameters"]["properties"]["names"]
    assert names["type"] == "array" and names["items"]["type"] == "string"

    # 嵌套对象（exit_plan）与简写可以混用。
    # exit_plan 本身**不在 required 里**：可省略，省略即用策略的 default_exit_plan。
    entry = spec["propose_target"]["parameters"]
    assert entry["properties"]["exit_plan"]["type"] == "object"
    assert entry["properties"]["exit_plan"]["required"] == ["stop_loss", "take_profit"]
    assert "exit_plan" not in entry["required"]
    assert entry["required"] == ["symbol", "ratio", "reason"]

    # 改止损必须给新计划，所以它那支是必填
    assert "exit_plan" in spec["amend_exit_plan"]["parameters"]["required"]

    assert "cancel_exit_plan" not in spec, "这个能力就不该存在（§9.4）"


def test_bitget_symbol_covers_mainstream_non_crypto():
    """品种池要能放美股/指数/黄金/外汇 —— 它们的 Bitget 符号命名不规律，必须逐条对上。"""
    from harness.tools.candles import bitget_symbol as b

    # 加密：三种写法都要落到同一个永续符号
    assert b("BTC/USDT") == b("BTC-USD") == b("BTC") == "BTCUSDT"
    assert b("ETH-USDT") == "ETHUSDT"

    # 美股/指数/贵金属：按 `X/USDT` 写就命中
    assert b("AAPL/USDT") == "AAPLUSDT"
    assert b("NVDA") == "NVDAUSDT"
    assert b("SPX/USDT") == "SPXUSDT"
    assert b("NDX100/USDT") == "NDX100USDT"
    assert b("XAU/USDT") == "XAUUSDT"

    # 这几个是通用规则会算错的：`EUR/USD` 会被补成并不存在的 `EURUSDT`
    assert b("EUR/USD") == b("EURUSD") == "EURUSDUSDT"
    assert b("GBP/USD") == "GBPUSDUSDT"
    assert b("XAUUSD") == b("GOLD") == "XAUUSDT"


# ── 内部：灌数据 ────────────────────────────────────────────


def _seed_news(conn, n: int, ts: int | None = None, prefix: str = "窗口内") -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO news_items (source, external_id, ts, title, summary, symbols) "
            "VALUES ('test', ?, ?, ?, '', ?)",
            (f"{prefix}-{i}", (ts if ts is not None else T0 - 60 - i), f"{prefix}标题 {i}",
             '["BTC/USDT"]'),
        )


def _seed_health(conn, source: str, last_ok_ts: int | None) -> None:
    """灌一行采集源健康度 —— 决定读工具把"空"解释成"平静"还是"停摆"。"""
    conn.execute(
        "INSERT INTO source_health (source, last_ok_ts, consecutive_failures) VALUES (?, ?, 0) "
        "ON CONFLICT(source) DO UPDATE SET last_ok_ts = excluded.last_ok_ts",
        (source, last_ok_ts),
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S3 全通过（{len(tests)} 项）")
