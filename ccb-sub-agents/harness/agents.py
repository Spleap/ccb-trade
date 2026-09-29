"""子 Agent 的配置（ARCHITECTURE §5.3）。

**一个新策略 = 一份配置，不是一次写代码。**

    persona  ×  universe  ×  tools 子集  ×  params

配置在 `agents/*.json`；装载后同步进 DB（`agents` 表存人设与唤醒间隔，
`agent_budget` 表存预算）。DB 里的预算是**主 Agent 的领地**（§8.5），
所以这里只在"没有预算"时给一个初值，绝不覆盖运行中的值。

风险偏好是一等公民（§9.4）
--------------------------
**同一个单笔亏损上限套在所有策略上，等于没有策略画像。** 一个 15m 日内和一个
日线趋势，风险胃口、止损宽度、冷却时长都不该一样。所以这些参数都放在这里、
**创建时写定**，而不是留在全局 `Config` 里：

| 字段 | 管什么 |
|---|---|
| `max_loss_per_trade_pct` | 单笔最大亏损（占起始权益），"不能亏太多"的那层兜底 |
| `max_drawdown_halt` | 账户累计回撤熔断线，破了只许减仓 |
| `stop_distance_min_pct` / `max_pct` | 止损距离的允许区间：太近是噪声，太远形同虚设 |
| `cooldown_after_stop` | 止损后同方向的冷却时长，防报复性交易 |
| `default_exit_plan` | 默认退路：LLM 省略 `exit_plan` 时框架替它套上的止盈止损 |

**没写的字段回落全局默认值**（`Config`），所以老配置不用改；
`risk_cfg()` 把本策略的画像盖到全局配置上，下游只认一份 cfg。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from harness.config import DEFAULT, PROJECT_ROOT, Config
from harness.store import repo

AGENTS_DIR = PROJECT_ROOT / "agents"


def _opt_float(d: dict, key: str) -> float | None:
    """没写 = None（回落全局默认），写了才生效 —— 0 与"没写"必须区分开。"""
    v = d.get(key)
    return None if v is None else float(v)


def _opt_int(d: dict, key: str) -> int | None:
    v = d.get(key)
    return None if v is None else int(v)


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

    # ── 风险偏好：创建时写定，None = 回落全局 Config ──────────────
    max_loss_per_trade_pct: float | None = None
    max_drawdown_halt: float | None = None
    stop_distance_min_pct: float | None = None
    stop_distance_max_pct: float | None = None
    cooldown_after_stop: int | None = None
    default_exit_plan: dict | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSpec":
        missing = [k for k in ("agent_id", "name", "persona") if not d.get(k)]
        if missing:
            raise ValueError(f"agent 配置缺少必填项：{missing}")
        plan = d.get("default_exit_plan")
        if plan is not None and not isinstance(plan, dict):
            raise ValueError("default_exit_plan 必须是对象（同 propose_target 的 exit_plan）")
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
            max_loss_per_trade_pct=_opt_float(d, "max_loss_per_trade_pct"),
            max_drawdown_halt=_opt_float(d, "max_drawdown_halt"),
            stop_distance_min_pct=_opt_float(d, "stop_distance_min_pct"),
            stop_distance_max_pct=_opt_float(d, "stop_distance_max_pct"),
            cooldown_after_stop=_opt_int(d, "cooldown_after_stop"),
            default_exit_plan=plan,
        )

    def risk_cfg(self, cfg: Config = DEFAULT) -> Config:
        """把本策略的风险偏好盖到全局配置上 = 这个 Agent **实际生效**的 cfg。

        下游（决策 ⑤、止损校验、watchdog、提示词渲染）只认这一份 cfg，
        不再需要知道"哪些参数是每个策略各写一份的"。
        """
        overrides = {
            k: v for k, v in (
                ("max_loss_per_trade_pct", self.max_loss_per_trade_pct),
                ("max_drawdown_halt", self.max_drawdown_halt),
                ("stop_distance_min_pct", self.stop_distance_min_pct),
                ("stop_distance_max_pct", self.stop_distance_max_pct),
                ("cooldown_after_stop", self.cooldown_after_stop),
            ) if v is not None
        }
        return replace(cfg, **overrides) if overrides else cfg

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
