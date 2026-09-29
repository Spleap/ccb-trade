"""D 类工具：决策工具 —— 唯一的出口（ARCHITECTURE §4.3）。

**整个系统里 LLM 能触发的"写"只有一处：`propose_target`。**
但它也不是直接写 DB —— 它把意图放进 `ctx.proposal`，由 loop 在 ⑤⑥ 统一裁决与执行。
这样"绕过校验直接下单"在结构上就不存在。

刻意缺一个工具
--------------
**没有 `cancel_exit_plan`。** 止损线不能删，只能收紧（§9.4 防作弊 1）。
不是"我们没实现"，是**这个能力就不该存在** —— 少一个工具，就少一条绕过风控的路径。

一条纪律：**"拒绝"必须说清为什么**，并且原因会回灌给 LLM（§3.5 允许它改正一次）。
"""
from __future__ import annotations

from typing import Any

from harness import exit_plan as plans
from harness.paper import fees, ledger
from harness.store import repo
from harness.tools.base import Tool, ToolContext
from harness.tools.candles import TF_SECONDS

_EPS = 1e-9


# ============================================================
# ⑤ 的核心：把提案算成"能不能做、做多少"
# ============================================================


def evaluate(ctx: ToolContext, symbol: str, ratio: Any, exit_plan_spec: dict | None) -> dict:
    """`propose_target` / `precheck` / loop 的 ⑤ 共用这一份逻辑。

    共用不是图省事：**如果 LLM 自我试算的口径和 harness 的裁决口径不一致，
    提示词里写再好的"请自行检查"都是假的。**
    """
    out: dict = {
        "ok": False, "reason": None, "symbol": symbol, "ratio": None, "mark": None,
        "side": 0, "target_qty": 0.0, "delta_qty": 0.0, "notional": 0.0,
        "leverage": 1.0, "plan": None, "exit_plan": exit_plan_spec, "atr": None,
        "risk_amount": 0.0,
    }

    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return {**out, "reason": f"ratio 必须是数字，收到 {ratio!r}"}
    out["ratio"] = ratio

    if not (-1.0 <= ratio <= 1.0):
        return {**out, "reason": f"ratio 必须在 [-1.0, 1.0]，收到 {ratio}"}

    mark = ctx.mark(symbol) if ctx.mark else None
    if not mark or mark <= 0:
        # 拿不到价 -> 不下单。绝不"猜一个"（§3.5 第 1 条）
        return {**out, "reason": f"拿不到 {symbol} 的参考价，本轮不下单（宁可不做，不可做错）"}
    out["mark"] = float(mark)

    pos = repo.get_position(ctx.conn, ctx.agent_id, symbol)
    cur_qty = float(pos["qty"]) if pos else 0.0

    if ratio == 0.0:                                   # 清仓：不需要 exit_plan
        out.update(ok=True, side=0, target_qty=0.0, delta_qty=-cur_qty)
        return out

    side = 1 if ratio > 0 else -1
    out["side"] = side

    if not ctx.budget:
        return {**out, "reason": "本轮没有预算（主 Agent 未下发）→ 不得开仓"}

    w = float(ctx.budget["w"])
    gross_cap = float(ctx.budget["gross_cap"])
    equity = float(ctx.equity)
    # 杠杆只放大**名义**：ratio/w 仍然是在描述"用掉多少风险预算"，
    # 杠杆决定这一份预算能撬动多大的仓位。（简化口径，不建模维持保证金与资金费率）
    leverage = max(1.0, float(ctx.leverage or 1.0))
    out["leverage"] = leverage
    target_notional = abs(ratio) * w * equity * leverage

    gross_other = _gross_other(ctx, symbol)
    if gross_other + target_notional > gross_cap * equity + _EPS:
        return {**out, "reason": (f"总敞口超限：其他持仓 {gross_other:.2f} + 本笔 "
                                 f"{target_notional:.2f} > gross_cap × 权益 "
                                 f"({gross_cap:.4f} × {equity:.2f} = {gross_cap * equity:.2f})")}

    target_qty = side * target_notional / float(mark)
    delta_qty = target_qty - cur_qty
    out.update(target_qty=target_qty, delta_qty=delta_qty)
    out["notional"] = abs(delta_qty) * float(mark)

    if out["notional"] < _EPS:                          # 已在目标位，无需成交
        return {**out, "ok": True}

    # —— 只要在"加大敞口"，就先过两道账户级闸门 ——
    if abs(target_qty) > abs(cur_qty) + _EPS:
        # 闸门 1：累计回撤熔断。权益跌破起始的 (1 - max_drawdown_halt) 就只许减仓。
        # 这是"不能让他亏太多"的最后一层 —— 单笔风控挡不住连续做错。
        floor = ctx.starting_equity * (1.0 - ctx.cfg.max_drawdown_halt)
        if ctx.starting_equity > 0 and equity < floor:
            return {**out, "reason": (
                f"累计回撤熔断：权益 {equity:.2f} 已低于起始 {ctx.starting_equity:.2f} 的 "
                f"{1.0 - ctx.cfg.max_drawdown_halt:.0%} 线（{floor:.2f}）→ 只许减仓或平仓")}

        # 闸门 2：止损后的冷却期，刚被打掉不许立刻加大敞口（§9.4 防作弊 2）。
        # LLM 会把"刚亏"当成"需要立刻挽回"的信号，这个本能必须在协议层挡住。
        if repo.is_cooling_down(ctx.conn, ctx.agent_id, ctx.as_of):
            return {**out, "reason": "止损冷却期内禁止加大敞口（防报复性交易，§9.4）"}

    # —— 到这里意味着"要产生成交"，那么 exit_plan 必填（§9.4）——
    if not exit_plan_spec:
        return {**out, "reason": "缺少 exit_plan：做交易就必须在开仓那一刻写好退路（§9.4）"}

    # ATR 取"它这次真正看到的值"（快照里的），这样计划与它对行情的认知一致
    atr = (ctx.snapshot.get(symbol) or {}).get("atr14")
    out["atr"] = atr

    try:
        plan = plans.resolve(exit_plan_spec, side, float(mark),
                             atr=atr, bar_seconds=TF_SECONDS.get(ctx.tf))
    except plans.PlanError as exc:
        return {**out, "reason": f"exit_plan 不合规：{exc}"}

    ok, why = plans.validate(plan, side, float(mark), ctx.cfg, leverage=leverage)
    if not ok:
        return {**out, "reason": why}

    ok, why = fees.check_min_notional(out["notional"], ctx.cfg)
    if not ok:
        return {**out, "reason": why}

    # ★ 单笔最大亏损：本笔止损触发时到底亏多少 = 目标仓位 × 止损距离。
    # 名义由 ratio 决定、止损距离由 LLM 决定，两者相乘才是真实风险 ——
    # 不校验它，"不能亏太多"就只是一句口号。
    stop = plan.get("stop_loss")
    if stop is not None:
        dist = abs(float(mark) - float(stop))
        risk = abs(target_qty) * dist
        cap = ctx.starting_equity * ctx.cfg.max_loss_per_trade_pct
        out["risk_amount"] = risk
        if cap > 0 and risk > cap + _EPS:
            # 明确告诉它"按这个止损距离，ratio 最多能开到多少"，否则它只会反复撞墙。
            #   risk = |ratio| × per_unit × dist ÷ mark ≤ cap
            #   ⇒ |ratio| ≤ cap × mark ÷ (per_unit × dist)
            per_unit = w * equity * leverage               # ratio=1 时的名义
            max_ratio = (cap * float(mark)) / (per_unit * dist) if per_unit > 0 and dist > 0 else 0.0
            return {**out, "reason": (
                f"单笔风险超限：本笔止损触发将亏 {risk:.2f}，超过上限 {cap:.2f}"
                f"（起始权益 {ctx.starting_equity:.2f} 的 {ctx.cfg.max_loss_per_trade_pct:.1%}）。"
                f"按当前止损距离 {dist:.6g}，ratio 的绝对值最多 {min(max_ratio, 1.0):.4f}")}

    out["plan"] = plan
    return {**out, "ok": True}


