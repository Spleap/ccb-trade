"""工具层：A/B/C/D 四类（ARCHITECTURE §4）。

| 类 | 模块 | 干什么 |
|---|---|---|
| A 数据 | `data.py` | 看市场（K 线、指标、新闻、情绪、宏观、预测市场） |
| B 账户 | `account.py` | 看自己（持仓 / 预算 / 历史决策） |
| C 记忆 | `memory.py`（tools 下） | 读自己的记忆 |
| D 决策 | `decision.py` | 唯一出口：提案 / 收紧止损 |

**工具集必须按策略裁剪**（§4.2 第 6 条）：一个只做新闻的策略不需要 `get_candles`，
给它只会增加幻觉面。`full_registry()` 只用于测试，实盘一律走 `registry_for(spec)`。
"""
from typing import Iterable

from harness.tools.account import ACCOUNT_TOOLS
from harness.tools.base import (DATA_UNAVAILABLE, Registry, Tool, ToolContext,  # noqa: F401
                                flush_snapshot, is_unavailable, to_json_schema,
                                truncate, unavailable)
from harness.tools.candles import (CandleSource, ListCandleSource,  # noqa: F401
                                   TF_SECONDS, last_close)
from harness.tools.data import DATA_TOOLS
from harness.tools.decision import DECISION_TOOLS
from harness.tools.memory import MEMORY_TOOLS


def full_registry() -> Registry:
    """全部工具（含 D 类出口）。测试用。"""
    return Registry([*DATA_TOOLS, *ACCOUNT_TOOLS, *MEMORY_TOOLS, *DECISION_TOOLS])


def registry_for(tool_names: Iterable[str]) -> Registry:
    """按策略裁剪**数据工具**。

    被裁的只有 A 类。B 类（看自己）、C 类（记忆）、D 类（出口）**永远在**：
    "先看自己"是框架强制的第 ① 步（§3.1），记忆是与 harness 的固定接口（§8.4），
    出口更不可能靠配置裁掉。配置能决定的只是"它能看到世界的哪几个面"。
    """
    wanted = set(tool_names)
    data = [t for t in DATA_TOOLS if t.name in wanted]
    return Registry([*data, *ACCOUNT_TOOLS, *MEMORY_TOOLS, *DECISION_TOOLS])


__all__ = [
    "DATA_UNAVAILABLE", "Registry", "Tool", "ToolContext",
    "flush_snapshot", "is_unavailable", "to_json_schema", "truncate", "unavailable",
    "CandleSource", "ListCandleSource", "TF_SECONDS", "last_close",
    "DATA_TOOLS", "ACCOUNT_TOOLS", "MEMORY_TOOLS", "DECISION_TOOLS",
    "full_registry", "registry_for",
]
