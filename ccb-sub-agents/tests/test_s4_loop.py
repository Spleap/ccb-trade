"""S4 验收：七步 loop 跑通（ARCHITECTURE §10.3）。

验收标准原文：**一个 Agent 能自己取数、输出 `target_ratio` + `exit_plan`、落单、写决策。**

直接跑：`python tests/test_s4_loop.py`
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import agents, loop, scheduler, tools        # noqa: E402
from harness.clock import FixedClock                     # noqa: E402
from harness.config import Config                          # noqa: E402
from harness.llm import LLMReply, ScriptedClient, reply     # noqa: E402
from harness.store import db, repo                         # noqa: E402
from harness.tools.candles import last_close               # noqa: E402

T0 = 1_700_000_000
HOUR = 3600
SYM = "BTC/USDT"
N = 60
STEP = 10.0
BASE = 60_000.0
# N 根里最后一根（index N-1）的 ts 正好 = T0，还没走完；能用的最后一根是 index N-2
MARK = BASE + (N - 2) * STEP                                # 60,580.0

SPEC_DICT = {
    "agent_id": "lin-01",
    "name": "林 · 趋势跟随",
    "persona": "你是林，一个只做中频趋势跟随的交易员，不在意一两根 K 线的噪声。",
    "universe": [SYM],
    "tf": "1h",
    "wake_interval": HOUR,
    "w": 0.25,
    "gross_cap": 0.8,
    "starting_equity": 1000.0,
    "tools": ["get_candles", "get_indicators", "get_news"],
}

ENTRY_PLAN = {"stop_loss": {"type": "atr", "value": 2.0},
              "take_profit": {"type": "atr", "value": 3.0},
              "time_stop": {"max_bars": 48}}


# ── 世界搭建 ────────────────────────────────────────────────


def bars(end_ts: int = T0, n: int = N) -> list[dict]:
    first = end_ts - (n - 1) * HOUR
    return [{"ts": first + i * HOUR, "open": BASE + i * STEP,
             "high": BASE + i * STEP + 40, "low": BASE + i * STEP - 60,
             "close": BASE + i * STEP, "volume": 100 + i} for i in range(n)]


def make_world(spec_dict: dict | None = None, with_budget: bool = True):
    conn = db.connect(":memory:")
    spec = agents.AgentSpec.from_dict(spec_dict or SPEC_DICT)
    with conn:
        agents.register(conn, spec, T0)
        if not with_budget:
            conn.execute("DELETE FROM agent_budget")
    candles = tools.ListCandleSource({SYM: bars()}, tf="1h")
    clock = FixedClock(T0)
    return conn, spec, candles, clock


def wake(conn, spec, candles, clock, llm, cfg=Config(), **kw):
    """跑一次唤醒。mark 从已完结的 K 线取 —— 与实盘同一个入口（§7.2）。"""
    return loop.run_once(
        conn, spec.agent_id, clock, llm=llm, spec=spec, candles=candles,
        mark=lambda s: last_close(candles, s, spec.tf, clock.now()), cfg=cfg, **kw)


def atr14(conn, spec, candles, clock) -> float:
    """取一次 ATR，供断言里手算止损位。"""
    from harness.tools import data as data_tools
    ctx = tools.ToolContext(conn=conn, agent_id=spec.agent_id, as_of=clock.now(),
                            candles=candles, cfg=Config(), tf=spec.tf)
    data_tools.get_indicators(ctx, SYM, names=["atr14"], tf=spec.tf)
    return ctx.snapshot[SYM]["atr14"]


# ── ① 主线：取数 -> 提案 -> 校验 -> 落单 -> 写决策 ────────────


def test_seven_step_loop_trades_and_journals():
    conn, spec, candles, clock = make_world()
    llm = ScriptedClient([
        reply("先看自己的状态。", ("get_my_portfolio", {})),
        reply("取指标。", ("get_indicators", {"symbol": SYM, "names": ["atr14", "rsi14"], "tf": "1h"})),
        reply("趋势成立。", ("propose_target", {"symbol": SYM, "ratio": 0.5,
                                              "reason": "1h 通道完整，ATR 上行", "exit_plan": ENTRY_PLAN})),
        reply("已提交，等本轮结束执行。"),
    ])
    res = wake(conn, spec, candles, clock, llm)

    assert res.status == "traded", res
    assert len(res.fills) == 1
    fill = res.fills[0]

    # 费用与滑点必须分开记（§7.3）：这是"策略赚的是价差还是被手续费吃了"的唯一依据
    assert fill["fee"] > 0 and fill["slippage"] > 0
    # 滑点永远对交易者不利：买入成交价 >= mark
    assert fill["price"] > MARK and fill["side"] == "buy"

    pos = repo.get_position(conn, spec.agent_id, SYM)
    assert pos is not None and pos["qty"] > 0
    assert pos["exit_plan"] and "stop_loss" in pos["exit_plan"], "落库的持仓必须带 exit_plan"

    # 名义额 = |ratio| × w × 权益，权益≈1000
    assert abs(fill["notional"] - 0.5 * 0.25 * 1000) < 2.0, fill["notional"]

    # ⑦ Journal：决策 + 快照
    row = conn.execute("SELECT * FROM agent_decisions WHERE decision_id = ?",
                       (res.decision_id,)).fetchone()
    assert row["result"] == "accepted"
    assert '"target_ratio"' not in row["target_ratio"] and SYM in row["target_ratio"]
    assert "get_indicators" in row["inputs_summary"]

    snap = conn.execute("SELECT * FROM market_snapshot WHERE decision_id = ?",
                        (res.decision_id,)).fetchone()
    assert snap is not None and "atr14" in snap["payload"], "K 线不落库，但必须落决策快照（§2.4）"

    # 📤 指令出口（§7.5）：产生成交就必须落一行 trade_signals，并随 WakeResult 带回终端
    assert res.signal is not None and res.signal["action"] == "open", res.signal
    sig_row = conn.execute("SELECT * FROM trade_signals WHERE signal_id = ?",
                           (res.signal["signal_id"],)).fetchone()
    assert sig_row["status"] == "emitted" and sig_row["symbol"] == SYM
    assert sig_row["decision_id"] == res.decision_id and sig_row["qty"] > 0
    assert sig_row["stop_loss"] is not None and sig_row["take_profit"] is not None, \
        "指令里必须带止盈止损，下游才能做二次风控"

    eq = repo.get_last_equity(conn, spec.agent_id)
    assert eq is not None and abs(eq["equity"] - 1000.0) < 5.0
    conn.close()


# ── ② 缺止损：协议层直接拒 ──────────────────────────────────


def test_missing_exit_plan_is_rejected():
    conn, spec, candles, clock = make_world()
    llm = ScriptedClient([
        reply("我觉得会涨。", ("propose_target", {"symbol": SYM, "ratio": 0.5,
                                                "reason": "感觉会涨"})),
        reply("算了。"),
    ])
    res = wake(conn, spec, candles, clock, llm)

    assert res.status == "rejected", res
    assert "exit_plan" in res.reason
    assert repo.get_position(conn, spec.agent_id, SYM) is None, "被拒就绝不能有仓位"
    assert repo.get_fills(conn, spec.agent_id) == []

    row = conn.execute("SELECT * FROM agent_decisions WHERE decision_id = ?",
                       (res.decision_id,)).fetchone()
    assert row["result"] == "rejected" and row["degraded_reason"]
    conn.close()


def test_missing_reason_is_rejected():
    """reason 不是装饰：面板要展示、归因要用、返佣合规要留痕（§3.4）。"""
    conn, spec, candles, clock = make_world()
    llm = ScriptedClient([
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "   ",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    res = wake(conn, spec, candles, clock, llm)
    assert res.status == "rejected" and "reason" in res.reason
    conn.close()


# ── ③ LLM 挂掉：什么都不做，而这是安全的 ────────────────────


def test_llm_failure_degrades_and_touches_nothing():
    conn, spec, candles, clock = make_world()
    res = wake(conn, spec, candles, clock, ScriptedClient([]))   # 剧本为空 -> 直接报错

    assert res.status == "degraded" and "LLM 不可用" in res.reason
    assert repo.get_position(conn, spec.agent_id, SYM) is None
    assert repo.get_fills(conn, spec.agent_id) == []

    row = conn.execute("SELECT * FROM agent_decisions WHERE decision_id = ?",
                       (res.decision_id,)).fetchone()
    assert row["result"] == "degraded" and row["degraded_reason"]
    conn.close()


def test_no_budget_blocks_opening():
    """没有预算 = 主 Agent 没给它额度。这时开仓必须被拒（§8.5）。"""
    conn, spec, candles, clock = make_world(with_budget=False)
    llm = ScriptedClient([
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "有信号",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    res = wake(conn, spec, candles, clock, llm)
    assert res.status == "rejected" and "预算" in res.reason
    conn.close()


# ── ④ 唯一出口也能用来平仓 ──────────────────────────────────


def test_ratio_zero_flattens_position():
    conn, spec, candles, clock = make_world()
    open_llm = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "做多",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    assert wake(conn, spec, candles, clock, open_llm).status == "traded"
    assert repo.get_position(conn, spec.agent_id, SYM) is not None

    close_llm = ScriptedClient([
        reply("趋势走坏了，清仓。", ("propose_target", {"symbol": SYM, "ratio": 0.0,
                                                     "reason": "1h 跌破通道下沿"})),
        reply("已清。"),
    ])
    res = wake(conn, spec, candles, clock, close_llm)
    assert res.status == "traded"
    assert repo.get_position(conn, spec.agent_id, SYM) is None, "ratio=0 必须真的清干净"
    assert conn.execute("SELECT COUNT(*) c FROM agent_fills").fetchone()["c"] == 2
    conn.close()


# ── ⑤ 止损只能收紧，不能放宽 ────────────────────────────────


def test_amend_only_tightens():
    conn, spec, candles, clock = make_world()
    open_llm = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "做多",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    wake(conn, spec, candles, clock, open_llm)
    before = repo.get_position(conn, spec.agent_id, SYM)
    stop_before = json.loads(before["exit_plan"])["stop_loss"]

    # 想放宽 —— 必须是拒绝（§9.4 防作弊 1：浮亏时"再等等"这条路不存在）
    widen = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("amend_exit_plan", {"symbol": SYM,
                                      "exit_plan": {"stop_loss": {"type": "atr", "value": 6.0}}})),
        reply("好吧。"),
    ])
    res = wake(conn, spec, candles, clock, widen)
    assert res.status == "rejected" and "放宽" in res.reason
    assert json.loads(repo.get_position(conn, spec.agent_id, SYM)["exit_plan"])["stop_loss"] == stop_before

    # 收紧 —— 必须落地，而且**不需要额外提案**（这轮它只想做这件事）
    tighten = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("amend_exit_plan", {"symbol": SYM,
                                      "exit_plan": {"stop_loss": {"type": "atr", "value": 1.0}}})),
        reply("已收紧。"),
    ])
    res = wake(conn, spec, candles, clock, tighten)
    assert res.status == "amended", res
    after = json.loads(repo.get_position(conn, spec.agent_id, SYM)["exit_plan"])["stop_loss"]
    assert after > stop_before, f"多头止损只能往上移：{stop_before} -> {after}"

    # 而且**没有 cancel_exit_plan 这个工具** —— 这个能力就不该存在
    assert "cancel_exit_plan" not in tools.full_registry().names()
    conn.close()


# ── ⑥ 止损之后的冷却期 ──────────────────────────────────────


def test_cooldown_blocks_reentry_after_stop():
    conn, spec, candles, clock = make_world()
    open_llm = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "做多",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    wake(conn, spec, candles, clock, open_llm)

    # 让 watchdog 打掉它（Loop 2 不碰 LLM），这会写进冷却期
    from harness import watchdog
    closed = watchdog.run_once(conn, clock, lambda s, ts: MARK * 0.5)   # 价格腰斩 -> 触发止损
    assert closed and closed[0]["reason"] == "stop_loss"
    assert repo.is_cooling_down(conn, spec.agent_id, clock.now())

    retry = ScriptedClient([
        reply("", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
        reply("", ("propose_target", {"symbol": SYM, "ratio": 0.5, "reason": "打回来",
                                     "exit_plan": ENTRY_PLAN})),
        reply("好。"),
    ])
    res = wake(conn, spec, candles, clock, retry)
    assert res.status == "rejected" and "冷却" in res.reason
    assert repo.get_position(conn, spec.agent_id, SYM) is None
    conn.close()


# ── ⑦ Loop 1：唤醒与重新排期 ────────────────────────────────


def test_scheduler_wakes_then_reschedules():
    conn, spec, candles, clock = make_world()
    llm = ScriptedClient([reply("今天很平静，不动。")])

    results = scheduler.run_once(conn, clock, llm, specs=[spec], candles=candles)
    assert len(results) == 1 and results[0].status == "no_action"
    assert results[0].signal is None, "没动作就不该给下游发指令（那是噪音，不是信号）"

    rt = repo.get_runtime(conn, spec.agent_id)
    assert rt["next_wake_at"] == T0 + spec.wake_interval
    assert rt["last_wake_at"] == T0, "last_wake_at 是「上次真的醒过」，不是「下次要醒」"

    # 没到点就不该再被唤醒
    assert scheduler.due_agents(conn, T0 + 1) == []
    assert [a["agent_id"] for a in scheduler.due_agents(conn, T0 + HOUR)] == [spec.agent_id]
    conn.close()


def test_scheduler_respects_concurrency_cap():
    """并发上限保护 LLM 限速；截断顺序必须是确定的，否则同样的输入跑两遍结果对不上。"""
    conn, spec, candles, clock = make_world()
    with conn:
        for i in (2, 3):
            agents.register(conn, agents.AgentSpec.from_dict(
                {**SPEC_DICT, "agent_id": f"lin-0{i}", "name": f"林{i}"}), T0)

    cfg = Config(max_concurrency=2)
    results = scheduler.run_once(conn, clock, ScriptedClient(), specs=[spec], candles=candles,
                                 cfg=cfg)
    # ScriptedClient 无剧本 -> 被唤醒的那两个是 degraded，另两个原封不动
    assert len(results) == 2
    assert len(scheduler.due_agents(conn, T0)) == 1, "被截断的要留到下一轮，不是丢掉"
    conn.close()


# ── 内部 ────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S4 全通过（{len(tests)} 项）")
