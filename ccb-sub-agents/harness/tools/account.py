"""B 类工具：账户工具 —— 读"自己"（ARCHITECTURE §4.3）。

子 Agent 只能看见自己的仓位、权益、预算、历史决策。
**它看不见别的子策略** —— 这是 §7.4 那条"子 Agent 只能自检，不能自裁全局"的物理落点。

注意：这里的"仓位"是**已成交**的事实，不是意图。意图走 D 类的 `propose_target`。
"""
from __future__ import annotations

import datetime as dt
import json

from harness.store import repo
from harness.tools.base import Tool, ToolContext, unavailable

MAX_DECISIONS = 20


def _utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _limit(value, default: int, hard: int) -> int:
    n = default if value in (None, 0) else int(value)
    return max(1, min(n, hard))


def get_my_portfolio(ctx: ToolContext) -> str:
    """持仓 + 现金 + 权益 + 浮动盈亏。

    拿不到某个持仓的参考价时**不拿成本价替代**，直接标出来 ——
    成本价替代会造出一条漂亮的假权益（§3.5 绝不静默失败）。
    """
    agent = repo.get_agent(ctx.conn, ctx.agent_id)
    if agent is None:
        return unavailable(f"不存在的 agent：{ctx.agent_id}")

    cash = float(agent["cash"])
    positions = repo.get_positions(ctx.conn, ctx.agent_id)
    if not positions:
        return (f"当前空仓。现金 {cash:.2f}，权益 {cash:.2f}，起始 {ctx.cfg.starting_equity:.2f}，"
                f"累计收益 {cash - ctx.cfg.starting_equity:+.2f}")

    lines, value, gross, missing = [], 0.0, 0.0, []
    for p in positions:
        mark = ctx.mark(p["symbol"]) if ctx.mark else None
        qty, avg = float(p["qty"]), float(p["avg_price"])
        if mark is None:
            missing.append(p["symbol"])
            lines.append(f"- {p['symbol']}  {qty:+.6g} @ {avg:.6g}  （拿不到现价，浮盈未知）")
            continue
        pnl = qty * (mark - avg)
        value += qty * mark
        gross += abs(qty * mark)
        lines.append(f"- {p['symbol']}  {qty:+.6g} @ {avg:.6g}  现价 {mark:.6g}  "
                     f"浮盈 {pnl:+.2f}  ({(mark / avg - 1) * 100:+.2f}%)")

    head = (f"现金 {cash:.2f} ｜ 持仓市值 {value:.2f} ｜ 权益 {cash + value:.2f} ｜ "
            f"起始 {ctx.cfg.starting_equity:.2f}")
    if max(1.0, float(ctx.leverage or 1.0)) > 1 and gross > 0:
        # 杠杆下"持仓市值"和"权益"不再是一个量级，敞口倍数必须显式说出来
        equity = cash + value
        mult = f"{gross / equity:.2f} 倍" if equity > 0 else "已亏穿权益"
        head += f" ｜ 总敞口 {gross:.2f}（权益的 {mult}）"
    tail = f"\n（拿不到现价：{', '.join(missing)} —— 这几笔未计入持仓市值）" if missing else ""
    return f"{head}\n持仓 {len(positions)} 笔：\n" + "\n".join(lines) + tail


def get_my_budget(ctx: ToolContext) -> str:
    """本次唤醒能下多大。没有预算 = 本 tick 不许开仓（§8.5：参数只有上层能改）。"""
    b = ctx.budget
    if not b:
        return "本轮没有收到预算（主 Agent 未下发）→ 不得开仓，只能减仓或平仓"
    leverage = max(1.0, float(ctx.leverage or 1.0))
    max_notional = b["w"] * ctx.equity * leverage
    lever = f"，杠杆 = {leverage:g}x" if leverage > 1 else ""
    return (f"本轮预算：w = {b['w']:.4f}，gross_cap = {b['gross_cap']:.4f}（Σ|名义| ÷ 权益）"
            f"{lever}，当前权益 {ctx.equity:.2f}\n"
            f"⇒ 单笔最大名义 = w × 权益 × 杠杆 = {max_notional:.2f}"
            f"（ratio 的绝对值上限是 1.0，所以这是你能下的满仓）")


def get_my_recent_decisions(ctx: ToolContext, n: int | None = None) -> str:
    """最近几次决策。看的是"我当时怎么想的、结果如何"，用于避免反复犯同一个错。"""
    k = _limit(n, 5, MAX_DECISIONS)
    rows = repo.recent_decisions(ctx.conn, ctx.agent_id, k)
    if not rows:
        return "（还没有任何决策记录 —— 这是你的第一次唤醒）"

    lines = []
    for r in rows:
        ratio = _brief(r.get("target_ratio"))
        lines.append(f"- [{_utc(r['ts'])}] {r.get('result')}  target={ratio}  "
                     f"{(r.get('reasoning') or '')[:160]}")
        if r.get("degraded_reason"):
            lines.append(f"    ⚠ {r['degraded_reason']}")
    return f"最近 {len(rows)} 次决策（倒序）：\n" + "\n".join(lines)


def _brief(raw) -> str:
    if not raw:
        return "—"
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)[:120]


ACCOUNT_TOOLS: list[Tool] = [
    Tool("get_my_portfolio", "读自己的持仓 / 现金 / 权益 / 浮盈",
         {}, get_my_portfolio),
    Tool("get_my_budget", "读本轮预算：w / gross_cap / 单笔最大名义",
         {}, get_my_budget),
    Tool("get_my_recent_decisions", "读自己最近几次决策与结果",
         {"n": "int<=20"}, get_my_recent_decisions),
]
