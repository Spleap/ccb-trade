"""Paper 撮合与账本（ARCHITECTURE §7）。

防呆设计（§7.2 前视陷阱）
------------------------
本模块**没有"用决策时刻的价格成交"这个选项**：调用方必须传入"决策之后"的
参考价（`mark_price`），成交价由 `fees` 在它基础上加减滑点。

这不是靠注释提醒，是靠 API 形状拦住 —— §7.2 说这是 paper 最容易出错、
且错了最不容易发现的地方，所以要从接口层面消掉这个可能性。

账本口径（§7.1）
----------------
    equity = cash + Σ(qty × mark_price)      # 带符号 qty，空头为负，一样成立

**这套口径天然是保证金口径，不需要为杠杆另开一个账户**：买入 5 倍名义的仓位时
现金会变成负数（＝借来的钱），而 `equity` 依旧等于"自有资金 + 浮动盈亏"，
所以价格反向走 1% 就亏掉 5% 的权益 —— 这正是 5 倍杠杆该有的样子。
"杠杆倍数"只影响 `decision.evaluate` 里**名义额**怎么算，不改变这里的记账；
强平判定在 `watchdog._liquidate_if_blown`。

各子策略独立记账、不做多空抵消。
"""
from __future__ import annotations

from harness.config import DEFAULT, Config
from harness.paper import fees
from harness.store import repo

_EPS = 1e-12


class OrderRejected(ValueError):
    """下单被拒。调用方要把它记成 result='rejected'，**不能吞掉**（§3.5）。"""


def apply_order(conn, agent_id: str, symbol: str, signed_qty: float, mark_price: float,
                ts: int, decision_id: str | None = None, exit_plan=None,
                close_reason: str | None = None, cfg: Config = DEFAULT) -> dict:
    """按 signed_qty 调整持仓：> 0 买入 / < 0 卖出。返回 fill 记录。

    一条重要例外：**减仓、平仓永远允许**，`min_notional` 只约束"敞口变大"的下单。
    否则一个跌到 5 U 的仓位会因为"不够最小下单额"而**永远平不掉** ——
    那正好把 §9.4 的保护性退出给堵死了。

    exit_plan：仅在开仓 / 反手 / 主动改计划时传入；减仓时不传则沿用旧计划。
    """
    if signed_qty == 0:
        raise OrderRejected("下单量为 0")

    agent = repo.get_agent(conn, agent_id)
    if agent is None:
        raise OrderRejected(f"不存在的 agent：{agent_id}")

    pos = repo.get_position(conn, agent_id, symbol)
    old_qty = float(pos["qty"]) if pos else 0.0
    new_qty = old_qty + signed_qty

    price = fees.exec_price(mark_price, signed_qty, cfg)      # 含滑点
    notional = abs(signed_qty) * price

    if abs(new_qty) > abs(old_qty):                           # 敞口变大才卡最小额
        ok, why = fees.check_min_notional(notional, cfg)
        if not ok:
            raise OrderRejected(why)

    fee = fees.taker_fee(notional, cfg)
    slippage = fees.slippage_cost(signed_qty, mark_price, price)

    # 现金流：买入付款、卖出入账；手续费永远支出。浮盈亏由 equity 体现。
    repo.set_cash(conn, agent_id, float(agent["cash"]) - signed_qty * price - fee)

    _update_position(conn, agent_id, symbol, pos, old_qty, new_qty, price, exit_plan, ts)

    fill = {
        # 由 (agent, 时刻, 序号) 派生：重跑两遍，成交表逐行一致。
        # 带序号是因为同一帧里止损平仓和 LLM 开仓可能同时发生。
        "fill_id": f"fill-{agent_id}-{ts}-{len(repo.get_fills(conn, agent_id, ts))}",
        "agent_id": agent_id,
        "decision_id": decision_id,
        "ts": ts,
        "symbol": symbol,
        "side": "buy" if signed_qty > 0 else "sell",
        "qty": abs(signed_qty),
        "price": price,
        "notional": notional,
        "fee": fee,
        "slippage": slippage,
        "close_reason": close_reason,
    }
    repo.insert_fill(conn, fill)
    return fill


def _update_position(conn, agent_id: str, symbol: str, pos: dict | None,
                     old_qty: float, new_qty: float, price: float,
                     exit_plan, ts: int) -> None:
    if abs(new_qty) < _EPS:                                   # 平净 -> 仓位消失
        repo.delete_position(conn, agent_id, symbol)
        return

    plan = exit_plan if exit_plan is not None else (pos["exit_plan"] if pos else None)

    if pos is None or (old_qty > 0) != (new_qty > 0):
        # 开仓，或反手。反手 = 平旧 + 开新，成本基准与 peak 全部重来。
        repo.upsert_position(conn, agent_id, symbol, new_qty, price, plan, price, ts, ts)
        return

    peak = pos["peak_price"] or price
    peak = max(peak, price) if new_qty > 0 else min(peak, price)

    if abs(new_qty) > abs(old_qty):                           # 同向加仓 -> 重算加权成本
        added = abs(new_qty - old_qty)
        avg = (abs(old_qty) * pos["avg_price"] + added * price) / abs(new_qty)
    else:                                                     # 同向减仓 -> 成本基准不变
        avg = pos["avg_price"]

    repo.upsert_position(conn, agent_id, symbol, new_qty, avg, plan, peak,
                         int(pos["opened_at"]), ts)


# ============================================================
# 权益
# ============================================================


def mark_to_market(conn, agent_id: str, prices: dict[str, float],
                   cfg: Config = DEFAULT) -> dict:
    """算当前权益。

    prices 缺某个 symbol 时 **不静默拿成本价替代** —— 记进 `missing` 交给调用方
    决定（§3.5：绝不静默失败）。成本价替代会造出一条漂亮的假权益曲线。
    """
    agent = repo.get_agent(conn, agent_id)
    if agent is None:
        raise OrderRejected(f"不存在的 agent：{agent_id}")

    cash = float(agent["cash"])
    value = 0.0
    missing: list[str] = []
    for p in repo.get_positions(conn, agent_id):
        mark = prices.get(p["symbol"])
        if mark is None:
            missing.append(p["symbol"])
            continue
        value += float(p["qty"]) * mark

    return {"cash": cash, "positions_value": value, "equity": cash + value, "missing": missing}


def write_equity(conn, agent_id: str, ts: int, prices: dict[str, float],
                 cfg: Config = DEFAULT) -> dict:
    """每个 tick 落一次 equity_curve（§7.4）。"""
    snap = mark_to_market(conn, agent_id, prices, cfg)
    repo.insert_equity(conn, agent_id, ts, snap["cash"], snap["positions_value"], snap["equity"])
    return snap
