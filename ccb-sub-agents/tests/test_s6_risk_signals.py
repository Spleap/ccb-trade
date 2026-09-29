"""代码层兜底与指令出口的验收（ARCHITECTURE §9.4 / §7.5）。

盯的是两件"不能靠提示词、只能靠代码"的事：

1. **三档风控** —— 单笔最大亏损 / 止损必须紧于强平线 / 累计回撤熔断。
   它们是"不能让他亏太多"的最后一层，写错一格，提示词里写得再漂亮都是假的。
2. **交易指令出口** —— 通过 ⑤ 的决策必须变成一行 `trade_signals`，
   且上一条同标的的未覆盖指令要被标 `superseded`（下游据此判断哪条还有效）。

直接跑：`python tests/test_s6_risk_signals.py`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import agents, exit_plan, signals            # noqa: E402
from harness.config import DEFAULT, Config                 # noqa: E402
from harness.store import db, repo                         # noqa: E402
from harness.tools import decision as decision_tools       # noqa: E402
from harness.tools.base import ToolContext                 # noqa: E402
from harness.tools.candles import ListCandleSource         # noqa: E402

T0 = 1_700_000_000
SYM = "BTC/USDT"
MARK = 60_000.0

# 止损 5%、止盈 10% —— 距离 3000，落在默认 stop_distance_max_pct（50%）之内
PLAN = {"stop_loss": {"type": "pct", "value": 0.05},
        "take_profit": {"type": "pct", "value": 0.10}}


def _bars(n: int = 60) -> list[dict]:
    """n 根已完结的 1h bar。high-low 恒为 200，且跳空不超过它 -> ATR14 = 200。"""
    start = T0 - n * 3600
    return [{"ts": start + i * 3600, "open": 60_000.0 + i * 100,
             "high": 60_100.0 + i * 100, "low": 59_900.0 + i * 100,
             "close": 60_000.0 + i * 100, "volume": 10.0 + i} for i in range(n)]


def make_ctx(equity: float = 1000.0, starting_equity: float = 1000.0,
             leverage: float = 1.0, w: float = 0.5, gross_cap: float = 1.0,
             mark: float = MARK, cfg: Config = Config(),
             universe: tuple[str, ...] = (), candles=None,
             default_exit_plan: dict | None = None) -> ToolContext:
    """一个手搭的决策上下文：预算 / 权益 / 杠杆都直接给定，绕开账本。"""
    conn = db.connect(":memory:")
    repo.create_agent(conn, "a1", "测试员", "test", 3600, starting_equity, T0)
    with conn:
        repo.set_budget(conn, "a1", "t1", w, gross_cap, T0)
    return ToolContext(conn=conn, agent_id="a1", as_of=T0, cfg=cfg, tf="1h",
                       mark=lambda _sym: mark, budget=repo.get_budget(conn, "a1"),
                       equity=equity, starting_equity=starting_equity, leverage=leverage,
                       universe=universe, candles=candles,
                       default_exit_plan=default_exit_plan)


# ── 闸门 1：单笔最大亏损 ────────────────────────────────────


def test_single_trade_loss_cap_rejects_and_suggests_max_ratio():
    """ratio=1.0、止损 5% -> 触发时亏 25；上限是起始权益的 2% = 20 -> 必须拒。"""
    ctx = make_ctx()
    v = decision_tools.evaluate(ctx, SYM, 1.0, PLAN)

    assert not v["ok"], v
    assert "单笔风险超限" in v["reason"]
    # 按 5% 的止损距离，per_unit = w×equity×lev = 500，
    # |ratio| ≤ cap×mark ÷ (per_unit×dist) = 20×60000 ÷ (500×3000) = 0.8
    assert "0.8000" in v["reason"], v["reason"]
    assert ctx.proposal is None, "被拒就不能留下提案"
    ctx.conn.close()


def test_ratio_within_cap_is_accepted_and_reports_risk():
    """同样的 5% 止损，ratio=0.8 -> 触发时正好亏 20，卡在上限上（不算超）→ 放行。"""
    ctx = make_ctx()
    v = decision_tools.evaluate(ctx, SYM, 0.8, PLAN)

    assert v["ok"], v["reason"]
    assert abs(v["risk_amount"] - 20.0) < 1e-6, v["risk_amount"]
    assert abs(v["target_qty"] - 0.8 * 0.5 * 1000 / MARK) < 1e-12
    ctx.conn.close()


def test_loss_cap_scales_with_equity_not_ratio():
    """风控的上限跟的是**权益**，不是 ratio —— 权益涨了，能承担的单笔风险也该涨。"""
    ctx = make_ctx(equity=2000.0, starting_equity=2000.0)
    cap = 2000.0 * Config().max_loss_per_trade_pct         # 40
    # 名义 = 1.0×0.5×2000 = 1000 -> qty = 1000/60000 -> 止损亏 1000/60000×3000 = 50 > 40
    v = decision_tools.evaluate(ctx, SYM, 1.0, PLAN)
    assert not v["ok"] and "单笔风险超限" in v["reason"]

    # 放到 0.8 就正好 40，卡线放行
    v = decision_tools.evaluate(ctx, SYM, 0.8, PLAN)
    assert v["ok"] and abs(v["risk_amount"] - cap) < 1e-6, v
    ctx.conn.close()


# ── 闸门 2：累计回撤熔断 ────────────────────────────────────


def test_drawdown_halt_blocks_increasing_exposure():
    """权益 600 < 起始 1000 的 70% 线（700）-> 只许减仓，加仓一律拒。"""
    ctx = make_ctx(equity=600.0, starting_equity=1000.0)
    v = decision_tools.evaluate(ctx, SYM, 0.5, PLAN)

    assert not v["ok"], v
    assert "累计回撤熔断" in v["reason"], v["reason"]
    ctx.conn.close()


# ── 闸门 3：止损必须紧于强平线 ──────────────────────────────


def test_stop_wider_than_liquidation_is_rejected():
    """10x 下强平距离 ≈ 9.5%；止损放在 10% 外，价格碰止损前先强平 —— 这条止损形同虚设。"""
    plan = exit_plan.resolve({"stop_loss": {"type": "pct", "value": 0.10},
                              "take_profit": {"type": "pct", "value": 0.15}}, 1, MARK)

    ok, why = exit_plan.validate(plan, 1, MARK, leverage=10)
    assert not ok and "强平" in why, why

    # 收紧到 5% 就安全了；同样 5%，5x 的强平线在 19.5% 外，本来就够宽
    tight = exit_plan.resolve({"stop_loss": {"type": "pct", "value": 0.05},
                               "take_profit": {"type": "pct", "value": 0.15}}, 1, MARK)
    assert exit_plan.validate(tight, 1, MARK, leverage=10)[0]
    assert exit_plan.validate(plan, 1, MARK, leverage=5)[0]
    # 现货（leverage=1）没有强平线，这条校验不生效
    assert exit_plan.validate(plan, 1, MARK, leverage=1)[0]


def test_take_profit_is_mandatory():
    """每一笔都要有止盈止损：只给止损 -> 直接拒（§9.4）。"""
    plan = exit_plan.resolve({"stop_loss": {"type": "pct", "value": 0.05}}, 1, MARK)
    ok, why = exit_plan.validate(plan, 1, MARK)
    assert not ok and "缺少止盈" in why, why


# ── 品种池：创建时就定好，池外一律拒（§5.3）─────────────────


def test_universe_rejects_outside_symbol_but_still_allows_flat():
    """品种池外的品种一律不许碰 —— 提示词里写着不算数，得在协议层拦下来。"""
    ctx = make_ctx(universe=("BTC/USDT",))
    v = decision_tools.evaluate(ctx, "ETH/USDT", 0.3, PLAN)

    assert not v["ok"] and "不在你的品种池内" in v["reason"], v
    # 但清仓（ratio=0）永远放行：池子是被改小的，旧持仓必须还能退出来
    flat = decision_tools.evaluate(ctx, "ETH/USDT", 0.0, PLAN)
    assert flat["ok"] and flat["target_qty"] == 0.0, flat
    ctx.conn.close()


# ── 默认退路：LLM 省略 exit_plan 时框架套上（§5.3 / §9.4）────


def test_default_exit_plan_is_applied_when_omitted():
    """省略 exit_plan -> 套用策略的默认退路；ATR 由框架自算（无需先调 get_indicators）。"""
    src = ListCandleSource({SYM: _bars()}, tf="1h")          # ATR14 = 200
    ctx = make_ctx(candles=src, default_exit_plan={
        "stop_loss": {"type": "atr", "value": 1.0},
        "take_profit": {"type": "atr", "value": 2.0}})

    v = decision_tools.evaluate(ctx, SYM, 0.5, None)          # exit_plan 整个省略

    assert v["ok"], v["reason"]
    assert abs(v["atr"] - 200.0) < 1e-6, v["atr"]
    assert abs(v["plan"]["stop_loss"] - (MARK - 200.0)) < 1e-6, v["plan"]
    assert abs(v["plan"]["take_profit"] - (MARK + 400.0)) < 1e-6, v["plan"]
    # 框架自算的 ATR 要记进快照：复盘时"止损为什么在这个位置"才查得出来
    assert ctx.snapshot[SYM]["atr14"] == 200.0
    ctx.conn.close()


def test_no_default_exit_plan_means_exit_plan_is_mandatory():
    """没配默认退路 + 省略 exit_plan -> 拒。默认值是兜底，不是可有可无。"""
    ctx = make_ctx()
    v = decision_tools.evaluate(ctx, SYM, 0.5, None)
    assert not v["ok"] and "没有配 default_exit_plan" in v["reason"], v
    ctx.conn.close()


# ── 止损距离下限：太近 = 开仓即被打掉（§9.4）────────────────


def test_stop_distance_below_floor_is_rejected():
    """0.2% 的止损低于 0.4% 的下限 —— 这个距离里全是噪声，还会白付手续费。"""
    ctx = make_ctx(cfg=Config(stop_distance_min_pct=0.004))
    too_tight = {"stop_loss": {"type": "pct", "value": 0.002},
                 "take_profit": {"type": "pct", "value": 0.02}}
    v = decision_tools.evaluate(ctx, SYM, 0.5, too_tight)
    assert not v["ok"] and "太近" in v["reason"], v

    # 放到 0.5%（= 下限之上）就放行
    ok_plan = {"stop_loss": {"type": "pct", "value": 0.005},
               "take_profit": {"type": "pct", "value": 0.02}}
    assert decision_tools.evaluate(ctx, SYM, 0.5, ok_plan)["ok"], "低于下限才拒，之上不该拦"
    ctx.conn.close()


# ── 风险偏好 per-agent：写了的生效，没写的回落全局 ────────────


def test_risk_cfg_overrides_global_and_falls_back():
    spec = agents.AgentSpec.from_dict({
        "agent_id": "x", "name": "激进者", "persona": "p", "universe": ["BTC/USDT"],
        "max_loss_per_trade_pct": 0.05, "stop_distance_min_pct": 0.004})

    cfg = spec.risk_cfg()
    assert cfg.max_loss_per_trade_pct == 0.05, "写了的必须生效"
    assert cfg.stop_distance_min_pct == 0.004
    # 没写的回落全局默认，老配置不用改
    assert cfg.max_drawdown_halt == DEFAULT.max_drawdown_halt
    assert cfg.cooldown_after_stop == DEFAULT.cooldown_after_stop
    assert cfg.stop_distance_max_pct == DEFAULT.stop_distance_max_pct

    # 一个风控字段都没写 -> 原样返回全局那一份（不产生多余副本）
    bare = agents.AgentSpec.from_dict(
        {"agent_id": "y", "name": "裸配置", "persona": "p", "universe": []})
    assert bare.risk_cfg() is DEFAULT
    # 全局配置本身不被污染
    assert DEFAULT.max_loss_per_trade_pct == 0.02


def test_default_exit_plan_must_be_an_object():
    import pytest
    with pytest.raises(ValueError, match="default_exit_plan"):
        agents.AgentSpec.from_dict({"agent_id": "z", "name": "n", "persona": "p",
                                    "universe": [], "default_exit_plan": "2xATR"})


# ── 指令出口：落库 + 覆盖语义 ────────────────────────────────


def test_signal_is_built_emitted_and_superseded():
    conn = db.connect(":memory:")
    repo.create_agent(conn, "a1", "测试员", "test", 3600, 1000.0, T0)
    ctx = ToolContext(conn=conn, agent_id="a1", as_of=T0)

    prop = {"symbol": SYM, "target_qty": 0.01, "delta_qty": 0.01, "mark": MARK,
            "plan": {"stop_loss": 58_800.0, "take_profit": 62_000.0}, "leverage": 2.0}
    with conn:
        sig = signals.emit(conn, ctx, prop, "d1", T0, "1h 通道完整")

    assert sig["action"] == "open" and sig["side"] == "long"
    assert abs(sig["risk_amount"] - 0.01 * 1200) < 1e-9, sig["risk_amount"]
    assert sig["stop_loss"] == 58_800.0 and sig["take_profit"] == 62_000.0
    assert sig["qty"] == 0.01, "指令里的 qty 是目标持仓，不是本次买卖量"

    row = conn.execute("SELECT * FROM trade_signals WHERE signal_id = ?",
                       (sig["signal_id"],)).fetchone()
    assert row["status"] == "emitted" and row["reason"] == "1h 通道完整"
    assert "📤" in signals.format_line(sig)

    # 加仓：同一标的上一条还没被覆盖的必须标 superseded（只增不改）
    with conn:
        sig2 = signals.emit(conn, ctx, {**prop, "target_qty": 0.02, "delta_qty": 0.01},
                            "d2", T0 + 60, "突破确认")
    assert sig2["action"] == "increase"
    assert conn.execute("SELECT status FROM trade_signals WHERE signal_id = ?",
                        (sig["signal_id"],)).fetchone()["status"] == "superseded"
    assert conn.execute("SELECT COUNT(*) c FROM trade_signals").fetchone()["c"] == 2

    # 反手：方向翻转
    with conn:
        rev = signals.emit(conn, ctx, {**prop, "target_qty": -0.02, "delta_qty": -0.03},
                           "d3", T0 + 120, "跌破通道下沿，反手")
    assert rev["action"] == "reverse" and rev["side"] == "short"

    # 平仓：目标归零 -> close / flat，风险归零
    with conn:
        flat = signals.emit(conn, ctx, {**prop, "target_qty": 0.0, "delta_qty": -0.02},
                            "d4", T0 + 180, "趋势走坏")
    assert flat["action"] == "close" and flat["side"] == "flat"
    assert flat["risk_amount"] == 0.0

    # 最近指令倒序：终端展示 / 下游消费都用它
    assert [s["decision_id"] for s in repo.recent_signals(conn, "a1", 3)] == ["d4", "d3", "d2"]
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S6 全通过（{len(tests)} 项）")