def _gross_other(ctx: ToolContext, symbol: str) -> float:
    """除本标的外，当前 Σ|持仓名义|。用现价，拿不到才退回成本价 ——
    这里退回成本价是**保守**的（敞口只会被高估，不会被低估）。"""
    total = 0.0
    for p in repo.get_positions(ctx.conn, ctx.agent_id):
        if p["symbol"] == symbol:
            continue
        mark = ctx.mark(p["symbol"]) if ctx.mark else None
        total += abs(float(p["qty"]) * (float(mark) if mark else float(p["avg_price"])))
    return total


# ============================================================
# ⑥ 执行（只由 loop 调用，LLM 碰不到）
# ============================================================


def execute(conn, ctx: ToolContext, prop: dict, ts: int, decision_id: str) -> dict | None:
    """把提案落成成交。返回 fill；无成交时返回 None。"""
    if abs(prop["delta_qty"]) < _EPS:
        return None
    return ledger.apply_order(
        conn, ctx.agent_id, prop["symbol"], prop["delta_qty"], prop["mark"], ts,
        decision_id=decision_id, exit_plan=prop["plan"], cfg=ctx.cfg,
    )


def apply_amend(conn, ctx: ToolContext, ts: int) -> dict | None:
    """落一次"收紧止损"的意图。返回新的 plan；没有意图返回 None。

    "只收紧"由 `plans.amend` 保证 —— 放宽在工具层就被拦下了，
    所以走到这里的一定是合法收紧。
    """
    amend = ctx.plan_amend
    if not amend:
        return None
    position = repo.get_position(conn, ctx.agent_id, amend["symbol"])
    if position is None:
        return None
    repo.upsert_position(
        conn, ctx.agent_id, amend["symbol"], float(position["qty"]),
        float(position["avg_price"]), amend["plan"], position["peak_price"],
        int(position["opened_at"]), ts,
    )
    ctx.record(amend["symbol"], amended_stop_loss=amend["plan"].get("stop_loss"))
    return amend["plan"]


