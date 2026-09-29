-- ============================================================
-- ccb-sub-agents schema
--
-- 约定
--   * 所有时间戳为 INTEGER，Unix 秒（UTC）
--   * 金额为 REAL；符号（symbol）自带计价货币
--   * 结构化字段用 TEXT 存 JSON
--
-- 分层（ARCHITECTURE §2.5）
--   信息层：info-feeds 常驻进程写，harness 只读
--   快照层：K 线不落库的必要补偿（§2.4）
--   账本层：harness 写
--   认知层：harness 写（反思可由 LLM 生成，但入库权在 harness，§8.2）
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ── 信息层（info-feeds 写）──────────────────────────────────
-- ts 一律是"事件发生时刻"，不是"采集时刻"（§2.6）——
-- 否则会出现"新闻比事实晚"的假象。

CREATE TABLE IF NOT EXISTS news_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    summary     TEXT,
    url         TEXT,
    symbols     TEXT,                       -- JSON array
    sentiment   REAL,                       -- 源自带打分，可为 NULL
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_items(ts);

CREATE TABLE IF NOT EXISTS social_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    text        TEXT,
    sentiment   TEXT,                       -- Bullish / Bearish / NULL
    engagement  INTEGER,
    UNIQUE (platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_social_ts ON social_items(ts);

CREATE TABLE IF NOT EXISTS market_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    external_id TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL,           -- listing / delisting / maintenance / regulatory
    symbols     TEXT,
    title       TEXT    NOT NULL,
    body        TEXT,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON market_events(ts);

CREATE TABLE IF NOT EXISTS prediction_quote (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    prob        REAL    NOT NULL,
    volume      REAL,
    UNIQUE (topic, outcome, ts)
);

CREATE TABLE IF NOT EXISTS macro_series (
    series_id   TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    value       REAL    NOT NULL,
    PRIMARY KEY (series_id, ts)
);

CREATE TABLE IF NOT EXISTS sentiment_index (
    name        TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    value       REAL    NOT NULL,
    PRIMARY KEY (name, ts)
);

-- 采集健康度：单源故障不传染（§2.2）
CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    last_ok_ts           INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);


-- ── 快照层（harness 写）────────────────────────────────────
-- 只存"这次决策实际读到的那几个数"，KB 级而非 GB 级（§2.4）。
-- 没有它，K 线不落库就无法复盘归因。

CREATE TABLE IF NOT EXISTS market_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    agent_id    TEXT    NOT NULL,
    decision_id TEXT,
    ts          INTEGER NOT NULL,
    payload     TEXT    NOT NULL            -- JSON：{symbol: {last, rsi14, atr, ...}}
);
CREATE INDEX IF NOT EXISTS idx_snapshot_agent ON market_snapshot(agent_id, ts);


-- ── 账本层（harness 写）────────────────────────────────────

CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    persona       TEXT,                     -- L1 身份层原文
    status        TEXT NOT NULL DEFAULT 'active',   -- active / paused / retired
    wake_interval INTEGER NOT NULL,
    cash          REAL NOT NULL,            -- 该子账户的现金
    created_at    INTEGER NOT NULL
);

-- 运行态：调度（§9.3）
CREATE TABLE IF NOT EXISTS agent_runtime (
    agent_id           TEXT PRIMARY KEY REFERENCES agents(agent_id),
    next_wake_at       INTEGER NOT NULL DEFAULT 0,
    cooldown_until     INTEGER NOT NULL DEFAULT 0,
    last_wake_at       INTEGER
);

-- 主 Agent 每 tick 下发的预算（§8.5：参数只能建议，只有上层能改）
CREATE TABLE IF NOT EXISTS agent_budget (
    agent_id   TEXT NOT NULL,
    tick_id    TEXT NOT NULL,
    w          REAL NOT NULL,               -- 分配权重
    gross_cap  REAL NOT NULL,               -- Σ|·| 口径的总敞口上限
    as_of      INTEGER NOT NULL,
    PRIMARY KEY (agent_id, tick_id)
);

-- 每个子策略独立记账，不做多空抵消。
-- 所以这里的唯一键是 (agent_id, symbol)，不存在"账户级净头寸"。
CREATE TABLE IF NOT EXISTS agent_positions (
    agent_id    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    qty         REAL    NOT NULL,           -- 带符号：+ 多 / - 空
    avg_price   REAL    NOT NULL,           -- 当前敞口的加权开仓价
    exit_plan   TEXT,                       -- JSON，见 §9.4
    peak_price  REAL,                       -- 移动止损用：开仓以来的最有利价
    opened_at   INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (agent_id, symbol)
);

