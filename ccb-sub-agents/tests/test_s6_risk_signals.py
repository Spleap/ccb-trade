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

from harness import exit_plan, signals                    # noqa: E402
from harness.config import Config                          # noqa: E402
from harness.store import db, repo                         # noqa: E402
from harness.tools import decision as decision_tools       # noqa: E402
from harness.tools.base import ToolContext                 # noqa: E402

T0 = 1_700_000_000
SYM = "BTC/USDT"
MARK = 60_000.0

# 止损 5%、止盈 10% —— 距离 3000，落在默认 stop_distance_max_pct（50%）之内
PLAN = {"stop_loss": {"type": "pct", "value": 0.05},
        "take_profit": {"type": "pct", "value": 0.10}}


def make_ctx(equity: float = 1000.0, starting_equity: float = 1000.0,
             leverage: float = 1.0, w: float = 0.5, gross_cap: float = 1.0,
             mark: float = MARK, cfg: Config = Config()) -> ToolContext:
    """一个手搭的决策上下文：预算 / 权益 / 杠杆都直接给定，绕开账本。"""
    conn = db.connect(":memory:")
    repo.create_agent(conn, "a1", "测试员", "test", 3600, starting_equity, T0)
    with conn:
        repo.set_budget(conn, "a1", "t1", w, gross_cap, T0)
    return ToolContext(conn=conn, agent_id="a1", as_of=T0, cfg=cfg, tf="1h",
                       mark=lambda _sym: mark, budget=repo.get_budget(conn, "a1"),
                       equity=equity, starting_equity=starting_equity, leverage=leverage)


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
