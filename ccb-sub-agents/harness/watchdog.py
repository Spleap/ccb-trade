"""Loop 2：保护性退出（ARCHITECTURE §9.4 / §9.5）。

**这个循环不碰 LLM，也不碰指标。** exit_plan 在开仓那一刻就已经被解析成
绝对价（见 `exit_plan.resolve`），这里只做一件事：把现价和最紧的那条止损线比一下。

它存在的意义（§9.4 / §3.5）
---------------------------
止损不依赖"LLM 恰好醒着"。正因如此，§3.5 才敢写：
**LLM 挂掉 = 什么都不做，而这是安全的。**
安全靠止损线，不靠醒得勤 —— 这也是"中低频唤醒"能够成立的物理基础。

测试里同样要跑它：如果只让策略跑而不跑 watchdog，等于假设"止损永远不触发"，
那测出来的收益全是假的。
"""
from __future__ import annotations

from typing import Callable

from harness import exit_plan
from harness.config import DEFAULT, Config
from harness.paper import ledger
from harness.store import repo

# price_fn(symbol, ts) -> 参考价；拿不到返回 None。由调用方注入（实盘给实时价，测试给固定价）。
PriceFn = Callable[[str, int], "float | None"]


def run_once(conn, clock, price_fn: PriceFn, cfg: Config = DEFAULT,
             leverage_of: Callable[[str], float] | None = None,
             cfg_of: Callable[[str], Config] | None = None) -> list[dict]:
    """扫一遍所有 active agent 的持仓，命中退路就平掉。

    返回本次触发的平仓记录，交给上层写日志/面板。**幂等**：同一 tick 重复调用
    不会重复平仓，因为仓位已经没了。

    `leverage_of` 给定且某 agent 的杠杆 > 1 时，额外做一次**强平判定** ——
    这是杠杆唯一的"额外风控"，也是它和现货最本质的差别：
    现货最多亏到零，杠杆会**亏穿**，必须在权益见底之前先动手。

    `cfg_of` 给每个 agent 取它自己那份配置（风险偏好是按策略写的）。
    止损后的冷却时长就来自这里 —— 用一个全局冷却期套所有策略，
    对 15m 高频是过紧、对 4h 波段是等于没有。
    """
    now = clock.now()
    closed: list[dict] = []

    for agent in repo.list_agents(conn, "active"):
        agent_id = agent["agent_id"]
        agent_cfg = (cfg_of(agent_id) if cfg_of else None) or cfg
        prices: dict[str, float] = {}
        dirty = False

        for pos in repo.get_positions(conn, agent_id):
            symbol = pos["symbol"]
            mark = price_fn(symbol, now)
            # 拿不到价 -> 本轮跳过。宁可漏平一次，也不能用错误的价格误平。
            if not mark or mark <= 0:
                continue
            prices[symbol] = mark

            # ① 先推进 peak，再判定 —— 否则移动止损永远慢一拍
            peak = _advance_peak(pos, mark)
            if peak != pos["peak_price"]:
                repo.set_peak_price(conn, agent_id, symbol, peak, now)
                pos = {**pos, "peak_price": peak}

            reason = exit_plan.triggers_exit(pos, mark, now)
            if reason is None:
                continue

            fill = ledger.apply_order(
                conn, agent_id, symbol, -float(pos["qty"]), mark, now,
                close_reason=reason, cfg=agent_cfg,
            )
            if reason in exit_plan.STOP_REASONS:
                # 防报复性交易：刚被打掉不许立刻打回去（§9.4 防作弊 2）
                repo.set_cooldown(conn, agent_id, now + agent_cfg.cooldown_after_stop)

            closed.append({
                "agent_id": agent_id,
                "symbol": symbol,
                "reason": reason,
                "price": fill["price"],
                "fee": fill["fee"],
                "ts": now,
            })
            dirty = True

        # ② 止损扫完之后再看强平：强平优先于把它当成一次普通止损
        leverage = float(leverage_of(agent_id) or 1.0) if leverage_of else 1.0
        if leverage > 1.0:
            blown = _liquidate_if_blown(conn, agent_id, prices, now, agent_cfg)
            if blown:
                closed.extend(blown)
                dirty = True

        if dirty:
            # 平仓改了现金和持仓，权益曲线要立刻反映，不能等到下个 tick
            ledger.write_equity(conn, agent_id, now, prices, agent_cfg)

    return closed


def _liquidate_if_blown(conn, agent_id: str, prices: dict[str, float], now: int,
                        cfg: Config) -> list[dict]:
    """权益已被浮亏吃穿到维持保证金以下 -> 全部强平。

    两条保守约定：

    * **任何一笔仓位拿不到现价就不强平** —— 宁可晚一帧，也不能用错价强平。
    * 阈值按**名义**算（`Σ|名义| × maintenance_margin_rate`），与真实交易所一致；
      10x 下约等于 9.5% 的不利波动，20x 下约 4.5%。
    """
    positions = repo.get_positions(conn, agent_id)
    if not positions:
        return []

    notional = 0.0
    for p in positions:
        mark = prices.get(p["symbol"])
        if not mark or mark <= 0:
            return []
        notional += abs(float(p["qty"]) * mark)

    agent = repo.get_agent(conn, agent_id)
    equity = float(agent["cash"]) + sum(
        float(p["qty"]) * prices[p["symbol"]] for p in positions)

    if equity > notional * cfg.maintenance_margin_rate:
        return []

    out: list[dict] = []
    for p in positions:
        symbol, mark = p["symbol"], prices[p["symbol"]]
        fill = ledger.apply_order(conn, agent_id, symbol, -float(p["qty"]), mark, now,
                                  close_reason="liquidated", cfg=cfg)
        out.append({
            "agent_id": agent_id,
            "symbol": symbol,
            "reason": "liquidated",
            "price": fill["price"],
            "fee": fill["fee"],
            "ts": now,
        })
    # 亏穿之后立刻打回去是最典型的报复性交易，冷却期在这里尤其必要（§9.4 防作弊 2）
    repo.set_cooldown(conn, agent_id, now + cfg.cooldown_after_stop)
    return out


def _advance_peak(pos: dict, mark: float) -> float:
    """peak_price = 开仓以来的最有利价（多头取最高、空头取最低）。"""
    peak = pos["peak_price"]
    if peak is None:
        return mark
    return max(peak, mark) if float(pos["qty"]) > 0 else min(peak, mark)


def run_forever(conn, clock, price_fn: PriceFn, cfg: Config = DEFAULT,
                leverage_of: Callable[[str], float] | None = None,
                cfg_of: Callable[[str], Config] | None = None,
                should_stop: Callable[[], bool] | None = None) -> None:
    """Loop 2 常驻。测试里不要用它 —— 逐次调 `run_once`。"""
    while not (should_stop and should_stop()):
        with conn:
            run_once(conn, clock, price_fn, cfg, leverage_of, cfg_of)
        clock.sleep(cfg.watchdog_interval)
