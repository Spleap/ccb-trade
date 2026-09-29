"""工具层底座：统一返回形态、长度上限、失败哨兵、快照收集（ARCHITECTURE §4）。

三条**框架级**约定（不靠 LLM 自觉，靠代码强制）
--------------------------------------------
1. **返回必须有长度上限** —— 服务端强制截断。签名里的 `limit` 只是"上限的上限"，
   不能指望 LLM 每次都填一个合理值（§4.2 第 3 条）。
2. **失败返回哨兵，绝不返回空** —— 返回空字符串会让 LLM **编造数据**，
   这是最贵的一类 bug（§4.2 第 5 条）。沿用 `info-feeds` 的 `DATA_UNAVAILABLE:` 约定。
3. **行情数值自动落快照** —— 这是框架的责任，不能指望它记得（§4.2 第 4 条 / §2.4）。
   工具读到数字时顺手 `ctx.record(symbol, ...)`，⑦ Journal 一次性落库。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from harness.config import DEFAULT, Config
from harness.store import repo

# 与 info-feeds 保持同一套哨兵文案，避免出现两种"没数据"的表达
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


def unavailable(reason: str) -> str:
    return f"{DATA_UNAVAILABLE}: {reason}"


def is_unavailable(text: str) -> bool:
    return text.startswith(DATA_UNAVAILABLE)


def truncate(text: str, max_chars: int) -> str:
    """硬截断。宁可截断，也不允许一次工具调用把上下文挤满。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（已截断，原文 {len(text)} 字符）"


# ============================================================
# 上下文：一次唤醒内共享
# ============================================================


@dataclass
class ToolContext:
    """一次唤醒（一个 tick）内，某个 Agent 的工具调用上下文。

    `as_of` 是**决策时刻**，所有查询的时间上界（§2.6）——
    它不是"墙上现在几点"，测试里它就是注入时钟的当前值。
    """

    conn: Any
    agent_id: str
    as_of: int
    candles: Any = None                       # CandleSource，见 tools/candles.py
    cfg: Config = DEFAULT
    calls: list[dict] = field(default_factory=list)
    _snapshot: dict = field(default_factory=dict)

    # ── 决策所需的外部事实（由 loop 在 ① LoadContext 时注入）────
    tf: str = "1h"                            # 主看周期：time_stop 的 max_bars 靠它换算
    mark: Any = None                          # Callable[[str], float|None]，参考价
    budget: dict | None = None                # agent_budget 行：w / gross_cap
    equity: float = 0.0
    starting_equity: float = 0.0              # 该子账户的起始权益，累计回撤熔断用
    leverage: float = 1.0                     # 名义放大倍数（来自 AgentSpec）

    # ── 决策缓冲：D 类工具唯一被允许的"写"（§4.2 第 1 条）────────
    # LLM 不能写 DB。它只能往这三个槽里放"意图"，由 loop 在 ⑤⑥ 统一裁决与执行。
    proposal: dict | None = None              # propose_target 通过的提案
    last_rejection: str | None = None         # 最近一次被拒的原因（绝不静默失败）
    plan_amend: dict | None = None            # amend_exit_plan 的收紧意图

    # ── 快照 ────────────────────────────────────────
    def record(self, symbol: str, **values: Any) -> None:
        """把这次读到行情数值记进快照。后读到的覆盖先读到的。"""
        bucket = self._snapshot.setdefault(symbol, {})
        bucket.update({k: v for k, v in values.items() if v is not None})

    @property
    def snapshot(self) -> dict:
        return self._snapshot

    # ── 调用日志 ────────────────────────────────────
    def note(self, tool: str, kwargs: dict, ok: bool, note: str = "") -> None:
        """记录调用轨迹。这是"绝不静默失败"的落点（§3.5 第 2 条）。"""
        self.calls.append({"tool": tool, "args": kwargs, "ok": ok, "note": note})

    def inputs_summary(self) -> str:
        """给 `agent_decisions.inputs_summary`：本次到底读了什么。"""
        parts = []
        for c in self.calls:
            mark = "" if c["ok"] else "!"
            args = ",".join(f"{k}={v}" for k, v in c["args"].items())
            parts.append(f"{c['tool']}{mark}({args})")
        return "; ".join(parts)

    def failures(self) -> list[str]:
        return [f"{c['tool']}: {c['note']}" for c in self.calls if not c["ok"]]


# ============================================================
# 工具
# ============================================================

_JSON_TYPE = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def _coerce(raw: str, node: dict) -> Any:
    if node["type"] == "integer":
        try:
            return int(raw)
        except ValueError:
            return raw
    if node["type"] == "number":
        try:
            return float(raw)
        except ValueError:
            return raw
    if node["type"] == "boolean":
        return raw.lower() in ("1", "true", "yes")
    return raw


