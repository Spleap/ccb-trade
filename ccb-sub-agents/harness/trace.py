"""轨迹（观测层）—— 把 Agent 的思考与工具调用**落库**，让人事后看得见。

为什么需要它
------------
终端上的实时输出是**一次性**的：屏幕滚过去就没了。而 `agent_decisions` 只留一个摘要
（读过哪些工具 + 最后那句判断）—— 工具**返回了什么内容**、它**中间推理了几轮**、
**为什么最后决定不动**，这些都不在任何表里。于是"它当时到底看到了什么"这个问题，
事后没有任何办法回答。

这个模块就是那个出口：`Recorder` 负责写，`render` 负责读成人话。

它是**旁路产物，不是决策依据**（`agent_decisions` 才是）。三条纪律：
1. 写失败一律吞掉 —— 观察者不该有能力搞砸被观察的东西（沿用 `loop._emit` 的约定）；
2. 只原样记录 + 按单字段长度截断，不做任何解释、不改写任何内容；
3. 删掉整张 `agent_trace` 也不影响任何一次判断，所以它随时可重建。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from harness import signals
from harness.config import DEFAULT, Config
from harness.store import repo

# 哪些事件值得在轨迹里单独占一行。
# `round_end` 不在其中：它只是"这段正文说完了"的**流式边界**，作用是触发碎片合并，
# 本身没有信息量（轮次边界已经由 thinking 表达）。
_KINDS = ("thinking", "tool_call", "tool_result", "degraded", "signal")


class Recorder:
    """`on_event` 的第二个消费者：把事件流 append 进 `agent_trace`。

    一次进程只需要一个实例 —— 它在收到 `wake_start` 时自动重新绑定到
    新的 decision_id，所以能连着跑很多次唤醒。
    """

    def __init__(self, conn, *, cfg: Config = DEFAULT, enabled: bool = True):
        self.conn = conn
        self.cfg = cfg
        self.enabled = enabled
        self.decision_id: str | None = None
        self.agent_id: str | None = None
        self.ts = 0
        self._seq = 0
        # LLM 的流式正文是**按碎片**回调的（一次几字符），一条一行的落库会碎成几百行。
        # 所以攒着，等下一个非 text 事件（或下一次唤醒）来了再合并成一段。
        self._pending: list[str] = []

    # ── 观察者接口 ───────────────────────────────────────────
    def __call__(self, ev: dict) -> None:
        if not self.enabled:
            return
        try:
            self._handle(ev)
        except Exception:
            pass                      # 记不下来不是事故，只是少了一段可读性

    def _handle(self, ev: dict) -> None:
        kind = ev.get("kind")

        if kind == "wake_start":
            self._flush()             # 上一次唤醒可能还有一段正文没落地，先归到**旧**的 id 上
            self.decision_id = ev.get("decision_id")
            self.agent_id = ev.get("agent")
            self.ts = int(ev.get("ts") or 0)
            self._seq = 0
            self._write("wake_start", {"agent": self.agent_id})
            return

        # 没绑上唤醒的事件（例如"找不到 agent 配置"那种直接降级）没有 decision_id 可挂，
        # 它们本来就已经在 agent_decisions 里留痕了。
        if self.decision_id is None:
            return

        if kind == "text":
            self._pending.append(str(ev.get("text") or ""))
            return

        self._flush()                 # 任何非 text 事件都是"这段正文说完了"的边界
        if kind in _KINDS:
            payload = {k: v for k, v in ev.items() if k != "kind"}
            self._write(kind, payload)

    # ── 落库 ────────────────────────────────────────────────
    def _flush(self) -> None:
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        if text:
            self._write("text", {"text": text})

    def _write(self, kind: str, payload: dict) -> None:
        payload, truncated = _clip_fields(payload, self.cfg.trace_max_chars)
        self._seq += 1
        repo.insert_trace(self.conn, f"{self.decision_id}:{self._seq:04d}",
                          self.decision_id, self.agent_id or "", self.ts, self._seq,
                          kind, payload, truncated)


def _clip_fields(payload: dict, limit: int) -> tuple[dict, int]:
    """按**单字段**截断（不是整条 payload 一起截）。

    这么切是因为每个字段的用途不同：`args` 通常很短、`result` 可能很长 ——
    一起截的话一条长 JSON 会把同一行里真正重要的短字段挤掉。
    """
    if limit <= 0:
        return payload, 0
    out: dict = {}
    cut = 0
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + f"\n…（轨迹里已截断，原文 {len(value)} 字符）"
            cut = 1
        else:
            out[key] = value
    return out, cut


# ============================================================
# 读：人可读时间轴
# ============================================================


def render(conn, decision_id: str, *, result_chars: int = 1500) -> str:
    """把一次唤醒的轨迹渲染成时间轴。

    `result_chars` 只影响**打印出来**的部分（工具返回太长会把屏幕刷没），
    库里始终是完整的 —— 截断时会明确写出还剩多少字符。
    """
    decision = repo.get_decision(conn, decision_id)
    if decision is None:
        return f"（库里没有这条决策：{decision_id}）"

    rows = repo.traces(conn, decision_id)
    out: list[str] = [_head(decision, len(rows))]
    if not rows:
        out.append("  （没有轨迹：这一轮跑在本功能上线前，或当时没开记录）")
    for row in rows:
        out.extend(_line(row, result_chars))
    out.append("")
    out.extend(_foot(decision, rows))
    return "\n".join(out)


def _head(decision: dict, n: int) -> str:
    return (f"──── 唤醒轨迹 · {decision.get('agent_id')} · {_when(decision.get('ts'))} ────\n"
            f"  decision_id={decision.get('decision_id')}   共 {n} 条事件")


def _line(row: dict, result_chars: int) -> list[str]:
    kind = row.get("kind")
    data = _loads(row.get("payload"))
    trunc = "（已截断）" if row.get("truncated") else ""

    if kind == "wake_start":
        return [f"  ▷ 唤醒开始 {trunc}"]
    if kind == "thinking":
        return ["", f"  ── 第 {data.get('round')} 轮 ──"]
    if kind == "text":
        return _block(data.get("text"), "     ")
    if kind == "tool_call":
        return [f"   ▸ 调用 {data.get('name')}({_args(data.get('args'))}) {trunc}".rstrip()]
    if kind == "tool_result":
        body = str(data.get("result") or "")
        head = f"     ← {data.get('name')} 返回"
        if len(body) > result_chars:
            rest = len(body) - result_chars
            head += f"（篇幅 {len(body)} 字符，下面只印前 {result_chars}，其余已落库）"
            body = body[:result_chars] + f"\n…（还有 {rest} 字符）"
        return [head, *_block(body, "       ")]
    if kind == "degraded":
        return [f"   ! 降级：{data.get('reason')}"]
    if kind == "signal":
        # `format_line` 自带 "📤 指令" 前缀，别再加一层
        return ["   " + signals.format_line(data.get("signal") or {})]
    return [f"   ? {kind} {json.dumps(data, ensure_ascii=False)}"]


def _foot(decision: dict, rows: list[dict]) -> list[str]:
    lines = ["──── 结局 ────",
             f"  result = {decision.get('result')}"
             + (f"   ｜ {decision['degraded_reason']}" if decision.get("degraded_reason") else "")]
    if decision.get("inputs_summary"):
        lines.append(f"  读了   {decision['inputs_summary']}")
    if decision.get("target_ratio"):
        lines.append(f"  目标   {decision['target_ratio']}")
    if decision.get("reasoning"):
        lines.append("  判断   " + " ".join(str(decision["reasoning"]).split()))
    kinds: dict[str, int] = {}
    for row in rows:
        kinds[row.get("kind")] = kinds.get(row.get("kind"), 0) + 1
    if kinds:
        lines.append("  轨迹   " + ", ".join(f"{k}={v}" for k, v in kinds.items()))
    return lines


# ── 渲染小工具 ──────────────────────────────────────────────

def _args(args: Any) -> str:
    """把工具入参压成一行 `k=v`，长值走一行 JSON。"""
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            parts.append(f"{key}={value}")
        else:
            parts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return ", ".join(parts)


def _block(text: Any, prefix: str) -> list[str]:
    body = str(text if text is not None else "")
    return [f"{prefix}{line}" for line in (body.splitlines() or [""])]


def _when(ts: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    return data if isinstance(data, dict) else {"value": data}
