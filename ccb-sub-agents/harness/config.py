"""harness 全局配置。

不引入配置框架：一个 frozen dataclass 足够（ARCHITECTURE §10.1）。
所有魔术数字集中在这里，代码里不许再出现裸常量。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"

# 信息层与账本层共用一个库。采集进程（info-feeds）读的是同一个 CCB_DB_PATH——
# 两个进程必须指向同一个文件，否则 harness 永远读不到新采集到的数据。
DB_PATH = os.getenv("CCB_DB_PATH") or str(PROJECT_ROOT / "ccb_subagents.db")


@dataclass(frozen=True)
class Config:
    # ── 存储 ────────────────────────────────────────────
    db_path: str = DB_PATH

    # ── 账本（§7.1 / §7.3）──────────────────────────────
    starting_equity: float = 1000.0
    taker_fee_rate: float = 0.0006      # 6 bps
    maker_fee_rate: float = 0.0002      # 2 bps
    slippage_bps: float = 2.0
    min_notional: float = 10.0          # 最小下单名义额

    # ── 风控（§9.4）─────────────────────────────────────
    # 三档兜底，从紧到松：单笔亏损 → 止损与强平的距离 → 账户累计回撤。
    cooldown_after_stop: int = 1800     # 止损后同方向冷却 30min
    stop_distance_max_pct: float = 0.5  # 止损距离上限，防"名义上设了但形同虚设"
    max_loss_per_trade_pct: float = 0.02   # ★ 单笔最多亏权益的 2%
    max_drawdown_halt: float = 0.30        # ★ 权益累计回撤 30% -> 只许减仓，不许加风险

    # ── 杠杆（简化口径，见 harness/paper/ledger.py 的账本说明）──────
    # 只做"名义放大 + 强平线"，**不建模逐仓维持保证金与资金费率** ——
    # 账本本身就是保证金口径（现金可为负），所以不需要额外开户。
    # 强平阈值按**名义**算（与真实交易所一致）：权益 ≤ Σ|名义| × 该比例 即强平。
    # 0.005 对应 10x 约 9.5% 的不利波动、20x 约 4.5%。
    maintenance_margin_rate: float = 0.005

    # ── 唤醒 ────────────────────────────────────────────
    default_wake_interval: int = 3600
    max_concurrency: int = 8

    # ── 循环节奏 ────────────────────────────────────────
    scheduler_interval: int = 10        # Loop 1：定时唤醒
    watchdog_interval: int = 60         # Loop 2：止损 / 强平扫描

    # ── 记忆（§8.4 / §8.7）──────────────────────────────
    memory_inject_recent: int = 5       # 每次唤醒注入最近 K 条情节记忆
    memory_inject_limit: int = 20       # 单次注入条数上限
    memory_inject_chars: int = 1500     # 注入的字符上限：**记忆不能挤掉行情本身**（§8.4）
    memory_max_rows: int = 2000         # 每 Agent 的记忆行数上限
    memory_episodic_ttl: int = 30 * 86400
    reflection_every_n: int = 10        # 每 N 次决策触发反思
    reflection_drawdown: float = 0.10   # 回撤超阈值强制反思
    reflection_min_chars: int = 8       # 第 4 问的最短长度：太短必然是废话（§8.3）

    # ── 工具（§4.2）─────────────────────────────────────
    tool_max_chars: int = 6000          # 单次工具返回的字符上限：服务端强制截断
    tool_default_limit: int = 100       # 单次工具返回的行数上限

    # ── LLM（§3.2 / §10.1）──────────────────────────────
    # 温度默认 0：同样的输入要得到同样的输出，这是"可测试、可复盘"的前提。
    llm_temperature: float = 0.0
    # ② ③ 自由段的最大轮数，防死循环（§3.5 超限即放弃）。
    # 原为 8，实测太紧：一个"先看自己 → 取多周期 K 线 → 取指标 → 看信息 → 提案"
    # 的正常流程，工具是**一轮一个**地调的，8 轮经常在提案前就用光 —— 一年回测里
    # 有 160/366 次唤醒因此判成 degraded。放宽到 16，仍能兜住真正的死循环。
    max_tool_rounds: int = 16


DEFAULT = Config()