def to_json_schema(params: dict) -> dict:
    """把工具的简写签名翻成 JSON Schema，喂给 LLM 的函数调用协议。

    简写语法（`Tool.params` 里用的就是它）::

        "symbol":      "str"                → 必填 string
        "tf":          "str=1h"             → 选填 string，默认 1h
        "limit":       "int<=200"           → 选填 integer，上限 200（服务端还会再夹一次）
        "names":       "list[str] 如 rsi14"  → 选填 string 数组，空格后是说明

    判据：**一个裸标量（没默认值 `=`、没上限 `<=`、也不是数组）才是必填。**
    带标注的参数在服务端都有兜底默认（`data.py` 的 `_limit` / `_as_list`），
    所以不该进 `required` —— 逼 LLM 填它只是白占上下文。
    `<=` 必须在 `=` 之前切，否则 `"int<=200"` 会被误读成"默认值 200"。
    """
    props: dict[str, dict] = {}
    required: list[str] = []

    for name, spec in params.items():
        if isinstance(spec, dict):
            # 直接给 JSON Schema 片段（嵌套对象用）：原样透传
            props[name] = spec
            if "default" not in spec:
                required.append(name)
            continue

        text, _, desc = str(spec).strip().partition(" ")
        desc = desc.strip()

        maximum: int | None = None
        if "<=" in text:
            text, _, raw_max = text.partition("<=")
            try:
                maximum = int(raw_max)
            except ValueError:
                maximum = None

        default: str | None = None
        if "=" in text:
            text, _, default = text.partition("=")

        base = text.strip()
        is_list = base.startswith("list[")
        if is_list:
            inner = base[5:].rstrip("]").strip() or "str"
            node: dict = {"type": "array", "items": {"type": _JSON_TYPE.get(inner, "string")}}
        else:
            node = {"type": _JSON_TYPE.get(base, "string")}

        if desc:
            node["description"] = desc
        if maximum is not None:
            node["maximum"] = maximum
        if default is not None:
            node["default"] = _coerce(default, node)
        if default is None and maximum is None and not is_list:
            required.append(name)

        props[name] = node

    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


@dataclass(frozen=True)
class Tool:
    """一个原子能力 = 一只"手"。"""

    name: str
    description: str
    params: dict
    fn: Callable[..., str]

    def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        try:
            out = self.fn(ctx, **kwargs)
        except Exception as exc:  # 工具是系统边界：任何异常都转成哨兵，绝不让它打断决策
            out = unavailable(f"{self.name} 调用失败：{type(exc).__name__}: {exc}")
            ctx.note(self.name, kwargs, ok=False, note=str(exc))
            return truncate(out, ctx.cfg.tool_max_chars)

        if not isinstance(out, str):
            out = str(out)
        ok = not is_unavailable(out)
        ctx.note(self.name, kwargs, ok=ok, note="" if ok else out)
        return truncate(out, ctx.cfg.tool_max_chars)

    def spec_for_llm(self) -> dict:
        """给 LLM 的函数签名（OpenAI tools 的 `function` 段）。"""
        return {"name": self.name, "description": self.description,
                "parameters": to_json_schema(self.params)}


class Registry:
    """工具集。**按策略裁剪**：给日线趋势策略 `get_orderbook` 只会增加幻觉面（§4.2 第 6 条）。"""

    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: Iterable[str]) -> "Registry":
        return Registry(self._tools[n] for n in names if n in self._tools)

    def spec(self) -> list[dict]:
        """给 LLM 的工具清单（只有目录，不含实现）。"""
        return [t.spec_for_llm() for t in self._tools.values()]

    def run(self, tool_name: str, ctx: ToolContext, **kwargs: Any) -> str:
        """按名字跑一个工具。

        第一个参数**不叫 `name`**：工具自己的参数里就有叫 `name` 的
        （`get_sentiment_index(name=...)`），签名撞了会让这些调用直接 `TypeError`。
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return unavailable(f"未知工具 {tool_name}（本策略可用：{', '.join(self.names())}）")
        return tool.run(ctx, **kwargs)


# ============================================================
# ⑦ Journal：把工具读数落成一条快照
# ============================================================


def flush_snapshot(ctx: ToolContext, decision_id: str | None = None) -> str | None:
    """**可以不存 K 线，但必须存"你当时看到了什么"**（§2.4）。

    没有它，K 线不落库就无法复盘归因。
    """
    if not ctx.snapshot:
        return None
    # 快照归属于决策 —— 由 decision_id 派生，重跑时自动确定
    snapshot_id = f"snap-{decision_id}" if decision_id else uuid.uuid4().hex
    repo.insert_snapshot(ctx.conn, snapshot_id, ctx.agent_id, ctx.as_of,
                         ctx.snapshot, decision_id)
    return snapshot_id
