"""撮合的费用与滑点模型（ARCHITECTURE §7.3）。

**这个项目的收入来自返佣，所以手续费不是"小项"，是核心变量。**
每一笔 fill 都必须把 fee 与 slippage **分开记** —— 否则后面算不清
"策略赚的是价差，还是被手续费吃掉了"。

本模块只做纯算术，不碰 DB、不碰时钟。
"""
from __future__ import annotations

from harness.config import DEFAULT, Config


def exec_price(mark_price: float, signed_qty: float, cfg: Config = DEFAULT) -> float:
    """按参考价推导成交价。

    **滑点永远对交易者不利**：买入吃高价、卖出吃低价。
    这条写死，是为了让 paper 结果偏保守 —— 宁可低估，不可高估。

    signed_qty: > 0 买入 / < 0 卖出
    """
    if mark_price <= 0:
        raise ValueError(f"无效参考价：{mark_price}")
    slip = mark_price * cfg.slippage_bps / 10_000.0
    return mark_price + slip if signed_qty > 0 else mark_price - slip


def taker_fee(notional: float, cfg: Config = DEFAULT) -> float:
    """第一版全部按 taker 计费（主动吃单）。"""
    return abs(notional) * cfg.taker_fee_rate


def slippage_cost(signed_qty: float, mark_price: float, fill_price: float) -> float:
    """滑点造成的实际损失（正数）。"""
    return abs(signed_qty) * abs(fill_price - mark_price)


def check_min_notional(notional: float, cfg: Config = DEFAULT) -> tuple[bool, str | None]:
    return (
        (True, None)
        if notional >= cfg.min_notional
        else (False, f"名义额 {notional:.2f} < 最小下单额 {cfg.min_notional:.2f}")
    )
