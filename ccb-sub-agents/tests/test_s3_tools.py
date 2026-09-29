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
    """「过去 24h 没新闻」是有效信息，不是数据源故障。混为一谈会让系统放弃本 tick。"""
    ctx = make_ctx()
    out = data_tools.get_news(ctx, lookback_hours=24)
    assert not tools.is_unavailable(out), out
    assert "无数据" in out
    assert ctx.failures() == []
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

    # 嵌套对象（exit_plan）与简写可以混用
    entry = spec["propose_target"]["parameters"]
    assert entry["properties"]["exit_plan"]["type"] == "object"
    assert entry["properties"]["exit_plan"]["required"] == ["stop_loss", "take_profit"]
    assert "exit_plan" in entry["required"]
    assert entry["required"] == ["symbol", "ratio", "reason", "exit_plan"]

    assert "cancel_exit_plan" not in spec, "这个能力就不该存在（§9.4）"


# ── 内部：灌数据 ────────────────────────────────────────────


def _seed_news(conn, n: int, ts: int | None = None, prefix: str = "窗口内") -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO news_items (source, external_id, ts, title, summary, symbols) "
            "VALUES ('test', ?, ?, ?, '', ?)",
            (f"{prefix}-{i}", (ts if ts is not None else T0 - 60 - i), f"{prefix}标题 {i}",
             '["BTC/USDT"]'),
        )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S3 全通过（{len(tests)} 项）")
