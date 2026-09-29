"""子 Agent 的配置（ARCHITECTURE §5.3）。

**一个新策略 = 一份配置，不是一次写代码。**

    persona  ×  tools 子集  ×  params

配置在 `agents/*.json`；装载后同步进 DB（`agents` 表存人设与唤醒间隔，
`agent_budget` 表存预算）。DB 里的预算是**主 Agent 的领地**（§8.5），
所以这里只在"没有预算"时给一个初值，绝不覆盖运行中的值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from harness.config import DEFAULT, PROJECT_ROOT, Config
from harness.store import repo

AGENTS_DIR = PROJECT_ROOT / "agents"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    name: str
    persona: str
    universe: tuple[str, ...]
    tools: tuple[str, ...] = field(default_factory=tuple)
    tf: str = "1h"
    wake_interval: int = 3600
    w: float = 0.25                      # 预算权重
    gross_cap: float = 0.8               # Σ|名义| ÷ 权益 的上限
    leverage: float = 1.0                # 名义放大倍数；1.0 = 现货口径（不加杠杆）
    starting_equity: float = 1000.0

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSpec":
        missing = [k for k in ("agent_id", "name", "persona") if not d.get(k)]
        if missing:
            raise ValueError(f"agent 配置缺少必填项：{missing}")
        return cls(
            agent_id=d["agent_id"], name=d["name"], persona=d["persona"],
            universe=tuple(d.get("universe") or ()),
            tools=tuple(d.get("tools") or ()),
            tf=d.get("tf", "1h"),
            wake_interval=int(d.get("wake_interval", 3600)),
            w=float(d.get("w", 0.25)),
            gross_cap=float(d.get("gross_cap", 0.8)),
            leverage=float(d.get("leverage", 1.0)),
            starting_equity=float(d.get("starting_equity", 1000.0)),
        )

    @classmethod
    def load(cls, path: str | Path) -> "AgentSpec":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_all(directory: str | Path | None = None) -> list[AgentSpec]:
    d = Path(directory) if directory else AGENTS_DIR
    if not d.exists():
        return []
    return [AgentSpec.load(p) for p in sorted(d.glob("*.json"))]


def register(conn, spec: AgentSpec, ts: int, cfg: Config = DEFAULT) -> None:
    """把配置同步进 DB。**幂等**：重启多少次都只会有这些行。"""
    if repo.get_agent(conn, spec.agent_id) is None:
        repo.create_agent(conn, spec.agent_id, spec.name, spec.persona,
                          spec.wake_interval, spec.starting_equity, ts)
    # 预算只在缺失时给初值：它是主 Agent 的领地，不能被配置文件每轮覆盖（§8.5）
    if repo.get_budget(conn, spec.agent_id) is None:
        repo.set_budget(conn, spec.agent_id, "bootstrap", spec.w, spec.gross_cap, ts)


def spec_of(conn, agent_id: str, directory: str | Path | None = None) -> AgentSpec | None:
    for s in load_all(directory):
        if s.agent_id == agent_id:
            return s
    return None