# ============================================================
# 工具
# ============================================================


def propose_target(ctx: ToolContext, symbol: str, ratio, reason: str,
                   exit_plan: dict | None = None) -> str:
    """提交目标占比。这是唯一的写操作（§3.4）。"""
    if not reason or not str(reason).strip():
        # reason 强制：面板要展示、归因要用、返佣合规要留痕（§3.4）
        ctx.proposal = None
        ctx.last_rejection = "缺少 reason"
        return "rejected: 缺少 reason —— 说明你判断的依据，面板与合规都要看"

    v = evaluate(ctx, symbol, ratio, exit_plan)
    if not v["ok"]:
        # 被拒即作废先前的提案：不能在它要了 B 之后悄悄替它执行 A（绝不静默失败）
        ctx.proposal = None
        ctx.last_rejection = v["reason"]
        return f"rejected: {v['reason']}"

    ctx.last_rejection = None
    ctx.proposal = {**v, "reason": str(reason)}

    if abs(v["delta_qty"]) < _EPS:
        return (f"accepted: 目标 {symbol} ratio={v['ratio']:+.4f}，与当前持仓一致，"
                f"不会产生成交")
    verb = "买入" if v["delta_qty"] > 0 else "卖出"
    lever = f"，{v['leverage']:g}x 杠杆" if v["leverage"] > 1 else ""
    return (f"accepted: 目标 {symbol} ratio={v['ratio']:+.4f}，本轮将{verb} "
            f"{abs(v['delta_qty']):.6g} @ ≈{v['mark']:.6g}（名义 {v['notional']:.2f}{lever}），"
            f"止损 {v['plan']['stop_loss']:.6g}（触发时亏 {v['risk_amount']:.2f}）。"
            f"本轮结束时执行，中途可再提交覆盖。")


def get_exit_plan(ctx: ToolContext, symbol: str) -> str:
    """读自己当前的保护线。**只读** —— 想删掉它是没有工具的。"""
    pos = repo.get_position(ctx.conn, ctx.agent_id, symbol)
    if pos is None:
        return f"（没有 {symbol} 的持仓，也就没有 exit_plan）"
    plan = plans.load(pos["exit_plan"])
    if not plan:
        return f"⚠ {symbol} 持仓存在但**没有 exit_plan** —— 这是异常状态，请立刻减仓"

    side = 1 if float(pos["qty"]) > 0 else -1
    stop = plans.effective_stop(pos)
    lines = [f"{symbol} 持仓 {float(pos['qty']):+.6g} @ {float(pos['avg_price']):.6g}，"
             f"开于 {pos['opened_at']}"]
    if plan.get("stop_loss") is not None:
        lines.append(f"- 止损 {plan['stop_loss']:.6g}")
    if plan.get("take_profit") is not None:
        lines.append(f"- 止盈 {plan['take_profit']:.6g}")
    if plan.get("trailing_dist") is not None:
        lines.append(f"- 移动止损 距离 {plan['trailing_dist']:.6g}（peak {pos['peak_price']}）")
    if plan.get("time_stop_seconds") is not None:
        lines.append(f"- 超时退出 {plan['time_stop_seconds']}s")
    if stop is not None:
        lines.append(f"⇒ 当前真正生效的止损线：{stop[0]:.6g}（{stop[1]}），"
                     f"方向 {'多头' if side > 0 else '空头'}")
    lines.append("（止损只能收紧、不能放宽，也没有撤销它的工具）")
    return "\n".join(lines)


