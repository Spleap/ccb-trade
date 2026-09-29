-- ============================================================
-- info-feeds 信息层 schema（采集进程写）
--
-- 这里是「信息层」的唯一权威定义。harness 那份
--   ccb-sub-agents/schema.sql
-- 里有一份**逐字相同的副本**，因为 harness 要能独立建库跑起来。
-- 改这里就必须同步改那边，反之亦然——两边的表结构必须字节级一致，
-- 否则先后启动的两个进程会各自建出不同的表。
--
-- 约定（与 harness 一致）
--   * 所有时间戳为 INTEGER，Unix 秒（UTC）
--   * 结构化字段用 TEXT 存 JSON
--   * ts 一律是「事件发生时刻」，不是「采集时刻」——
--     否则回放时会出现「新闻比事实晚」的假象。
--
-- 本文件比 harness 那份多一张 collector_state（水位线），
-- 那是采集进程自己的账本，harness 不读。
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ── 信息层 ─────────────────────────────────────────────────

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

-- 采集健康度：单源故障不传染（ARCHITECTURE §2.2）
CREATE TABLE IF NOT EXISTS source_health (
    source               TEXT PRIMARY KEY,
    last_ok_ts           INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);


-- ── 采集进程专有（harness 不读）────────────────────────────
-- 水位线持久化：last_watermark 存 DB 而不是内存，否则重启就断档
-- （ARCHITECTURE §2.2 硬要求 2）。它记的是「该源已成功采集到的最大事件时刻」。
CREATE TABLE IF NOT EXISTS collector_state (
    source          TEXT PRIMARY KEY,
    last_watermark  INTEGER,
    updated_at      INTEGER
);
