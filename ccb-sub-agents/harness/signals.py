"""交易指令出口（ARCHITECTURE §7.5）。

**本框架不向下游下单，只产出格式化的交易指令。** 这个模块是唯一出口：
把一次通过 ⑤ 复核的决策翻译成一行 `trade_signals`，并交给终端播出去。

指令与成交是两件事
------------------
* `trade_signals` = 给外部执行方的**契约**：标的、目标仓位、止损止盈、理由、依据。
* `agent_fills`   = 内部 paper 账本，用来算权益、做风控兜底。

刻意分开，是因为两者回答的问题不同：账本回答"如果真按它说的做，现在会怎样"，
指令回答"我想让下游做什么"。下游有自己的盘口、最小下单单位和资金规模，
所以**指令里的 `qty` 是目标持仓量（带符号），不是这次要买卖的量** ——
从"当前仓位"到"目标仓位"之间的路怎么走，由执行方决定。
"""
from __future__ import annotations

_EPS = 1e-9


def _action(cur_qty: float, target_qty: float) -> str:
    """给人看的动作标签。真正的语义在 `qty`（目标仓位）里。"""
    if abs(target_qty) < _EPS:
        return "close"
    if abs(cur_qty) < _EPS:
        return "open"
    if (cur_qty > 0) != (target_qty > 0):
        return "reverse"
    return "increase" if abs(target_qty) > abs(cur_qty) else "reduce"


def build(agent_id: str, decision_id: str, ts: int, prop: dict,
          reason: str | None, evidence: str | None) -> dict:
    """把一条通过复核的提案翻成指令行。纯函数，不碰 DB。

    `prop` 是 ⑤ 复核后的提案，含 symbol / side / mark / target_qty / delta_qty / plan。
    """
    symbol = prop["symbol"]
    target_qty = float(prop["target_qty"])
    cur_qty = target_qty - float(prop["delta_qty"])
    mark = float(prop["mark"])
    plan = prop.get("plan") or {}
    stop = plan.get("stop_loss")

    return {
        # 由 decision_id 派生：同一次决策在任何时候都产出同一个 signal_id
        "signal_id": f"sig-{decision_id}-{symbol}",
        "agent_id": agent_id,
        "decision_id": decision_id,
        "ts": ts,
        "action": _action(cur_qty, target_qty),
        "symbol": symbol,
        "side": "flat" if abs(target_qty) < _EPS else ("long" if target_qty > 0 else "short"),
        "qty": target_qty,
        "notional": target_qty * mark,
        "entry_price": mark,
        "stop_loss": float(stop) if stop is not None else None,
        "take_profit": plan.get("take_profit"),
        "trailing_dist": plan.get("trailing_dist"),
        "time_stop_sec": plan.get("time_stop_seconds"),
        "leverage": float(prop.get("leverage") or 1.0),
        # 触发止损时的亏损额（不含手续费与滑点）—— 下游可据此做二次风控
        "risk_amount": abs(target_qty) * abs(mark - float(stop)) if stop is not None else 0.0,
        "reason": reason,
        "evidence": evidence,
        "status": "emitted",
    }


def emit(conn, ctx, prop: dict, decision_id: str, ts: int, reason: str | None) -> dict:
    """落库并返回指令行。同时把该标的上一条未覆盖的指令标成 superseded。

    只增不改：新的来了，旧的只是"不再新鲜"，不删 —— 下游要靠这个判断哪条还有效，
    复盘时也要能看见"我们当时先说了什么、后来改成了什么"。
    """
    from harness.store import repo

    sig = build(ctx.agent_id, decision_id, ts, prop, reason, ctx.inputs_summary())
    repo.supersede_signals(conn, ctx.agent_id, sig["symbol"])
    repo.insert_signal(conn, sig)
    return sig


def format_line(sig: dict) -> str:
    """一行终端可读的指令。**给人和给机器的字段完全一致**，不做二次加工。"""
    qty = f"{sig['qty']:+.6g}"
    stop = "—" if sig["stop_loss"] is None else f"{sig['stop_loss']:.6g}"
    take = "—" if sig["take_profit"] is None else f"{sig['take_profit']:.6g}"
    lever = f" {sig['leverage']:g}x" if (sig["leverage"] or 1) > 1 else ""
    return (f"📤 指令 [{sig['action']:8s}] {sig['symbol']} {sig['side']:5s} "
            f"qty={qty}@{sig['entry_price']:.6g}{lever} "
            f"SL={stop} TP={take} risk={sig['risk_amount']:.2f}")
