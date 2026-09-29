"""S2 验收：手工造一笔带止损的仓位，价格穿越时被自动平掉 —— **全程不涉及 LLM**（§10.3）。

直接跑：`python tests/test_s2_watchdog.py`
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from harness import exit_plan, watchdog              # noqa: E402
from harness.clock import FixedClock               # noqa: E402
from harness.paper import ledger                     # noqa: E402
from harness.store import db, repo                   # noqa: E402

T0 = 1_700_000_000
SYM = "BTC/USDT"


def setup():
    conn = db.connect(":memory:")
    clock = FixedClock(T0)
    repo.create_agent(conn, "a1", "测试员", "test", 3600, 1000.0, T0)
    return conn, clock


def make_price_fn(prices: dict):
    return lambda symbol, ts: prices.get(symbol)


def open_long(conn, spec, entry=60_000.0, qty=0.02, ts=T0):
    # 每一笔必须有止盈 + 止损。这些用例只关心止损那一侧，所以这里补一个
    # 离得很远的止盈，免得被"缺少止盈"挡在 validate 之外。
    spec = {"take_profit": {"type": "pct", "value": 0.20}, **spec}
    plan = exit_plan.resolve(spec, side=1, entry_price=entry, atr=400.0, bar_seconds=3600)
    ok, why = exit_plan.validate(plan, 1, entry)
    assert ok, why
    ledger.apply_order(conn, "a1", SYM, qty, entry, ts, exit_plan=plan)
    return plan


def test_stop_loss_fires_without_llm():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.02}})     # 58800

    prices = {SYM: 58_700.0}                                          # 穿越止损
    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn(prices))

    assert len(closed) == 1 and closed[0]["reason"] == "stop_loss", closed
    assert repo.get_position(conn, "a1", SYM) is None, "止损触发后仓位必须消失"

    fills = repo.get_fills(conn, "a1")
    assert len(fills) == 2, "应恰好一开一平"
    exit_fill = [f for f in fills if f["close_reason"] == "stop_loss"]
    assert len(exit_fill) == 1 and exit_fill[0]["side"] == "sell"
    assert abs(exit_fill[0]["qty"] - 0.02) < 1e-12

    # 防报复性交易：止损后有冷却期（§9.4 防作弊 2）
    assert repo.is_cooling_down(conn, "a1", clock.now()), "止损后必须进冷却期"

    # 平仓那一刻的权益要立刻落库，不能等到下个 tick；且与逐笔成交推算的现金一致
    last = repo.get_last_equity(conn, "a1")
    cash_expected = 1000.0
    for f in fills:
        sign = -1 if f["side"] == "buy" else 1
        cash_expected += sign * f["qty"] * f["price"] - f["fee"]
    assert abs(last["cash"] - cash_expected) < 1e-9, (last, cash_expected)
    assert abs(last["equity"] - last["cash"]) < 1e-9, "已无持仓，权益 = 现金"
    assert last["equity"] < 1000.0, "止损触发必然是亏的"
    conn.close()


def test_untouched_when_price_does_not_cross():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.05}})     # 57000

    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn({SYM: 59_500.0}))

    assert closed == []
    assert repo.get_position(conn, "a1", SYM) is not None
    assert not repo.is_cooling_down(conn, "a1", clock.now())
    conn.close()


def test_trailing_stop_tightens_over_time():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.05},
                     "trailing": {"mult": 1.5}})                       # 距离 600

    # 价格上冲、拉高 peak
    with conn:
        assert watchdog.run_once(conn, clock, make_price_fn({SYM: 62_000.0})) == []
    assert abs(repo.get_position(conn, "a1", SYM)["peak_price"] - 62_000.0) < 1e-9

    # 生效止损线被抬到 62000 - 600 = 61400
    eff = exit_plan.effective_stop(repo.get_position(conn, "a1", SYM))
    assert eff and abs(eff[0] - 61_400.0) < 1e-9 and eff[1] == "trailing_stop"

    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn({SYM: 61_300.0}))
    assert len(closed) == 1 and closed[0]["reason"] == "trailing_stop", closed
    conn.close()


def test_take_profit_does_not_set_cooldown():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.05},
                     "take_profit": {"type": "pct", "value": 0.04}})    # 62400

    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn({SYM: 62_500.0}))
    assert len(closed) == 1 and closed[0]["reason"] == "take_profit"
    assert not repo.is_cooling_down(conn, "a1", clock.now()), "止盈不该触发冷却"
    conn.close()


def test_missing_price_skips_instead_of_guessing():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.02}})

    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn({}))      # 拿不到价
    assert closed == [] and repo.get_position(conn, "a1", SYM) is not None
    conn.close()


def test_time_stop_closes_stale_position():
    conn, clock = setup()
    open_long(conn, {"stop_loss": {"type": "pct", "value": 0.20},
                     "time_stop": {"max_bars": 1}})                     # 1h
    with conn:
        assert watchdog.run_once(conn, clock, make_price_fn({SYM: 60_000.0})) == []
    clock.advance_by(3600)
    with conn:
        closed = watchdog.run_once(conn, clock, make_price_fn({SYM: 60_000.0}))
    assert len(closed) == 1 and closed[0]["reason"] == "time_stop", closed
    conn.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"S2 全通过（{len(tests)} 项）—— 全程未调用 LLM")
