"""S5 验收：记忆层（ARCHITECTURE §10.3 / §8）。

验收标准原文：**第二次唤醒能 recall 到第一次。**

直接跑：`python tests/test_s5_memory.py`
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import agents, loop, memory, tools                 # noqa: E402
from harness.clock import FixedClock                          # noqa: E402
from harness.config import Config                               # noqa: E402
from harness.llm import LLMReply, ScriptedClient, reply          # noqa: E402
from harness.paper import ledger                                # noqa: E402
from harness.store import db, repo                              # noqa: E402
from harness.tools import data as data_tools                    # noqa: E402
from harness.tools import memory as memory_tools                # noqa: E402
from harness.tools.base import ToolContext                      # noqa: E402
from harness.tools.candles import last_close                    # noqa: E402

T0 = 1_700_000_000
HOUR = 3600
SYM = "BTC/USDT"
N = 60
STEP = 10.0
BASE = 60_000.0
MARK = BASE + (N - 2) * STEP

SPEC_DICT = {
    "agent_id": "lin-01",
    "name": "林 · 趋势跟随",
    "persona": "你是林，一个只做中频趋势跟随的交易员。",
    "universe": [SYM],
    "tf": "1h",
    "wake_interval": HOUR,
    "w": 0.25,
    "gross_cap": 0.8,
    "starting_equity": 1000.0,
    "tools": ["get_candles", "get_indicators"],
}

ENTRY_PLAN = {"stop_loss": {"type": "atr", "value": 2.0},
              "take_profit": {"type": "atr", "value": 3.0},
              "time_stop": {"max_bars": 48}}

GOOD_REFLECTION = """1. 我当时判断 1h 通道完好，可以继续持多。
2. 实际价格直接跌破了我的止损，被平掉了。
3. 差异在于我只看了通道形状，没有看成交量，突破那根的成交量是萎缩的。
4. 下次遇到通道内做多时，若最近 3 根 bar 的成交量低于 20 根均量的 0.8 倍，就先不开仓。"""


def bars(end_ts: int = T0, n: int = N) -> list[dict]:
    first = end_ts - (n - 1) * HOUR
    return [{"ts": first + i * HOUR, "open": BASE + i * STEP,
             "high": BASE + i * STEP + 40, "low": BASE + i * STEP - 60,
             "close": BASE + i * STEP, "volume": 100 + i} for i in range(n)]


def make_world(spec_dict: dict | None = None, cfg: Config | None = None):
    conn = db.connect(":memory:")
    spec = agents.AgentSpec.from_dict(spec_dict or SPEC_DICT)
    with conn:
        agents.register(conn, spec, T0)
    candles = tools.ListCandleSource({SYM: bars()}, tf="1h")
    return conn, spec, candles, FixedClock(T0), (cfg or Config())


def wake(conn, spec, candles, clock, llm, cfg=Config(), **kw):
    return loop.run_once(
        conn, spec.agent_id, clock, llm=llm, spec=spec, candles=candles,
        mark=lambda s: last_close(candles, s, spec.tf, clock.now()), cfg=cfg, **kw)


OPEN_SCRIPT = [
    reply("先看自己。", ("get_my_portfolio", {})),
    reply("取指标。", ("get_indicators", {"symbol": SYM, "names": ["atr14"], "tf": "1h"})),
    reply("看一眼收盘价。", ("get_candles", {"symbol": SYM, "tf": "1h", "limit": 20})),
    reply("趋势成立。", ("propose_target", {"symbol": SYM, "ratio": 0.5,
                                          "reason": "1h 通道完整", "exit_plan": ENTRY_PLAN})),
    reply("已提交。"),
]


def make_ctx(conn, spec, candles, clock, cfg=Config()) -> ToolContext:
    return ToolContext(conn=conn, agent_id=spec.agent_id, as_of=clock.now(),
                       candles=candles, cfg=cfg, tf=spec.tf)


# ── ① 主线：第二次唤醒能 recall 到第一次 ─────────────────────


def test_second_wake_remembers_first():
    conn, spec, candles, clock, cfg = make_world()
    assert wake(conn, spec, candles, clock, ScriptedClient(OPEN_SCRIPT), cfg).status == "traded"

    # 第二次唤醒：换一个 client，看它**收到了什么**
    second = ScriptedClient([reply("今天先不动。")])
    clock.advance_by(HOUR)
    res = wake(conn, spec, candles, clock, second, cfg)
    assert res.status == "no_action"

    user_msg = second.seen[0][1]["content"]
    assert "你的记忆" in user_msg, "① LoadContext 必须把记忆注入进去（§8.4 push）"
    assert SYM in user_msg and "accepted" in user_msg, user_msg
    assert "累计手续费" in user_msg or "胜率" in user_msg, "统计要压在前面（§8.6 第 2 条）"

    # pull 路径：它主动查也能查到
    ctx = make_ctx(conn, spec, candles, clock, cfg)
    out = memory_tools.recall(ctx, "BTC/USDT")
    assert "accepted" in out and "无匹配" not in out
    conn.close()


# ── ② A 路径：情节记忆纯代码派生 ────────────────────────────


def test_episode_is_derived_by_code_and_is_idempotent():
    conn, spec, candles, clock, cfg = make_world()
    res = wake(conn, spec, candles, clock, ScriptedClient(OPEN_SCRIPT), cfg)

    rows = repo.query_memory(conn, spec.agent_id, kinds=[memory.EPISODIC])
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_id"] == res.decision_id
    assert row["expires_at"] == T0 + cfg.memory_episodic_ttl, "老情节要退出上下文（§8.6 第 4 条）"

    content = json.loads(row["content"])
    assert content["decision"] == "accepted"
    assert content["target_ratio"] == {SYM: 0.5}
    assert content["reason"] == "1h 通道完整"
    assert content["saw"][SYM]["atr14"] > 0, "情节要记住'当时看到什么'（§2.4）"
    assert row["env_fingerprint"].startswith("atr_pct=")

    # 幂等：同一帧重放不会写第二条
    assert memory.record_episode(conn, spec.agent_id, res.decision_id, T0, cfg) is None
    assert len(repo.query_memory(conn, spec.agent_id)) == 1
    conn.close()


def test_rejected_decision_is_remembered_and_weighed_higher():
    """撞过的墙比顺手的操作更值得记（importance 更高）。"""
    conn, spec, candles, clock, cfg = make_world()
    llm = ScriptedClient([reply("我觉得会涨。", ("propose_target", {"symbol": SYM, "ratio": 0.5,
                                                                 "reason": "感觉会涨"})),
                          reply("算了。")])
    res = wake(conn, spec, candles, clock, llm, cfg)
    assert res.status == "rejected"

    rows = repo.query_memory(conn, spec.agent_id, kinds=[memory.EPISODIC])
    assert len(rows) == 1 and rows[0]["importance"] == 1.5
    assert json.loads(rows[0]["content"])["decision"] == "rejected"
    conn.close()


# ── ③ C 路径：统计纯代码聚合 ────────────────────────────────


def test_stats_are_code_aggregated_across_windows():
    conn, spec, candles, clock, cfg = make_world()
    with conn:
        ledger.apply_order(conn, spec.agent_id, SYM, 0.01, 60_000.0, T0)
        ledger.write_equity(conn, spec.agent_id, T0, {SYM: 60_000.0})
        ledger.apply_order(conn, spec.agent_id, SYM, -0.01, 59_000.0, T0 + HOUR,
                           close_reason="stop_loss")
        ledger.write_equity(conn, spec.agent_id, T0 + HOUR, {SYM: 59_000.0})
        stats = memory.update_stats(conn, spec.agent_id, T0 + HOUR, cfg)

    for window in ("all", "7d", "30d"):
        assert window in stats
    allstats = repo.get_stats(conn, spec.agent_id, "all")
    assert allstats["fills"] == 2.0
    assert allstats["stop_outs"] == 1.0
    assert allstats["fee_paid"] > 0 and allstats["slippage_paid"] > 0
    assert allstats["max_drawdown"] > 0 and allstats["return"] < 0
    assert "win_rate" not in allstats, "只有止损没有止盈时，胜率是 0/1 —— 不给一个会误导的数"
    conn.close()


# ── ④ B 路径：反思的闸门 ────────────────────────────────────


def test_gate_accepts_concrete_action():
    ok, why = memory.gate_reflection(GOOD_REFLECTION)
    assert ok, why


def test_gate_rejects_vague_and_rule_changing_reflections():
    """§8.3 的原例句：这不是学习，这是废话占位。"""
    vague = "1. 我以为会涨。\n2. 结果跌了。\n3. 差异是我的判断错了。\n4. 市场很难预测，要更加谨慎。"
    ok, why = memory.gate_reflection(vague)
    assert not ok and ("废话" in why or "预测" in why), why

    rule = GOOD_REFLECTION.replace(
        "就先不开仓", "就先放宽止损等它回来")
    ok, why = memory.gate_reflection(rule)
    assert not ok and "放宽" in why

    short = "1. 涨了。\n2. 跌了。\n3. 不一样。\n4. 再等等看。"
    ok, why = memory.gate_reflection(short)
    assert not ok, "第 4 问没有可执行动作就该丢"

    truncated = "1. 我以为会涨。\n2. 跌了。\n3. 判断错了。"
    assert not memory.gate_reflection(truncated)[0], "四问没答全就该丢"


def test_reflection_triggered_after_stop_and_written():
    conn, spec, candles, clock, cfg = make_world()
    # 造一次止损平仓：这是"最值得复盘"的触发点
    with conn:
        ledger.apply_order(conn, spec.agent_id, SYM, 0.01, 60_000.0, T0)
        ledger.apply_order(conn, spec.agent_id, SYM, -0.01, 58_000.0, T0 + HOUR,
                           close_reason="stop_loss")
        ledger.write_equity(conn, spec.agent_id, T0 + HOUR, {SYM: 58_000.0})

    trigger = memory.reflection_trigger(conn, spec.agent_id, T0 + HOUR, cfg)
    assert trigger and "止损" in trigger, trigger

    llm = ScriptedClient([LLMReply(content=GOOD_REFLECTION)])
    with conn:
        out = memory.maybe_reflect(conn, spec.agent_id, T0 + HOUR, llm=llm, cfg=cfg)
    assert out["written"], out

    rows = repo.query_memory(conn, spec.agent_id, kinds=[memory.REFLECTION])
    assert len(rows) == 1
    assert "成交量" in rows[0]["content"]
    assert SYM in (rows[0]["tags"] or "")

    # 反思是"过闸门才入库"的：给一个废话答案，一条都不许写
    conn2, spec2, _, clock2, cfg2 = make_world()
    with conn2:
        ledger.apply_order(conn2, spec2.agent_id, SYM, -0.01, 60_000.0, T0,
                           close_reason="stop_loss")
        out = memory.maybe_reflect(conn2, spec2.agent_id, T0, cfg=cfg2,
                                   llm=ScriptedClient([LLMReply(content="嗯，学到了很多。")]))
    assert not out["written"] and "闸门" in out["reason"]
    assert repo.query_memory(conn2, spec2.agent_id, kinds=[memory.REFLECTION]) == []
    conn.close()
    conn2.close()


def test_reflection_not_written_when_llm_is_down():
    conn, spec, candles, clock, cfg = make_world()
    with conn:
        ledger.apply_order(conn, spec.agent_id, SYM, 0.01, 60_000.0, T0)
        ledger.apply_order(conn, spec.agent_id, SYM, -0.01, 58_000.0, T0 + HOUR,
                           close_reason="stop_loss")
        out = memory.maybe_reflect(conn, spec.agent_id, T0 + HOUR,
                                   llm=ScriptedClient([]), cfg=cfg)
    assert not out["written"] and "LLM 不可用" in out["reason"]
    conn.close()


# ── ⑤ 读取：注入的配额与市场状态提醒 ────────────────────────


def test_injection_warns_on_different_regime():
    conn, spec, candles, clock, cfg = make_world()
    with conn:
        repo.insert_memory(conn, "m1", spec.agent_id, T0, memory.EPISODIC,
                           "early days", env_fingerprint="atr_pct=1.00", importance=1.0)
    now = T0 + HOUR
    block = memory.inject_block(conn, spec.agent_id, now, cur_fingerprint="atr_pct=0.10")
    assert "不同的市场状态" in block

    calm = memory.inject_block(conn, spec.agent_id, now, cur_fingerprint="atr_pct=1.20")
    assert "不同的市场状态" not in calm, "差异不到 2 倍就不该报警，否则每条都挂等于没挂"
    conn.close()


def test_injection_respects_char_cap():
    conn, spec, candles, clock, cfg = make_world()
    cfg = Config(memory_inject_chars=200)
    with conn:
        for i in range(10):
            repo.insert_memory(conn, f"m{i}", spec.agent_id, T0 + i, memory.EPISODIC,
                               "长" * 200, importance=1.0)
    block = memory.inject_block(conn, spec.agent_id, T0 + 20, cfg=cfg)
    assert len(block) < 400 and "已截断" in block, "记忆不能挤掉行情本身（§8.4）"
    conn.close()


def test_expired_episodes_leave_context_but_stay_in_stats():
    conn, spec, candles, clock, cfg = make_world()
    with conn:
        repo.insert_memory(conn, "old", spec.agent_id, T0, memory.EPISODIC, "老记忆",
                           expires_at=T0 + 10)
    fresh = memory.inject_block(conn, spec.agent_id, T0 + 5)
    stale = memory.inject_block(conn, spec.agent_id, T0 + 50)
    assert "老记忆" in fresh
    assert stale is None or "老记忆" not in stale, "过期情节退出上下文（§8.6 第 4 条）"
    # 但记录还在库里，仍然参与统计聚合
    assert repo.query_memory(conn, spec.agent_id, kinds=[memory.EPISODIC], limit=10)
    conn.close()


def test_recall_says_so_when_nothing_matches():
    conn, spec, candles, clock, cfg = make_world()
    wake(conn, spec, candles, clock, ScriptedClient(OPEN_SCRIPT), cfg)
    ctx = make_ctx(conn, spec, candles, clock, cfg)

    hit = memory_tools.recall(ctx, "BTC", k=5)
    assert "BTC/USDT" in hit

    miss = memory_tools.recall(ctx, "zzz-nothing-here", k=5)
    assert "无匹配记忆" in miss, "匹配不到就明说，返回一堆不相关的比返回空更糟"
    conn.close()


# ── ⑥ 生命周期：衰减与淘汰 ──────────────────────────────────


def test_decay_prunes_weakest_and_is_deterministic():
    conn, spec, candles, clock, cfg = make_world()
    cfg = Config(memory_max_rows=5)
    with conn:
        for i in range(8):
            repo.insert_memory(conn, f"m{i}", spec.agent_id, T0 + i, memory.EPISODIC,
                               f"第 {i} 条", importance=1.0 + i)
    with conn:
        out = memory.decay_and_prune(conn, spec.agent_id, T0 + 100, cfg)
    assert out["pruned"] == 3
    left = repo.query_memory(conn, spec.agent_id, limit=10)
    assert len(left) == 5
    assert "m0" not in [r["memory_id"] for r in left], "最弱（最重要度最低 + 最老）的先走"

    # 重算 score 不改内容，原始记录始终留着（§8.7 只追加不修改）
    assert all(r["content"].startswith("第") for r in left)
    conn.close()


# ── 内部 ────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S5 全通过（{len(tests)} 项）")
