"""Loop 1 · scheduler（ARCHITECTURE §9.5）—— 唤醒决策。

    due = [a for a in agents if now >= a.next_wake]
    for a in due[:MAX_CONCURRENCY]:      # 并发上限，保护 LLM 限速
        run_agent_loop(a)                # §3.1 七步
        a.next_wake = now + a.wake_interval

**一个刻意的偏离**：`shuffle(due)` 那种写法这里不用。
改成**按"等得最久的先跑"确定性排序 + 并发上限截断**。

理由：`random.shuffle` 会让同一份输入的两次运行结果对不上，
而"同样的起点跑两遍结果一致"是可测试性的前提。
确定性排序同样能达到错峰的效果（各 Agent 的 `wake_interval` 本就把它们错开了），
而且**谁被截断是可以解释的**（它等得最短），比"随机截断"更适合复盘。
"""
from __future__ import annotations

from typing import Callable, Iterable

from harness import agents, ids, loop, tools
from harness.config import DEFAULT, Config
from harness.llm import LLMClient
from harness.store import repo


def due_agents(conn, now: int) -> list[dict]:
    """到期且已排好序的 agent（等得最久的排最前，同刻按 agent_id 稳定排序）。"""
    rows = []
    for a in repo.list_agents(conn, "active"):
        rt = repo.get_runtime(conn, a["agent_id"])
        if rt is None:
            continue
        next_wake = int(rt["next_wake_at"] or 0)
        if now >= next_wake:
            rows.append({**a, "_next_wake": next_wake})
    rows.sort(key=lambda r: (r["_next_wake"], r["agent_id"]))
    return rows


def run_once(conn, clock, llm: LLMClient, *, specs: Iterable[agents.AgentSpec] | None = None,
             candles=None, mark: Callable[[str, int], "float | None"] | None = None,
             cfg: Config = DEFAULT,
             on_event: Callable[[dict], None] | None = None) -> list[loop.WakeResult]:
    """扫一遍到期的 agent 并唤醒它们。测试里逐次调它。

    `on_event` 原样传给七步 loop，用来把"正在想什么、调了什么工具"实时播出去。
    """
    now = clock.now()
    by_id = {s.agent_id: s for s in (specs if specs is not None else agents.load_all())}

    results: list[loop.WakeResult] = []
    for agent in due_agents(conn, now)[: cfg.max_concurrency]:
        agent_id = agent["agent_id"]
        spec = by_id.get(agent_id)
        interval = spec.wake_interval if spec else int(agent["wake_interval"])

        if spec is None:
            results.append(loop.degrade(conn, agent_id, now,
                                        ids.new_id(clock, "dec", agent_id, now,
                                                   repo.count_decisions_at(conn, agent_id, now)),
                                        "找不到 agent 配置（agents/*.json）"))
        else:
            results.append(loop.run_once(
                conn, agent_id, clock, llm=llm,
                registry=tools.registry_for(spec.tools), spec=spec, candles=candles,
                mark=(lambda sym, _m=mark, _as=now: _m(sym, _as)) if mark else None,
                cfg=cfg, on_event=on_event,
            ))

        # 无论这一轮成败，下一次唤醒都要重新排 —— 否则失败一次它就永远卡在 due 里
        with conn:
            repo.set_next_wake(conn, agent_id, now + interval, last_wake_ts=now)

    return results


def run_forever(conn, clock, llm: LLMClient, *, specs=None, candles=None, mark=None,
                cfg: Config = DEFAULT, should_stop: Callable[[], bool] | None = None) -> None:
    """Loop 1 常驻。测试里不要用它 —— 由测试逐次调 `run_once`。"""
    while not (should_stop and should_stop()):
        with conn:
            run_once(conn, clock, llm, specs=specs, candles=candles, mark=mark, cfg=cfg)
        clock.sleep(cfg.scheduler_interval)