def amend_exit_plan(ctx: ToolContext, symbol: str, exit_plan: dict) -> str:
    """**只能朝有利方向移动**。放宽一律拒绝（§9.4 防作弊 1）。

    防的是 LLM 浮亏时那个本能："再等等"。这必须在协议层禁掉，而不是靠提示词劝。
    """
    pos = repo.get_position(ctx.conn, ctx.agent_id, symbol)
    if pos is None:
        return f"rejected: 没有 {symbol} 的持仓，无从修改止盈止损"
    old = plans.load(pos["exit_plan"])
    if not old:
        return f"rejected: {symbol} 当前没有 exit_plan，无法修改（请先减仓再重新提案）"

    side = 1 if float(pos["qty"]) > 0 else -1
    atr = (ctx.snapshot.get(symbol) or {}).get("atr14")
    try:
        new = plans.amend(old, exit_plan, side, float(pos["avg_price"]),
                          atr=atr, bar_seconds=TF_SECONDS.get(ctx.tf))
    except plans.PlanError as exc:
        ctx.last_rejection = str(exc)
        return f"rejected: {exc}"

    ctx.plan_amend = {"symbol": symbol, "plan": new, "spec": exit_plan}
    return (f"accepted: {symbol} 计划已收紧 —— 止损 "
            f"{old.get('stop_loss')} → {new.get('stop_loss')}，本轮结束时生效")


def precheck(ctx: ToolContext, symbol: str, ratio, exit_plan: dict | None = None) -> str:
    """试算，**不产生任何后果**。让 LLM 先自己撞一次墙，比事后被拒好。"""
    v = evaluate(ctx, symbol, ratio, exit_plan)
    if not v["ok"]:
        return f"✗ 会被拒绝：{v['reason']}"
    if abs(v["delta_qty"]) < _EPS:
        return f"✓ 通过：目标 {symbol} ratio={v['ratio']:+.4f} 与当前持仓一致，无成交"
    return (f"✓ 通过：将{'买入' if v['delta_qty'] > 0 else '卖出'} "
            f"{abs(v['delta_qty']):.6g} @ ≈{v['mark']:.6g}，名义 {v['notional']:.2f}，"
            f"止损 {v['plan']['stop_loss']:.6g}（触发时亏 {v['risk_amount']:.2f}）")


_EXIT_PLAN_SCHEMA: dict = {
    "type": "object",
    "description": "退路。做交易必须在开仓那一刻写好止盈与止损（缺任何一个都会被直接拒绝）。"
                   '例：{"stop_loss":{"type":"atr","value":2.0},'
                   '"take_profit":{"type":"atr","value":3.5}}',
    "properties": {
        "stop_loss": {"type": "object",
                      "description": '止损。type ∈ atr/pct/price，如 {"type":"atr","value":2.0}'},
        "take_profit": {"type": "object",
                        "description": '止盈。type ∈ atr/pct/price，如 {"type":"atr","value":3.5}'},
        "trailing": {"type": "object",
                     "description": '移动止损，如 {"enabled":true,"mult":1.5}（ATR 口径）'},
        "time_stop": {"type": "object",
                      "description": '超时退出，如 {"max_bars":48} 或 {"seconds":3600}'},
    },
    "required": ["stop_loss", "take_profit"],
}

DECISION_TOOLS: list[Tool] = [
    Tool("propose_target", "提交目标占比（唯一写操作）。ratio ∈ [-1,1]，+多 / -空 / 0 清仓",
         {"symbol": "str", "ratio": "float", "reason": "str", "exit_plan": _EXIT_PLAN_SCHEMA},
         propose_target),
    Tool("get_exit_plan", "读自己某持仓当前的止盈止损",
         {"symbol": "str"}, get_exit_plan),
    Tool("amend_exit_plan", "收紧某持仓的止盈止损。**只能收紧，不能放宽**",
         {"symbol": "str", "exit_plan": _EXIT_PLAN_SCHEMA}, amend_exit_plan),
    Tool("precheck", "试算一次提案会不会被拒，不产生任何后果",
         {"symbol": "str", "ratio": "float", "exit_plan": _EXIT_PLAN_SCHEMA}, precheck),
]
