"""S1 验收：手工灌一笔单，权益曲线算得对（ARCHITECTURE §10.3）。

直接跑：`python tests/test_s1_ledger.py`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import exit_plan                      # noqa: E402
from harness.paper import ledger                   # noqa: E402
from harness.store import db, repo                 # noqa: E402

T0 = 1_700_000_000


def fresh():
    conn = db.connect(":memory:")
    repo.create_agent(conn, "a1", "测试员", "test", 3600, 1000.0, T0)
    return conn


def test_fee_and_slippage_are_separated():
    """§7.3：fee 与 slippage 必须分开记，否则算不清赚的是价差还是被手续费吃了。"""
    conn = fresh()
    fill = ledger.apply_order(conn, "a1", "BTC/USDT", 0.01, 60_000.0, T0)

    assert abs(fill["price"] - 60_012.0) < 1e-9, "买入必须吃滑点（对交易者不利）"
    assert fill["fee"] > 0 and fill["slippage"] > 0
    assert abs(fill["slippage"] - 0.12) < 1e-9          # 0.01 × 12
    assert abs(fill["fee"] - 600.12 * 0.0006) < 1e-9

    pos = repo.get_position(conn, "a1", "BTC/USDT")
    assert abs(pos["qty"] - 0.01) < 1e-12
    assert abs(pos["avg_price"] - 60_012.0) < 1e-9

    snap = ledger.mark_to_market(conn, "a1", {"BTC/USDT": 60_000.0})
    expected = 1000.0 - fill["fee"] - fill["slippage"]
    assert abs(snap["equity"] - expected) < 1e-6, (snap, expected)
    assert snap["missing"] == []
    conn.close()


def test_equity_curve_is_written():
    conn = fresh()
    ledger.apply_order(conn, "a1", "BTC/USDT", 0.01, 60_000.0, T0)
    ledger.write_equity(conn, "a1", T0, {"BTC/USDT": 61_000.0})

    last = repo.get_last_equity(conn, "a1")
    assert abs(last["positions_value"] - 610.0) < 1e-9   # 0.01 × 61000
    assert abs(last["equity"] - (last["cash"] + last["positions_value"])) < 1e-9
    conn.close()


def test_min_notional_blocks_growth_never_blocks_exit():
    conn = fresh()
    try:
        ledger.apply_order(conn, "a1", "BTC/USDT", 0.0001, 60_000.0, T0)   # ≈ 6 U
        raise AssertionError("敞口变大且不足最小额，应当被拒")
    except ledger.OrderRejected:
        pass

    ledger.apply_order(conn, "a1", "BTC/USDT", 0.01, 60_000.0, T0)
    ledger.apply_order(conn, "a1", "BTC/USDT", -0.0099, 60_000.0, T0 + 1)  # 剩 ≈ 6 U
    pos = repo.get_position(conn, "a1", "BTC/USDT")
    assert abs(pos["qty"] - 0.0001) < 1e-12, "减仓绝不能被最小下单额挡住"
    conn.close()


def test_short_and_flip():
    conn = fresh()
    ledger.apply_order(conn, "a1", "BTC/USDT", -0.01, 60_000.0, T0)
    pos = repo.get_position(conn, "a1", "BTC/USDT")
    assert pos["qty"] < 0
    assert abs(pos["avg_price"] - 59_988.0) < 1e-9, "卖出必须吃低价"
    assert repo.get_agent(conn, "a1")["cash"] > 1000.0, "卖出应有现金入账"

    snap = ledger.mark_to_market(conn, "a1", {"BTC/USDT": 59_000.0})
    assert snap["equity"] > 1000.0, "做空遇跌应赚"

    # 反手：净头寸变多，成本基准与 peak 全部重来
    ledger.apply_order(conn, "a1", "BTC/USDT", 0.02, 60_000.0, T0 + 1)
    pos = repo.get_position(conn, "a1", "BTC/USDT")
    assert pos["qty"] > 0 and abs(pos["qty"] - 0.01) < 1e-12
    assert abs(pos["avg_price"] - 60_012.0) < 1e-9
    assert abs(pos["peak_price"] - 60_012.0) < 1e-9
    conn.close()


def test_exit_plan_resolve_validate():
    plan = exit_plan.resolve(
        {"stop_loss": {"type": "pct", "value": 0.02},
         "take_profit": {"type": "pct", "value": 0.04},
         "trailing": {"mult": 1.5},
         "time_stop": {"max_bars": 48}},
        side=1, entry_price=60_000.0, atr=400.0, bar_seconds=3600,
    )
    assert abs(plan["stop_loss"] - 58_800.0) < 1e-9
    assert abs(plan["take_profit"] - 62_400.0) < 1e-9
    assert abs(plan["trailing_dist"] - 600.0) < 1e-9     # 1.5 × 400
    assert plan["time_stop_seconds"] == 48 * 3600
    ok, why = exit_plan.validate(plan, 1, 60_000.0)
    assert ok, why

    # 无止损 -> 拒（"做交易就必须做好止盈止损"）
    ok, why = exit_plan.validate(exit_plan.resolve({}, 1, 60_000.0), 1, 60_000.0)
    assert not ok and "止损" in why

    # 方向反了 -> 拒
    bad = exit_plan.resolve({"stop_loss": {"type": "price", "value": 61_000.0}}, 1, 60_000.0)
    assert not exit_plan.validate(bad, 1, 60_000.0)[0]

    # 距离过大 -> 拒（防止"名义上设了但形同虚设"）
    far = exit_plan.resolve({"stop_loss": {"type": "pct", "value": 0.8}}, 1, 60_000.0)
    assert not exit_plan.validate(far, 1, 60_000.0)[0]


def test_amend_only_tightens():
    plan = exit_plan.resolve({"stop_loss": {"type": "pct", "value": 0.05}}, 1, 60_000.0)

    tighter = exit_plan.amend(plan, {"stop_loss": {"type": "pct", "value": 0.02}}, 1, 60_000.0)
    assert tighter["stop_loss"] > plan["stop_loss"], "收紧应当放行"

    for patch, label in [({"stop_loss": {"type": "pct", "value": 0.10}}, "放宽"),
                         ({}, "撤销")]:
        try:
            exit_plan.amend(plan, patch, 1, 60_000.0)
            raise AssertionError(f"{label}止损必须被拒")
        except exit_plan.PlanError:
            pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S1 全通过（{len(tests)} 项）")
