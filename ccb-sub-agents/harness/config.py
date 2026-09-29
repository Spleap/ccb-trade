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
    # 注意：本节里除"强平阈值"外，都是**可以被 agents/*.json 按策略覆盖**的默认值
    # （见 `agents.AgentSpec.risk_cfg`）—— 风险偏好是策略画像的一部分，
    # 全局值只是"没写时的兜底"。强平阈值是交易所口径，不随策略变，所以不开放覆盖。
    #
    # 冷却期必须 ≥ 一个唤醒周期，否则它在下一轮唤醒前就过期了，等于没有 ——
    # 与之配套的 agent 是 4h 唤醒，所以这里取 4h。
    # 注意粒度是**整策略**（`agent_runtime.cooldown_until` 单字段，没有标的与方向维度），
    # 所以一次止损会冻结该策略下的所有标的 —— 这是刻意的强纪律，不是按方向冷却。
    cooldown_after_stop: int = 14400    # 止损后整策略冷却 4h（= 一个唤醒周期）
    # 止损距离的允许区间。上限防"名义上设了但形同虚设"；
    # 下限防"止损紧到只是噪声"—— 1h 尺度上给一个 0.05% 的止损，等于开仓即被打掉，
    # 而每一笔还要付手续费。0 = 不限（老配置行为不变）。
    stop_distance_min_pct: float = 0.0
    stop_distance_max_pct: float = 0.5
    max_loss_per_trade_pct: float = 0.02   # ★ 单笔最多亏权益的 2%
    max_drawdown_halt: float = 0.30        # ★ 权益累计回撤 30% -> 只许减仓，不许加风险

    # ── 杠杆（简化口径，见 harness/paper/ledger.py 的账本说明）──────
    # 只做"名义放大 + 强平线"，**不建模逐仓维持保证金与资金费率** ——
    # 账本本身就是保证金口径（现金可为负），所以不需要额外开户。
    # 强平阈值按**名义**算（与真实交易所一致）：权益 ≤ Σ|名义| × 该比例 即强平。
    # 0.005 对应 10x 约 9.5% 的不利波动、20x 约 4.5%。
    maintenance_margin_rate: float = 0.005

    # ── 信息新鲜度（§2.2 单源故障不传染）──────────────────
    # 一个采集源超过这么久没成功过，读工具就不再报"没有新闻"，而报"该源已停摆"。
    # **"事故"和"平静"必须是两条不同的信息** —— 混在一起，LLM 会把采集中断
    # 读成"今天很安静"然后放心下单。6h 对分钟级的源（新闻 3~5min、社媒 10~15min、
    # 情绪指数 1h、预测市场 5min）足够宽松，不会误报。
    info_stale_seconds: int = 21600

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

    # 轨迹（观测层）里**单个字段**的字符上限。工具返回本来就被上面那条截过了，
    # 这里再兜一层是防 LLM 的正文与未来的新字段失控。超了会截断并打 truncated 标记。
    trace_max_chars: int = 4000
    # 要不要把"思考 + 工具调用"落进 `agent_trace`。默认开：用户看得见的输出不该只在
    # 终端上滚一次。关掉只影响观测层，一个字节的决策内容都不会变。
    trace_enabled: bool = True

    # ── LLM（§3.2 / §10.1）──────────────────────────────
    # 温度默认 0：同样的输入要得到同样的输出，这是"可测试、可复盘"的前提。
    llm_temperature: float = 0.0
    # ② ③ 自由段的最大轮数，防死循环（§3.5 超限即放弃）。
    # 原为 8，实测太紧：一个"先看自己 → 取多周期 K 线 → 取指标 → 看信息 → 提案"
    # 的正常流程，工具是**一轮一个**地调的，8 轮经常在提案前就用光 —— 一年回测里
    # 有 160/366 次唤醒因此判成 degraded。放宽到 16，仍能兜住真正的死循环。
    max_tool_rounds: int = 16


DEFAULT = Config()

# 读信息工具 -> 它依赖的采集源（info-feeds/info_feeds/collector/sources.py 的 SOURCES）。
# 只用于"停摆"判定：读回空时，靠它区分"源断了"和"真的没事发生"。
# 空元组 = 这张表还没有采集源接入 —— 那样读工具会如实说"尚未接入采集"，
# 而不是伪装成"没有事件"。
#
# 宏观（`get_macro`）刻意不在表里：FRED 是月度经济数据，源每 12h 才跑一轮，
# 用小时级阈值判它必然误报，而"这个月的 CPI 还没更新"本来就不是风险。
INFO_SOURCES: dict[str, tuple[str, ...]] = {
    "news": ("cryptocurrency_cv", "google_news", "google_news_global"),
    "sentiment": ("reddit", "stocktwits"),
    "sentiment_index": ("fear_greed",),
    "prediction": ("polymarket",),
    "events": (),                      # 交易所公告还没有采集源接入
}
