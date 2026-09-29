"""七步 loop（ARCHITECTURE §3.1）—— 一次唤醒的完整骨架。

    ① LoadContext   读自己：仓位 / 权益 / 本次预算 / **记忆**               [代码]
    ② Observe       调工具取数                                       [LLM·ReAct]
    ③ Deliberate    形成判断                                         [LLM·ReAct]
    ④ Propose       输出 target_ratio + exit_plan                     [LLM·结构化]
    ⑤ Validate      预算 / 最小下单额 / 止损合规 / **单笔亏损上限**       [代码]
    ⑥ Execute       记 paper 账本 + **产出交易指令**（不下单）           [代码]
    ⑦ Journal       写决策 + 落快照 + **派生记忆**（§8 三条路径）        [代码]

**只有 ②③ 放开 LLM，其余五步全由代码控制。** 这不是保守，是 §3.2 的结论：
纯 ReAct 会漏步骤（忘了先看自己的仓位）、会忘记输出、产物不统一；
固定管线又退化成代码策略。骨架 + 自由段两头都要。

关于"降级"（§3.5）
------------------
**LLM 挂掉 = 什么都不做，而这是安全的。** 因为止损由 watchdog（Loop 2）独立执行，
LLM 的可用性不影响仓位安全。所以本模块遇到 LLM 失败时**直接放弃本 tick** ——
即使它在失败前已经提交过提案，也不执行。**"宁可不做，不可做错。"**
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable

from harness import agents, ids, memory, signals, tools
from harness.config import DEFAULT, Config
from harness.llm import LLMClient, LLMError, tool_message
from harness.paper import ledger
from harness.prompts import build_system_prompt, context_message
from harness.store import repo
from harness.tools import account as account_tools
from harness.tools import decision as decision_tools
from harness.tools.base import ToolContext, flush_snapshot
from harness.tools.candles import last_close

# WakeResult.status 的取值：
#   traded   提案通过并产生了成交
#   amended  只收紧了某个持仓的止盈止损
#   no_action LLM 主动选择不动（或目标与现状一致）
#   rejected 提案被 ④ 或 ⑤ 拒绝（原因在 reason）
#   degraded 本 tick 放弃（LLM 不可用 / 未收敛 / 配置缺失），原因在 reason
STATUSES = ("traded", "amended", "no_action", "rejected", "degraded")


@dataclass
class WakeResult:
    """一次唤醒的产物。**每种结局都要有名字**，绝不静默（§3.5）。"""

    agent_id: str
    decision_id: str
    ts: int
    status: str
    reason: str | None = None
    fills: list[dict] = field(default_factory=list)
    target_ratio: dict | None = None
    snapshot_id: str | None = None
    reflection: dict | None = None            # 本轮是否写了反思（§8.3），没写会带原因
    signal: dict | None = None                # 向下游交付的交易指令（§7.5）


def degrade(conn, agent_id: str, ts: int, decision_id: str, why: str) -> WakeResult:
    """降级也要留痕 —— 面板必须看得到"它为什么这一轮没动"。"""
    repo.insert_decision(conn, decision_id, agent_id, ts, result="degraded",
                         degraded_reason=why)
    return WakeResult(agent_id, decision_id, ts, "degraded", reason=why)


def _emit(on_event: Callable[[dict], None] | None, kind: str, **payload) -> None:
    """把"正在发生什么"播出去，给终端看。

    **只影响看得见，不影响结果** —— 和 `printer` 一样，刻意不进 summary、
    不进决策记录，所以开着它也不会改变任何落库内容。
    回调自己抛异常也当没发生：观察者不该有能力搞砸被观察的东西。
    """
    if on_event is None:
        return
    try:
        on_event({"kind": kind, **payload})
    except Exception:
        pass


# ============================================================
# 主入口
# ============================================================


def run_once(conn, agent_id: str, clock, *, llm: LLMClient,
             registry: tools.Registry | None = None, spec: agents.AgentSpec | None = None,
             candles=None, mark: Callable[[str], "float | None"] | None = None,
             cfg: Config = DEFAULT, memory_block: str | None = None,
             on_event: Callable[[dict], None] | None = None) -> WakeResult:
    """跑一次七步。实盘由 scheduler 驱动，测试里直接调它。

    `on_event` 是**旁路观察者**：调它的时候它按顺序收到 wake_start / thinking /
    text / tool_call / tool_result / degraded，供终端实时渲染。
    不传它就完全静默，且结果一模一样。
    """
    now = clock.now()
    # 这个 id 由 (agent, 时刻, 这一秒的第几条) 决定 —— 见 harness/ids.py（重跑可复现）。
    # 带序号是因为同一秒里可能连写两条（一次唤醒里既平仓又开仓），只靠时刻会撞主键。
    decision_id = ids.new_id(clock, "dec", agent_id, now,
                            repo.count_decisions_at(conn, agent_id, now))

    agent = repo.get_agent(conn, agent_id)
    if agent is None:
        raise ValueError(f"不存在的 agent：{agent_id}")

    _emit(on_event, "wake_start", agent=agent_id, ts=now)

    # ═══ ① LoadContext ═══════════════════════════════════════
    spec = spec or agents.spec_of(conn, agent_id)
    if spec is None:
        why = f"找不到 agent 配置（agents/*.json 里没有 {agent_id}）"
        _emit(on_event, "degraded", reason=why)
        return degrade(conn, agent_id, now, decision_id, why)

    if mark is None:
        if candles is None:
            raise ValueError("必须提供 mark 或 candles 之一：没有价格来源就只能瞎猜，"
                             "而瞎猜是不允许的（§3.5）")
        mark = lambda sym, _c=candles, _tf=spec.tf, _as=now: last_close(_c, sym, _tf, _as)  # noqa: E731

    if registry is None:
        registry = tools.registry_for(spec.tools)

    positions = repo.get_positions(conn, agent_id)
    prices = _marks(conn, agent_id, mark)
    mtm = ledger.mark_to_market(conn, agent_id, prices, cfg)

    ctx = ToolContext(conn=conn, agent_id=agent_id, as_of=now, candles=candles, cfg=cfg,
                      tf=spec.tf, mark=mark, budget=repo.get_budget(conn, agent_id),
                      equity=mtm["equity"], starting_equity=spec.starting_equity,
                      leverage=spec.leverage)

    # 记忆注入（§8.4 push 路径）。指纹只用于"这条记忆是不是来自另一个市场状态"，
    # 所以它算出来的 ATR **不进决策快照** —— 快照的语义是"LLM 当时真的看到了什么"。
    if memory_block is None:
        memory_block = memory.inject_block(
            conn, agent_id, now,
            cur_fingerprint=memory.current_fingerprint(spec.universe, candles, spec.tf, now),
            symbols=spec.universe, cfg=cfg)

    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(spec, spec.starting_equity, cfg)},
        {"role": "user", "content": context_message(
            as_of=now, now_str=_utc(now),
            portfolio=account_tools.get_my_portfolio(ctx),
            budget=account_tools.get_my_budget(ctx),
            positions_without_plan=[p["symbol"] for p in positions if not p["exit_plan"]],
            memory_block=memory_block,
        )},
    ]

    # ═══ ② Observe ⇄ ③ Deliberate（自由段）═══════════════════
    # ④ Propose 不是单独一步：它就是这段里的一个工具调用。
    texts, degraded = _converse(llm, registry, ctx, messages, cfg, on_event)

    if degraded:
        _emit(on_event, "degraded", reason=degraded)
        with conn:
            res = degrade(conn, agent_id, now, decision_id, degraded)
            # LLM 已经确认不可用，不再回头去叫它做反思（llm=None）
            _derive_memory(conn, agent_id, decision_id, now, cfg, None)
            return res

    # ⑦ 的 `reasoning` 存的是**这一轮的判断依据**，不是整段对话记录：
    # 有提案时就是 propose_target 的 `reason`（§3.4：面板展示、归因、合规留痕都靠它）；
    # 只在它压根没提案时，才退回用它说过的原话 —— 而且取**最后说的那一段**：
    # 中间的"我先看看…"是过程独白，能用于归因的只有它收尾给出的判断。
    transcript = next((t.strip() for t in reversed(texts) if t and t.strip()), None)

    # ═══ ⑤ Validate → ⑥ Execute → ⑦ Journal ═════════════════
    with conn:
        prop = ctx.proposal
        if prop is None:
            # LLM 选择不动。注意：**收紧止损仍然要落地** —— 它可能这轮只想做这件事。
            amended = decision_tools.apply_amend(conn, ctx, now)
            status = "amended" if amended else ("rejected" if ctx.last_rejection else "no_action")
            _journal(conn, ctx, decision_id, now, reasoning=transcript, result=status,
                     degraded_reason=ctx.last_rejection)
            return WakeResult(agent_id, decision_id, now, status, reason=ctx.last_rejection,
                              snapshot_id=flush_snapshot(ctx, decision_id),
                              reflection=_derive_memory(conn, agent_id, decision_id, now, cfg, llm))

        # 用**执行时刻**的价再裁决一次：一个 tick 内的行情漂移也算漂移（§3.1 ⑤）
        v = decision_tools.evaluate(ctx, prop["symbol"], prop["ratio"], prop["exit_plan"])
        decision_tools.apply_amend(conn, ctx, now)

        if not v["ok"]:
            why = f"⑤ 复核未通过：{v['reason']}"
            _journal(conn, ctx, decision_id, now, reasoning=prop["reason"] or transcript,
                     target_ratio={prop["symbol"]: prop["ratio"]},
                     result="rejected", degraded_reason=why)
            return WakeResult(agent_id, decision_id, now, "rejected", reason=why,
                              snapshot_id=flush_snapshot(ctx, decision_id),
                              reflection=_derive_memory(conn, agent_id, decision_id, now, cfg, llm))

        fresh = {**prop, **{k: v[k] for k in
                            ("mark", "side", "target_qty", "delta_qty", "notional", "plan")}}
        fill = decision_tools.execute(conn, ctx, fresh, now, decision_id)
        # ⑥ 之后再取一次价：本轮的成交已经改变了持仓，①时刻的 prices 已经不适用
        ledger.write_equity(conn, agent_id, now, _marks(conn, agent_id, mark), cfg)

        # 📤 指令出口（§7.5）：只有真产生了动作才发指令 ——
        # 目标与现状一致时不该惊动下游，那是噪音，不是信号。
        signal = None
        if fill:
            signal = signals.emit(conn, ctx, {**fresh, "leverage": v["leverage"]},
                                  decision_id, now, prop["reason"])

        _journal(conn, ctx, decision_id, now, reasoning=prop["reason"] or transcript,
                 target_ratio={prop["symbol"]: prop["ratio"]}, exit_plan=fresh["plan"],
                 orders=[fill] if fill else [], result="accepted",
                 degraded_reason=None if fill else "（未产生成交：目标与当前一致）")
        snapshot_id = flush_snapshot(ctx, decision_id)
        reflection = _derive_memory(conn, agent_id, decision_id, now, cfg, llm)

    if signal is not None:
        _emit(on_event, "signal", signal=signal)

    return WakeResult(agent_id, decision_id, now, "traded" if fill else "no_action",
                      reason=prop["reason"], fills=[fill] if fill else [],
                      target_ratio={prop["symbol"]: prop["ratio"]}, snapshot_id=snapshot_id,
                      reflection=reflection, signal=signal)


# ============================================================
# ② ③ 自由段
# ============================================================


def _converse(llm: LLMClient, registry: tools.Registry, ctx: ToolContext,
              messages: list[dict], cfg: Config,
              on_event: Callable[[dict], None] | None = None,
              ) -> tuple[list[str], str | None]:
    """把 LLM 与工具之间来回跑完。返回 (它说过的自然语言, 降级原因)。"""
    texts: list[str] = []

    for round_no in range(cfg.max_tool_rounds):
        _emit(on_event, "thinking", round=round_no + 1)
        # 有观察者就开流式：模型边吐字边播出去（Claude Code 那种观感）。
        # 没有观察者就走一次性调用 —— 结果完全一样，只是不吭声。
        on_text = (lambda piece: _emit(on_event, "text", text=piece)) if on_event else None
        try:
            reply = llm.chat(messages, tools=registry.spec(),
                             temperature=cfg.llm_temperature, on_text=on_text)
        except LLMError as exc:
            # §3.5：LLM 挂掉 -> 维持现有仓位，本 tick 放弃。安全，因为止损不归它管。
            return texts, f"LLM 不可用：{exc}"

        messages.append(reply.as_message())
        if reply.content:
            texts.append(reply.content)

        if not reply.tool_calls:
            return texts, None

        for call in reply.tool_calls:
            _emit(on_event, "tool_call", name=call.name, args=call.arguments)
            out = registry.run(call.name, ctx, **call.arguments)
            _emit(on_event, "tool_result", name=call.name, result=out)
            messages.append(tool_message(call.id, call.name, out))
    else:
        # 轮数用尽 = 没收敛。有提案就带着警告继续，没有就放弃（§3.5 宁可不做）
        if ctx.proposal is None:
            return texts, f"工具调用轮数超过 {cfg.max_tool_rounds} 仍未收敛，本轮放弃"
        texts.append(f"（注意：本轮工具调用达到上限 {cfg.max_tool_rounds}）")
        return texts, None


# ============================================================
# ⑦ Journal
# ============================================================


def _journal(conn, ctx: ToolContext, decision_id: str, ts: int, *, reasoning,
             target_ratio=None, exit_plan=None, orders=None, result: str,
             degraded_reason: str | None) -> None:
    """⑦ Journal：写决策记录。"""
    repo.insert_decision(
        conn, decision_id, ctx.agent_id, ts,
        inputs_summary=ctx.inputs_summary(), reasoning=reasoning,
        target_ratio=target_ratio, exit_plan=exit_plan, orders=orders,
        result=result, degraded_reason=degraded_reason,
    )


def _derive_memory(conn, agent_id: str, decision_id: str, ts: int, cfg: Config,
                   llm: LLMClient | None) -> dict | None:
    """⑦ 之后的三条记忆路径（§8.2）。

    A 情节 + C 统计是纯代码，**每 tick 必做**；B 反思只有命中触发点才会真的叫 LLM，
    而且它返回的是"写没写 + 为什么"，不是异常 —— 没写是正常结局。
    """
    memory.record_episode(conn, agent_id, decision_id, ts, cfg)
    memory.update_stats(conn, agent_id, ts, cfg)
    memory.decay_and_prune(conn, agent_id, ts, cfg)
    if llm is None:
        return None
    return memory.maybe_reflect(conn, agent_id, ts, llm=llm, cfg=cfg)


def _marks(conn, agent_id: str, mark) -> dict[str, float]:
    """当前持仓的现价表。取不到价的标的**不塞进表里** ——
    交给 mark_to_market 记进 missing，而不是拿成本价顶替（§3.5）。"""
    prices: dict[str, float] = {}
    for p in repo.get_positions(conn, agent_id):
        m = mark(p["symbol"])
        if m:
            prices[p["symbol"]] = float(m)
    return prices


def _utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
