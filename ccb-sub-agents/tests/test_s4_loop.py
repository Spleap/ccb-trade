"""S4 验收：七步 loop 跑通（ARCHITECTURE §10.3）。

验收标准原文：**一个 Agent 能自己取数、输出 `target_ratio` + `exit_plan`、落单、写决策。**

直接跑：`python tests/test_s4_loop.py`
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import agents, loop, scheduler, tools, trace   # noqa: E402
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


# ── ⑧ 观测层：思考与工具调用必须留痕 ────────────────────────


class DripClient:
    """把正文按碎片吐给 `on_text`（模拟真实流式），最后回报一个完整 reply。

    `ScriptedClient` 是一次性把整段话回调出去的，所以测不出"碎片合并"这件事。
    """

    def __init__(self, text: str, chunk: int = 4):
        self.text = text
        self.chunk = chunk

    def chat(self, messages, tools=None, temperature=0.0, on_text=None):
        if on_text:
            for i in range(0, len(self.text), self.chunk):
                on_text(self.text[i:i + self.chunk])
        return LLMReply(content=self.text)


TRADE_SCRIPT = [
    reply("先看自己的状态。", ("get_my_portfolio", {})),
    reply("取指标。", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
    reply("趋势成立。", ("propose_target", {"symbol": SYM, "ratio": 0.5,
                                          "reason": "1h 通道完整，ATR 上行", "exit_plan": ENTRY_PLAN})),
    reply("已提交。"),
]


def test_trace_records_thinking_and_tool_calls():
    """终端上滚过去的东西必须留得下来 —— 尤其是工具**返回的内容**（§4.2）。"""
    conn, spec, candles, clock = make_world()
    rec = trace.Recorder(conn)
    res = wake(conn, spec, candles, clock, ScriptedClient(list(TRADE_SCRIPT)), on_event=rec)
    assert res.status == "traded", res

    rows = repo.traces(conn, res.decision_id)
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "wake_start"
    for want in ("thinking", "text", "tool_call", "tool_result", "signal"):
        assert want in kinds, f"轨迹里少了 {want}：{kinds}"

    # seq 从 1 起、严格递增 —— 回放顺序全靠它
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["decision_id"] == res.decision_id for r in rows)
    assert all(r["agent_id"] == spec.agent_id for r in rows)

    # 关键一条：工具**拿回了什么**必须落库，否则"它当时看到了什么"无从回答
    results = [json.loads(r["payload"]) for r in rows if r["kind"] == "tool_result"]
    assert any("atr14" in json.dumps(p) for p in results), results
    calls = [json.loads(r["payload"]) for r in rows if r["kind"] == "tool_call"]
    assert any(c["name"] == "get_indicators" for c in calls), calls
    conn.close()


def test_streamed_text_is_merged_into_one_row():
    """LLM 的正文是**按碎片**回调的 —— 落库时必须合并成一段，不能一碎一碎地存几百行。"""
    conn, spec, candles, clock = make_world()
    text = "行情没什么变化，这一轮我不动。"
    rec = trace.Recorder(conn)
    res = wake(conn, spec, candles, clock, DripClient(text, chunk=2), on_event=rec)
    assert res.status == "no_action", res

    texts = [json.loads(r["payload"])["text"] for r in repo.traces(conn, res.decision_id)
             if r["kind"] == "text"]
    assert texts == [text], f"碎片没合并：{texts}"
    conn.close()


def test_trace_does_not_change_the_decision():
    """观察者是**旁路**：开着它和关着它，决策必须一字不差。"""
    def run(with_recorder: bool):
        conn, spec, candles, clock = make_world()
        kw = {"on_event": trace.Recorder(conn)} if with_recorder else {}
        res = wake(conn, spec, candles, clock, ScriptedClient(list(TRADE_SCRIPT)), **kw)
        row = dict(conn.execute("SELECT * FROM agent_decisions WHERE decision_id = ?",
                                (res.decision_id,)).fetchone())
        row.pop("decision_id")
        row.pop("ts")
        out = (res.status, row, repo.get_positions(conn, spec.agent_id),
               repo.get_fills(conn, spec.agent_id))
        conn.close()
        return out

    assert run(True) == run(False)


def test_broken_recorder_cannot_break_the_decision():
    """记录器坏了（这里是把连接关掉）也不能影响决策 —— 观察者不该有搞砸被观察者的能力。"""
    conn, spec, candles, clock = make_world()
    dead = db.connect(":memory:")
    dead.close()
    res = wake(conn, spec, candles, clock, ScriptedClient(list(TRADE_SCRIPT)),
               on_event=trace.Recorder(dead))
    assert res.status == "traded", res
    assert repo.get_position(conn, spec.agent_id, SYM) is not None
    conn.close()


def test_long_payload_is_flagged_truncated():
    """单字段超长要截断并打标记 —— 但**决策用的原文不受影响**（截断只发生在轨迹里）。"""
    conn, spec, candles, clock = make_world()
    rec = trace.Recorder(conn, cfg=Config(trace_max_chars=40))
    res = wake(conn, spec, candles, clock, ScriptedClient(list(TRADE_SCRIPT)), on_event=rec)

    big = [r for r in repo.traces(conn, res.decision_id)
           if r["kind"] == "tool_result" and r["truncated"]]
    assert big, "超长的工具返回应当被标记为已截断"
    assert "已截断" in big[0]["payload"]
    # 决策记录里的 inputs_summary 是另一条路径，不该被轨迹的截断牵连
    row = repo.get_decision(conn, res.decision_id)
    assert "get_indicators" in row["inputs_summary"]
    conn.close()


def test_render_shows_a_readable_timeline():
    conn, spec, candles, clock = make_world()
    res = wake(conn, spec, candles, clock, ScriptedClient(list(TRADE_SCRIPT)),
               on_event=trace.Recorder(conn))

    out = trace.render(conn, res.decision_id)
    assert res.decision_id in out
    assert "第 1 轮" in out and "get_indicators" in out
    assert "结局" in out and "result = accepted" in out
    # 决策在、轨迹不在（本功能上线前跑的）：要说清楚"是没有"，不能是报错也不能是空白
    conn.execute("DELETE FROM agent_trace WHERE decision_id = ?", (res.decision_id,))
    assert "没有轨迹" in trace.render(conn, res.decision_id)
    # 连决策都没有：同样得是一句人话
    assert "库里没有这条决策" in trace.render(conn, "dec-不存在")
    conn.close()


# ── 内部 ────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S4 全通过（{len(tests)} 项）")
