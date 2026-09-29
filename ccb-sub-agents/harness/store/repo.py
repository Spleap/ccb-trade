"""仓储层：所有 DB 读写的唯一入口。

权限约定（ARCHITECTURE §2.5）：
  * 信息层（news / social / market_events / ...）对 harness 是**只读**的，
    本模块只提供读函数 —— 写入由 info-feeds 常驻进程负责。
  * 账本层与认知层由 harness 写。

事务约定：本模块**不 commit**，由调用方用 `with conn:` 保证原子性。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

# ============================================================
# agents / 账户
# ============================================================


def create_agent(conn, agent_id: str, name: str, persona: str | None,
                 wake_interval: int, cash: float, ts: int) -> None:
    conn.execute(
        "INSERT INTO agents (agent_id, name, persona, status, wake_interval, cash, created_at) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?)",
        (agent_id, name, persona, wake_interval, cash, ts),
    )
    conn.execute(
        "INSERT OR IGNORE INTO agent_runtime (agent_id, next_wake_at, cooldown_until) "
        "VALUES (?, ?, 0)",
        (agent_id, ts),
    )


def get_agent(conn, agent_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return dict(r) if r else None


def list_agents(conn, status: str | None = "active") -> list[dict]:
    if status is None:
        rows = conn.execute("SELECT * FROM agents ORDER BY agent_id")
    else:
        rows = conn.execute("SELECT * FROM agents WHERE status = ? ORDER BY agent_id", (status,))
    return [dict(r) for r in rows]


def set_cash(conn, agent_id: str, cash: float) -> None:
    conn.execute("UPDATE agents SET cash = ? WHERE agent_id = ?", (cash, agent_id))


# ============================================================
# 运行态（调度与唤醒预算）
# ============================================================


def get_runtime(conn, agent_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM agent_runtime WHERE agent_id = ?", (agent_id,)).fetchone()
    return dict(r) if r else None


def set_next_wake(conn, agent_id: str, next_ts: int, last_wake_ts: int | None = None) -> None:
    """排下一次唤醒。

    `last_wake_ts` 不传就不动它 —— 它记录"上次真的醒过"，而不是"下次要醒"。
    两者混成一个字段，会让"它多久没醒了"这个问题永远算不对。
    """
    if last_wake_ts is None:
        conn.execute("UPDATE agent_runtime SET next_wake_at = ? WHERE agent_id = ?",
                     (next_ts, agent_id))
    else:
        conn.execute(
            "UPDATE agent_runtime SET next_wake_at = ?, last_wake_at = ? WHERE agent_id = ?",
            (next_ts, last_wake_ts, agent_id))


def set_cooldown(conn, agent_id: str, until: int) -> None:
    conn.execute("UPDATE agent_runtime SET cooldown_until = ? WHERE agent_id = ?", (until, agent_id))


def is_cooling_down(conn, agent_id: str, now: int) -> bool:
    rt = get_runtime(conn, agent_id)
    return bool(rt and rt["cooldown_until"] > now)


# ============================================================
# 交易指令（向下游交付的唯一产物）
# ============================================================


def insert_signal(conn, signal: dict) -> None:
    conn.execute(
        "INSERT INTO trade_signals (signal_id, agent_id, decision_id, ts, action, symbol, side, "
        "qty, notional, entry_price, stop_loss, take_profit, trailing_dist, time_stop_sec, "
        "leverage, risk_amount, reason, evidence, status) "
        "VALUES (:signal_id, :agent_id, :decision_id, :ts, :action, :symbol, :side, "
        ":qty, :notional, :entry_price, :stop_loss, :take_profit, :trailing_dist, :time_stop_sec, "
        ":leverage, :risk_amount, :reason, :evidence, :status)",
        signal,
    )


def recent_signals(conn, agent_id: str | None = None, limit: int = 20) -> list[dict]:
    """最近发出的指令（倒序）。终端展示与下游消费都用它。"""
    if agent_id is None:
        rows = conn.execute(
            "SELECT * FROM trade_signals ORDER BY ts DESC LIMIT ?", (limit,))
    else:
        rows = conn.execute(
            "SELECT * FROM trade_signals WHERE agent_id = ? ORDER BY ts DESC LIMIT ?",
            (agent_id, limit))
    return [dict(r) for r in rows]


def supersede_signals(conn, agent_id: str, symbol: str) -> int:
    """把某个标的上一批还没被覆盖的指令标掉 —— 下游据此知道"这条已经不新鲜了"。"""
    cur = conn.execute(
        "UPDATE trade_signals SET status = 'superseded' "
        "WHERE agent_id = ? AND symbol = ? AND status = 'emitted'",
        (agent_id, symbol),
    )
    return cur.rowcount


# ============================================================
# 预算
# ============================================================


def set_budget(conn, agent_id: str, tick_id: str, w: float, gross_cap: float, as_of: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO agent_budget (agent_id, tick_id, w, gross_cap, as_of) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent_id, tick_id, w, gross_cap, as_of),
    )


def get_budget(conn, agent_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM agent_budget WHERE agent_id = ? ORDER BY as_of DESC LIMIT 1", (agent_id,)
    ).fetchone()
    return dict(r) if r else None


# ============================================================
# 持仓
# ============================================================


def get_positions(conn, agent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM agent_positions WHERE agent_id = ? ORDER BY symbol", (agent_id,)
    )
    return [dict(r) for r in rows]


def get_position(conn, agent_id: str, symbol: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM agent_positions WHERE agent_id = ? AND symbol = ?", (agent_id, symbol)
    ).fetchone()
    return dict(r) if r else None


def upsert_position(conn, agent_id: str, symbol: str, qty: float, avg_price: float,
                    exit_plan, peak_price: float | None,
                    opened_at: int, ts: int) -> None:
    """exit_plan 既可能是 dict（刚 resolve 出来），也可能是 DB 里读出来的 JSON 串。"""
    conn.execute(
        "INSERT INTO agent_positions "
        "(agent_id, symbol, qty, avg_price, exit_plan, peak_price, opened_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(agent_id, symbol) DO UPDATE SET "
        "qty=excluded.qty, avg_price=excluded.avg_price, exit_plan=excluded.exit_plan, "
        "peak_price=excluded.peak_price, opened_at=excluded.opened_at, updated_at=excluded.updated_at",
        (agent_id, symbol, qty, avg_price, _jt(exit_plan), peak_price, opened_at, ts),
    )


def set_peak_price(conn, agent_id: str, symbol: str, peak: float, ts: int) -> None:
    """ts 必须由调用方从 Clock 取 —— 直接调 time.time() 会让重跑不可复现。"""
    conn.execute(
        "UPDATE agent_positions SET peak_price = ?, updated_at = ? "
        "WHERE agent_id = ? AND symbol = ?",
        (peak, ts, agent_id, symbol),
    )


def delete_position(conn, agent_id: str, symbol: str) -> None:
    conn.execute("DELETE FROM agent_positions WHERE agent_id = ? AND symbol = ?", (agent_id, symbol))


# ============================================================
# 成交
# ============================================================


def insert_fill(conn, fill: dict) -> None:
    conn.execute(
        "INSERT INTO agent_fills (fill_id, agent_id, decision_id, ts, symbol, side, qty, "
        "price, notional, fee, slippage, close_reason) "
        "VALUES (:fill_id, :agent_id, :decision_id, :ts, :symbol, :side, :qty, "
        ":price, :notional, :fee, :slippage, :close_reason)",
        fill,
    )


def get_fills(conn, agent_id: str, since: int | None = None) -> list[dict]:
    if since is None:
        rows = conn.execute(
            "SELECT * FROM agent_fills WHERE agent_id = ? ORDER BY ts", (agent_id,))
    else:
        rows = conn.execute(
            "SELECT * FROM agent_fills WHERE agent_id = ? AND ts >= ? ORDER BY ts",
            (agent_id, since))
    return [dict(r) for r in rows]


# ============================================================
# 权益
# ============================================================


def insert_equity(conn, agent_id: str, ts: int, cash: float,
                  positions_value: float, equity: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO equity_curve (agent_id, ts, cash, positions_value, equity) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent_id, ts, cash, positions_value, equity),
    )


def get_last_equity(conn, agent_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM equity_curve WHERE agent_id = ? ORDER BY ts DESC LIMIT 1", (agent_id,)
    ).fetchone()
    return dict(r) if r else None


def get_equity_curve(conn, agent_id: str, since: int | None = None,
                     limit: int | None = None) -> list[dict]:
    """**升序**返回权益曲线（旧 -> 新），方便直接算回撤。"""
    sql = "SELECT * FROM equity_curve WHERE agent_id = ?"
    args: list[Any] = [agent_id]
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts"
    if limit is not None:
        sql += " DESC LIMIT ?"
        rows = list(conn.execute(sql, args + [limit]))[::-1]
    else:
        rows = list(conn.execute(sql, args))
    return [dict(r) for r in rows]


# ============================================================
# 决策 / 快照
# ============================================================


def insert_decision(conn, decision_id: str, agent_id: str, ts: int,
                    inputs_summary: str | None = None, reasoning: str | None = None,
                    target_ratio: Any = None, exit_plan: Any = None, orders: Any = None,
                    result: str = "accepted", degraded_reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO agent_decisions (decision_id, agent_id, ts, inputs_summary, reasoning, "
        "target_ratio, exit_plan, orders, result, degraded_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (decision_id, agent_id, ts, inputs_summary, reasoning,
         _j(target_ratio), _j(exit_plan), _j(orders), result, degraded_reason),
    )


def recent_decisions(conn, agent_id: str, limit: int = 5) -> list[dict]:
    """最近几次决策（倒序）。B 类工具 `get_my_recent_decisions` 用。"""
    rows = conn.execute(
        "SELECT * FROM agent_decisions WHERE agent_id = ? ORDER BY ts DESC LIMIT ?",
        (agent_id, limit),
    )
    return [dict(r) for r in rows]


def count_decisions_since(conn, agent_id: str, since: int) -> int:
    r = conn.execute(
        "SELECT COUNT(*) c FROM agent_decisions WHERE agent_id = ? AND ts >= ?",
        (agent_id, since),
    ).fetchone()
    return int(r["c"])


def count_decisions_at(conn, agent_id: str, ts: int) -> int:
    """同一秒内该 Agent 已写了几条决策。

    决策的 id 由 `(agent, 时刻, 这一秒的第几条)` 派生（`harness/ids.py`）——
    一次唤醒里可能既平仓又开仓、同一帧内也可能连写两条，只靠 `(agent, 时刻)`
    会撞主键。序号只用于消歧，不代表任何业务含义。
    """
    r = conn.execute(
        "SELECT COUNT(*) c FROM agent_decisions WHERE agent_id = ? AND ts = ?",
        (agent_id, ts),
    ).fetchone()
    return int(r["c"])


def get_decision(conn, decision_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM agent_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
    return dict(r) if r else None


def count_decisions_by_result(conn, agent_id: str) -> dict[str, int]:
    """按 `result` 统计该 Agent 的决策分布（traded / no_action / degraded …）。

    面板与收尾小结用它回答"它到底做了什么" —— 尤其是"降级了几次"，
    那是判断 LLM 是不是在稳定工作最直接的指标。
    """
    rows = conn.execute(
        "SELECT result, COUNT(*) c FROM agent_decisions WHERE agent_id = ? GROUP BY result",
        (agent_id,),
    )
    return {r["result"]: int(r["c"]) for r in rows}


def get_snapshot(conn, decision_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM market_snapshot WHERE decision_id = ? ORDER BY ts LIMIT 1",
        (decision_id,),
    ).fetchone()
    return dict(r) if r else None


def insert_snapshot(conn, snapshot_id: str, agent_id: str, ts: int,
                    payload: Any, decision_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO market_snapshot (snapshot_id, agent_id, decision_id, ts, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, agent_id, decision_id, ts, _j(payload)),
    )


# ============================================================
# 记忆 / 统计
# ============================================================


def insert_memory(conn, memory_id: str, agent_id: str, ts: int, kind: str, content: str,
                  tags: Any = None, importance: float = 1.0, score: float | None = None,
                  env_fingerprint: str | None = None, decision_id: str | None = None,
                  outcome: float | None = None, expires_at: int | None = None) -> None:
    conn.execute(
        "INSERT INTO agent_memory (memory_id, agent_id, ts, kind, content, tags, importance, "
        "score, env_fingerprint, decision_id, outcome, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, agent_id, ts, kind, content, _j(tags), importance,
         importance if score is None else score, env_fingerprint, decision_id, outcome, expires_at),
    )


def query_memory(conn, agent_id: str, kinds: Iterable[str] | None = None,
                 limit: int = 20, since: int | None = None,
                 fresh_at: int | None = None, contains: str | None = None) -> list[dict]:
    """`fresh_at`：只返回"此刻还没过期"的记忆（§8.6 第 4 条，老情节退出上下文但保留）。

    `contains`：关键词检索，`recall` 工具用（§8.4 pull 路径）。
    """
    sql = "SELECT * FROM agent_memory WHERE agent_id = ?"
    args: list[Any] = [agent_id]
    if kinds:
        kinds = list(kinds)
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if since is not None:
        sql += " AND ts >= ?"
        args.append(since)
    if fresh_at is not None:
        sql += " AND (expires_at IS NULL OR expires_at > ?)"
        args.append(fresh_at)
    if contains:
        sql += " AND content LIKE ?"
        args.append(f"%{contains}%")
    sql += " ORDER BY score DESC, ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def update_memory_score(conn, memory_id: str, score: float) -> None:
    conn.execute("UPDATE agent_memory SET score = ? WHERE memory_id = ?", (score, memory_id))


def delete_memories(conn, memory_ids: Iterable[str]) -> int:
    ids = list(memory_ids)
    if not ids:
        return 0
    conn.execute(
        f"DELETE FROM agent_memory WHERE memory_id IN ({','.join('?' * len(ids))})", ids)
    return len(ids)


def count_memory(conn, agent_id: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) c FROM agent_memory WHERE agent_id = ?", (agent_id,)).fetchone()
    return int(r["c"])


def has_memory_for(conn, decision_id: str) -> bool:
    """派生是幂等的：同一个 decision 重复派生不会写第二条（重跑同一帧时）。"""
    r = conn.execute(
        "SELECT 1 FROM agent_memory WHERE decision_id = ? LIMIT 1", (decision_id,)).fetchone()
    return r is not None


def upsert_stat(conn, agent_id: str, window: str, metric: str, value: float, ts: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO agent_stats (agent_id, window, metric, value, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent_id, window, metric, value, ts),
    )


def get_stats(conn, agent_id: str, window: str = "all") -> dict:
    rows = conn.execute(
        "SELECT metric, value FROM agent_stats WHERE agent_id = ? AND window = ?",
        (agent_id, window),
    )
    return {r["metric"]: r["value"] for r in rows}


# ============================================================
# 信息层（只读）
# ============================================================


def recent_news(conn, as_of: int, since: int, symbols: list[str] | None = None,
                limit: int = 20) -> list[dict]:
    """所有信息查询都强制带 as_of 时间上界（ARCHITECTURE §2.6）。

    这样"忘记加时间过滤"在结构上就是不可能的。
    """
    sql = "SELECT * FROM news_items WHERE ts <= ? AND ts >= ?"
    args: list[Any] = [as_of, since]
    if symbols:
        sql += " AND (" + " OR ".join("symbols LIKE ?" for _ in symbols) + ")"
        args += [f"%{s}%" for s in symbols]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def recent_events(conn, as_of: int, since: int, symbols: list[str] | None = None,
                  limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM market_events WHERE ts <= ? AND ts >= ?"
    args: list[Any] = [as_of, since]
    if symbols:
        sql += " AND (" + " OR ".join("symbols LIKE ?" for _ in symbols) + ")"
        args += [f"%{s}%" for s in symbols]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def recent_social(conn, as_of: int, since: int, symbol: str | None = None,
                  limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM social_items WHERE ts <= ? AND ts >= ?"
    args: list[Any] = [as_of, since]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def recent_sentiment_index(conn, name: str, as_of: int, limit: int = 30) -> list[dict]:
    """升序返回（旧 -> 新），方便直接喂给 LLM 看趋势。"""
    rows = conn.execute(
        "SELECT ts, value FROM sentiment_index WHERE name = ? AND ts <= ? "
        "ORDER BY ts DESC LIMIT ?",
        (name, as_of, limit),
    )
    return [dict(r) for r in rows][::-1]


def recent_macro(conn, series_id: str, as_of: int, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, value FROM macro_series WHERE series_id = ? AND ts <= ? "
        "ORDER BY ts DESC LIMIT ?",
        (series_id, as_of, limit),
    )
    return [dict(r) for r in rows][::-1]


def recent_prediction(conn, topic: str, as_of: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, outcome, prob, volume FROM prediction_quote "
        "WHERE topic = ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
        (topic, as_of, limit),
    )
    return [dict(r) for r in rows]


# ============================================================
# 内部工具
# ============================================================


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def _jt(v: Any) -> str | None:
    """json-or-text：已经是 JSON 字符串的原样落库，避免二次编码。"""
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)