CREATE TABLE IF NOT EXISTS agent_fills (
    fill_id      TEXT PRIMARY KEY,
    agent_id     TEXT    NOT NULL,
    decision_id  TEXT,
    ts           INTEGER NOT NULL,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,          -- buy / sell
    qty          REAL    NOT NULL,          -- 绝对值
    price        REAL    NOT NULL,          -- 含滑点后的成交价
    notional     REAL    NOT NULL,
    fee          REAL    NOT NULL,          -- ★ 与滑点分开记（§7.3）
    slippage     REAL    NOT NULL,
    close_reason TEXT                       -- stop_loss / take_profit / trailing_stop / time_stop / NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_agent ON agent_fills(agent_id, ts);

CREATE TABLE IF NOT EXISTS equity_curve (
    agent_id        TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    cash            REAL    NOT NULL,
    positions_value REAL    NOT NULL,
    equity          REAL    NOT NULL,
    PRIMARY KEY (agent_id, ts)
);


-- ── 指令输出层 ─────────────────────────────────────────────
-- 本框架**不向下游下单**，只产出格式化的交易指令。
-- 每条通过风控的决策在这里落一行，是"交给外部执行方"的唯一契约。
-- 只增不改；同一次决策被后续指令覆盖时，把旧的标 superseded。

CREATE TABLE IF NOT EXISTS trade_signals (
    signal_id      TEXT PRIMARY KEY,
    agent_id       TEXT    NOT NULL,
    decision_id    TEXT,
    ts             INTEGER NOT NULL,
    action         TEXT    NOT NULL,        -- open / increase / reduce / close / reverse
    symbol         TEXT    NOT NULL,
    side           TEXT    NOT NULL,        -- long / short / flat
    qty            REAL    NOT NULL,        -- 带符号：+ 多 / - 空
    notional       REAL    NOT NULL,        -- 带符号的名义额
    entry_price    REAL    NOT NULL,        -- 参考价（下游按自己的盘口成交）
    stop_loss      REAL,                    -- 绝对价；close/flat 时可为 NULL
    take_profit    REAL,                    -- 绝对价
    trailing_dist  REAL,                    -- 移动止损距离（价差）
    time_stop_sec  INTEGER,                 -- 最长持有秒数
    leverage       REAL    NOT NULL DEFAULT 1.0,
    risk_amount    REAL    NOT NULL DEFAULT 0.0,   -- 触发止损时的亏损额
    reason         TEXT,                    -- 决策理由
    evidence       TEXT,                    -- 这一刻它看到了什么（inputs_summary）
    status         TEXT    NOT NULL DEFAULT 'emitted'   -- emitted / superseded
);
CREATE INDEX IF NOT EXISTS idx_signals_agent ON trade_signals(agent_id, ts);


-- ── 认知层（harness 写）────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id     TEXT PRIMARY KEY,
    agent_id        TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    inputs_summary  TEXT,                   -- 本次读到什么（摘要）
    reasoning       TEXT,                   -- LLM 的 reason
    target_ratio    TEXT,                   -- JSON：{symbol: ratio}
    exit_plan       TEXT,
    orders          TEXT,                   -- JSON：实际产生的 fills
    result          TEXT,                   -- accepted / rejected / degraded
    degraded_reason TEXT                    -- §3.5：降级原因，绝不静默失败
);
CREATE INDEX IF NOT EXISTS idx_decisions_agent ON agent_decisions(agent_id, ts);

-- 只追加不修改（§8.7）。修正靠新记忆覆盖旧记忆，保留 audit trail。
-- kind ∈ {episodic, reflection, param_suggestion}
CREATE TABLE IF NOT EXISTS agent_memory (
    memory_id       TEXT PRIMARY KEY,
    agent_id        TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    kind            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    tags            TEXT,                   -- JSON array：skill / symbol / regime
    importance      REAL    NOT NULL DEFAULT 1.0,
    score           REAL    NOT NULL DEFAULT 1.0,   -- importance × recency，淘汰用
    env_fingerprint TEXT,                   -- 环境指纹，防自我强化偏差（§8.6）
    decision_id     TEXT,
    outcome         REAL,                   -- 该决策的实际结果（收益率）
    expires_at      INTEGER                 -- 超过此时刻退出上下文，仍进统计
);
CREATE INDEX IF NOT EXISTS idx_memory_agent ON agent_memory(agent_id, kind, ts);

-- 纯代码聚合，LLM 不碰（§8.2 路径 C）。
-- 它是防自我强化偏差的主力：客观数字压过主观叙事（§8.6）。
CREATE TABLE IF NOT EXISTS agent_stats (
    agent_id   TEXT    NOT NULL,
    window     TEXT    NOT NULL,            -- all / 7d / 30d
    metric     TEXT    NOT NULL,            -- win_rate / max_drawdown / pnl / ...
    value      REAL    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (agent_id, window, metric)
);
