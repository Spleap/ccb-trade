"""C 类工具：记忆工具（ARCHITECTURE §4.3 / §8.4）。

**只有 `recall` 一个。** 记忆的写入权不归 LLM（§8.2）——
不是"我们没做 `remember`"，是**这个能力就不该存在**：
LLM 写自己的记忆 = 给自己下达未来的行为指令，那是一条能绕过所有风控的路径。

检索走关键词，不引 embedding：记忆量是千级，关键词够用、可解释、零依赖（§10.1）。
匹配不到就**明说匹配不到** —— 返回一堆不相关的记忆比返回空更糟。
"""
from __future__ import annotations

from harness import memory
from harness.tools.base import Tool, ToolContext

MAX_K = 20


def recall(ctx: ToolContext, query: str, k: int | None = None) -> str:
    """按关键词检索自己的记忆（情节 / 反思）。"""
    n = max(1, min(int(k or 5), MAX_K))
    q = (query or "").strip()
    rows = memory.search(ctx.conn, ctx.agent_id, q, n, ctx.as_of)
    if not rows:
        return f"（无匹配记忆：{q or '（查询为空）'}）"

    lines = [f"匹配到 {len(rows)} 条（关键词：{memory.keyword(q) or '（无）'}）："]
    for r in rows:
        when = memory.utc_str(r["ts"])
        body = memory.format_episode(r["content"]) if r["kind"] == memory.EPISODIC \
            else " ".join(str(r["content"] or "").split())
        lines.append(f"- [{when}] ({r['kind']}) {body}")
    return "\n".join(lines)


MEMORY_TOOLS: list[Tool] = [
    Tool("recall", "按关键词检索你自己的记忆（过去的决策与反思）",
         {"query": "str 如 BTC/USDT 或 止损", "k": "int<=20"}, recall),
]
