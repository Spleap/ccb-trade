"""记忆层（ARCHITECTURE §8）—— 三条写入路径，没有一条是"LLM 自由写"。

| 路径 | 谁生成 | 怎么触发 | 落库 kind |
|---|---|---|---|
| A 情节 | **纯代码派生** | 每次决策后自动 | `episodic` |
| B 反思 | LLM 生成，**代码决定何时写、写什么格式** | 代码设定的触发点 | `reflection` |
| C 统计 | **纯代码聚合** | 每次决策后 | `agent_stats` 表 |

读取也是两条路（§8.4）：

| 方式 | 谁发起 | 作用 |
|---|---|---|
| **注入** `inject_block()` | harness 主动 push | 保证基本盘：不会因为"忘了搜"而失忆 |
| **检索** `tools/memory.recall()` | LLM 主动 pull | 给主动性 |

为什么对 LLM 这么不信任（§8.3）
--------------------------------
**LLM 写自己的记忆 = 给自己下达未来的行为指令。** 这是一条能绕过所有风控的路径 ——
如果它能写"下次可以放宽止损"，那 §9.4 那条"止损只能收紧"就在下一轮被绕过了。
所以：情节由代码派生、统计由代码聚合，唯一放开的是"反思"，而且它要过闸门。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from harness.config import DEFAULT, Config
from harness.exit_plan import STOP_REASONS
from harness.llm import LLMClient, LLMError
from harness.store import repo

EPISODIC = "episodic"
REFLECTION = "reflection"
SUGGESTION = "param_suggestion"          # 参数建议只入库、不生效（§8.5）


# ============================================================
# 环境指纹（§8.6 防线 1）
# ============================================================


def env_fingerprint(snapshot: dict) -> str | None:
    """只用"客观、可比、与事件无关"的量 —— 各标的 `ATR / 现价` 的中位数。

    它回答一个问题：**当时市场有多颠？** 读取记忆时拿它和现在的指纹比一下，
    差异太大就标一句"这条记忆来自另一个市场状态"，防止拿低波动的经验套高波动的行情。
    """
    vals: list[float] = []
    for cell in (snapshot or {}).values():
        atr, px = cell.get("atr14"), cell.get("last_close")
        if atr and px and float(px) > 0:
            vals.append(float(atr) / float(px))
    if not vals:
        return None
    vals.sort()
    return f"atr_pct={vals[len(vals) // 2] * 100:.2f}"


def current_fingerprint(universe, candles, tf: str, as_of: int) -> str | None:
    """此刻的环境指纹。

    注意它**不进决策快照** —— 决策快照的语义是"LLM 当时真的看到了什么"（§2.4），
    而这里算 ATR 只是为了记忆卫生。两者混在一起会让快照说谎。
    """
    from harness.tools import indicators

    if candles is None:
        return None
    snap: dict = {}
    for symbol in universe:
        try:
            bars = candles.fetch(symbol, tf, 200, as_of)
        except Exception:
            continue
        if not bars:
            continue
        values = indicators.compute(["atr14"], bars)
        if values.get("atr14") is not None:
            snap[symbol] = {"atr14": values["atr14"], "last_close": float(bars[-1]["close"])}
    return env_fingerprint(snap)


def _atr_pct(fingerprint: str | None) -> float | None:
    if not fingerprint:
        return None
    try:
        return float(fingerprint.partition("=")[2])
    except ValueError:
        return None


def _regime_warn(was: str | None, now: float | None) -> str:
    """差异大到 2 倍以上才提示 —— 否则每条记忆都挂个警告，等于没警告。"""
    old = _atr_pct(was)
    if not old or not now or old <= 0:
        return ""
    ratio = now / old
    if 0.5 <= ratio <= 2.0:
        return ""
    return f" ⚠ 该记忆来自不同的市场状态（当时波动是现在的 {ratio:.1f} 倍）"


# ============================================================
# A 路径：情节记忆（纯代码派生）
# ============================================================


def record_episode(conn, agent_id: str, decision_id: str, ts: int,
                   cfg: Config = DEFAULT) -> str | None:
    """从刚落库的 decision + snapshot 派生一条情节记忆。**不调 LLM，不抛异常。**

    为什么必须代码派生：LLM 会把"我以为"写成"我观察到的"，
    而情节记忆的用处正是**事后归因** —— 它一旦被主观污染，统计与反思全建在沙子上。
    """
    if repo.has_memory_for(conn, decision_id):          # 幂等：重跑同一帧不会写第二条
        return None
    dec = repo.get_decision(conn, decision_id)
    if dec is None:
        return None

    snap_row = repo.get_snapshot(conn, decision_id)
    saw = _loads(snap_row["payload"]) if snap_row else {}
    target = _loads(dec["target_ratio"]) or {}
    orders = _loads(dec["orders"]) or []

    content = {
        "decision": dec["result"],
        "target_ratio": target or None,
        "reason": dec["reasoning"],
        "saw": saw,                                   # 当时实际读到的数
        "fills": [{k: f.get(k) for k in ("symbol", "side", "qty", "price", "fee", "close_reason")}
                  for f in orders if isinstance(f, dict)],
        "note": dec["degraded_reason"],
    }

    importance = 1.0
    if dec["result"] in ("rejected", "degraded"):
        importance = 1.5                              # 撞过的墙比顺手的操作更值得记
    if any(f.get("close_reason") in STOP_REASONS for f in orders if isinstance(f, dict)):
        importance = 2.0                              # 被止损打掉：最值得复盘

    tags = sorted(set(target) | set(saw))
    tags += sorted({f["close_reason"] for f in orders
                    if isinstance(f, dict) and f.get("close_reason")})
    tags.append(str(dec["result"]))

    # 情节天然归属于决策：id 直接派生，重跑两遍必然是同一行。
    # 幂等也就不用再靠"查重"来兜底了。
    memory_id = f"ep-{decision_id}"
    repo.insert_memory(
        conn, memory_id, agent_id, ts, EPISODIC,
        json.dumps(content, ensure_ascii=False, default=str),
        tags=tags, importance=importance,
        env_fingerprint=env_fingerprint(saw), decision_id=decision_id,
        expires_at=ts + cfg.memory_episodic_ttl,
    )
    return memory_id


# ============================================================
# C 路径：统计记忆（纯代码聚合）
# ============================================================


def update_stats(conn, agent_id: str, ts: int, cfg: Config = DEFAULT) -> dict:
    """滚动聚合。

    **它是防自我强化偏差的主力**（§8.6 第 2 条）：客观数字压过主观叙事。
    叙事可以骗人，胜率不能。所以注入时统计永远排在最前面。
    """
    out: dict = {}
    for window, since in (("all", None), ("30d", ts - 30 * 86400), ("7d", ts - 7 * 86400)):
        metrics = _metrics(conn, agent_id, since)
        for name, value in metrics.items():
            repo.upsert_stat(conn, agent_id, window, name, value, ts)
        out[window] = metrics
    return out


def _metrics(conn, agent_id: str, since: int | None) -> dict:
    curve = repo.get_equity_curve(conn, agent_id, since)
    fills = repo.get_fills(conn, agent_id, since)
    m: dict[str, float] = {}

    if curve:
        m["equity"] = float(curve[-1]["equity"])
        start = float(curve[0]["equity"])
        if start > 0:
            m["return"] = m["equity"] / start - 1.0
        peak = start
        worst = 0.0
        for row in curve:
            peak = max(peak, float(row["equity"]))
            if peak > 0:
                worst = max(worst, (peak - float(row["equity"])) / peak)
        m["max_drawdown"] = worst

    if fills:
        m["fills"] = float(len(fills))
        m["fee_paid"] = sum(float(f["fee"] or 0) for f in fills)
        m["slippage_paid"] = sum(float(f["slippage"] or 0) for f in fills)
        # 胜率按**平仓原因**统计（止盈次数 / (止盈+止损) 次数）。
        # 之所以不用金额口径：Paper 账本没有逐笔实现盈亏，硬算出来的数会假装精确。
        tp = sum(1 for f in fills if f["close_reason"] == "take_profit")
        sl = sum(1 for f in fills if f["close_reason"] in STOP_REASONS)
        if tp + sl:
            m["stop_outs"] = float(sl)
        if tp:
            # 一次止盈都没有时不报胜率：`0/1 = 0%` 不是"胜率为零"，
            # 而是"还没有样本"，报出去会被当成结论（§8.6 数字必须诚实）。
            m["win_rate"] = tp / (tp + sl)
    return m


# ============================================================
# B 路径：反思（LLM 生成 + 代码闸门）
# ============================================================


def reflection_trigger(conn, agent_id: str, ts: int, cfg: Config = DEFAULT) -> str | None:
    """**触发点由代码决定，不由 LLM 决定要不要反思。**（§8.3）

    三个触发点，命中一个就反思：平仓后（尤其止损）/ 每 N 次决策 / 回撤超阈值。
    """
    base = _last_reflection_ts(conn, agent_id)

    fills = repo.get_fills(conn, agent_id, since=base)
    stopped = [f for f in fills if f["close_reason"] in STOP_REASONS]
    if stopped:
        last = stopped[-1]
        return f"被止损平掉 {len(stopped)} 次（最近 {last['symbol']}｜{last['close_reason']}）"
    if any(f["close_reason"] for f in fills):
        return f"有 {sum(1 for f in fills if f['close_reason'])} 次平仓发生"

    n = repo.count_decisions_since(conn, agent_id, base)
    if n >= cfg.reflection_every_n:
        return f"距上次反思已累计 {n} 次决策"

    # 回撤超阈值。加一个"期间确实有新决策"的前提，否则一旦深水区就会每 tick 都反思。
    dd = repo.get_stats(conn, agent_id, "all").get("max_drawdown") or 0.0
    if dd >= cfg.reflection_drawdown and n > 0:
        return f"最大回撤 {dd:.1%} 超过阈值 {cfg.reflection_drawdown:.0%}"
    return None


REFLECTION_SYSTEM = """你在做一次事后复盘。这是一次**只读**的复盘：你不会下单，也不会改变任何规则。

只回答下面四个问题，用 `1.` `2.` `3.` `4.` 编号，不要写别的客套话：

1. 我当时的判断是什么？
2. 实际发生了什么？
3. 这两者的差异在哪？
4. 下次遇到类似情况，我应该怎么做？

硬要求：
- **第 4 问必须给出一个可执行的具体动作**，要带条件或数字。
  例：`当 RSI > 70 且成交量没有放大时不开多`。写成"要更加谨慎"这类话，整条反思会被丢弃。
- **不许提改规则的结论**（放宽止损、提高预算、延长冷却期）—— 参数不归你调，你只能建议。
- **不许预测未来行情**。记忆是复盘，不是预言。
- 不许引用上面材料里没有的事实。"""


def maybe_reflect(conn, agent_id: str, ts: int, *, llm: LLMClient,
                  cfg: Config = DEFAULT) -> dict:
    """命中触发点就叫一次 LLM 生成反思，过闸门才入库。

    返回 `{"written": bool, "reason": str, ...}`。
    没写不是失败 —— 和"没数据"一样，它是正常结局（§3.5）。
    """
    trigger = reflection_trigger(conn, agent_id, ts, cfg)
    if trigger is None:
        return {"written": False, "reason": "未命中触发点"}

    material = _reflection_material(conn, agent_id, ts, cfg)
    try:
        reply = llm.chat([{"role": "system", "content": REFLECTION_SYSTEM},
                          {"role": "user", "content": material}],
                         tools=None, temperature=cfg.llm_temperature)
    except LLMError as exc:
        return {"written": False, "reason": f"LLM 不可用：{exc}"}

    text = (reply.content or "").strip()
    ok, why = gate_reflection(text, cfg)
    if not ok:
        # 丢弃而不是"修一修"：闸门被绕过一次，这条路径就废了（§8.3）
        return {"written": False, "reason": f"反思未过闸门：{why}"}

    # 反思的 id 由 (agent, 时刻) 派生：重跑两遍必然是同一行。
    # 一次唤醒最多一条反思，所以这个键不会撞。
    memory_id = f"ref-{agent_id}-{ts}"
    repo.insert_memory(conn, memory_id, agent_id, ts, REFLECTION, text,
                       tags=_recent_tags(conn, agent_id), importance=2.0,
                       env_fingerprint=_recent_fingerprint(conn, agent_id))
    return {"written": True, "memory_id": memory_id, "trigger": trigger, "gate": None}


_RULE_BANS = ("放宽", "提高预算", "增加预算", "延长冷却", "放大止损", "调高止损")
_FORECAST_BANS = ("预测", "会涨", "会跌", "必然", "肯定会", "一定涨", "一定跌")
_ACTION_MARKERS = ("时", "若", "如果", "超过", "低于", "高于", "大于", "小于", "只", "先")
_SECTION_RE = re.compile(r"(?m)^\s*([1-4])[.、)]\s*")


def gate_reflection(text: str, cfg: Config = DEFAULT) -> tuple[bool, str | None]:
    """写入门槛。**第 4 问是硬门槛。**（§8.3）

    通过：`"RSI > 70 且无成交量确认时不开多"`
    丢弃：`"市场很难预测，要更加谨慎"` —— 这不是学习，这是废话占位
    """
    text = text or ""
    parts = _split_sections(text)
    if len(parts) < 4:
        return False, "必须按 1.~4. 回答全部四问"

    for word in _RULE_BANS:
        if word in text:
            return False, f"含改规则的结论「{word}」—— 参数不归它调，只能建议（§8.5）"

    q4 = parts[4].strip()
    if len(q4) < cfg.reflection_min_chars:
        return False, f"第 4 问只有 {len(q4)} 个字，必然是废话"
    for word in _FORECAST_BANS:
        if word in q4:
            return False, f"第 4 问含对未来的预测「{word}」—— 记忆是复盘，不是预言"
    if not any(mk in q4 for mk in _ACTION_MARKERS) and not re.search(r"\d", q4):
        return False, "第 4 问没有可执行的具体动作（既无条件词也没有数字）"
    return True, None


def _split_sections(text: str) -> dict[int, str]:
    """按 `1.` ~ `4.` 切开。不要求顺序完美，但必须四条都在。"""
    hits = list(_SECTION_RE.finditer(text))
    out: dict[int, str] = {}
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        out[int(hit.group(1))] = text[hit.end():end]
    return out


def _reflection_material(conn, agent_id: str, ts: int, cfg: Config) -> str:
    """给反思的材料：**只给事实，不给判断** —— 判断正是它要产出的东西。"""
    lines = ["# 复盘材料", ""]

    decs = repo.recent_decisions(conn, agent_id, 5)
    if decs:
        lines.append("## 你最近的决策（新 -> 旧）")
        for r in decs:
            bits = [f"- [{utc_str(r['ts'])}] result={r['result']}"]
            if r["target_ratio"]:
                bits.append(f"target={r['target_ratio']}")
            if r["reasoning"]:
                bits.append(f"reason={_oneline(r['reasoning'], 200)}")
            if r["degraded_reason"]:
                bits.append(f"note={_oneline(r['degraded_reason'], 120)}")
            lines.append("｜".join(bits))

    curve = repo.get_equity_curve(conn, agent_id)
    if curve:
        first, last = curve[0], curve[-1]
        chg = (float(last["equity"]) / float(first["equity"]) - 1.0) * 100 if first["equity"] else 0.0
        lines += ["", "## 之后实际发生了什么（客观数字）",
                  f"- 权益：{float(first['equity']):.2f} → {float(last['equity']):.2f}（{chg:+.2f}%）"]

    totals = _metrics(conn, agent_id, None)
    if totals:
        lines.append("- 累计：" + "，".join(
            f"{k}={v:.4g}" for k, v in sorted(totals.items()) if k != "equity"))

    fills = [f for f in repo.get_fills(conn, agent_id) if f["close_reason"]]
    if fills:
        lines.append("- 最近的平仓：")
        for f in fills[-5:]:
            lines.append(f"  · [{utc_str(f['ts'])}] {f['symbol']} {f['side']} {f['qty']:.6g}"
                         f" @ {f['price']:.6g}｜原因 {f['close_reason']}")

    lines += ["", "现在按 1.~4. 回答。"]
    return "\n".join(lines)


# ============================================================
# 读取：注入（push）
# ============================================================


def inject_block(conn, agent_id: str, ts: int, *, cur_fingerprint: str | None = None,
                 symbols: list[str] | None = None, cfg: Config = DEFAULT) -> str | None:
    """每次唤醒固定注入的内容（§8.4）。返回 None 表示还没有记忆可注入。

    顺序是有讲究的：**统计在前，反思在后，情节垫底** ——
    客观数字优先于主观叙事（§8.6 第 2 条），角色最近发生的事垫在最后收尾。
    """
    from harness.tools.base import truncate

    stats = repo.get_stats(conn, agent_id, "all")
    reflections = repo.query_memory(conn, agent_id, kinds=[REFLECTION],
                                    limit=3, fresh_at=ts)
    recent = repo.query_memory(conn, agent_id, kinds=[EPISODIC],
                               limit=cfg.memory_inject_recent, fresh_at=ts)

    same: list[dict] = []
    if symbols:                                       # "同品种的历史决策"
        seen = {r["memory_id"] for r in recent}
        for sym in symbols[:2]:
            for r in repo.query_memory(conn, agent_id, limit=3, fresh_at=ts, contains=sym):
                if r["memory_id"] not in seen:
                    seen.add(r["memory_id"])
                    same.append(r)

    if not (stats or reflections or recent or same):
        return None

    cur = _atr_pct(cur_fingerprint)
    lines: list[str] = []
    if stats:
        lines += ["【统计（客观数字，优先级高于任何叙事）】", _fmt_stats(stats)]
    if reflections:
        lines += ["【反思（你自己总结的）】"] + [_fmt_memory(r, cur) for r in reflections]
    if same:
        lines += ["【同品种的历史】"] + [_fmt_memory(r, cur) for r in same]
    if recent:
        lines += ["【最近的情节】"] + [_fmt_memory(r, cur) for r in recent]

    lines += ["", "（记忆只供参考。**现价与指标永远以本轮工具返回的为准**，不要用记忆里的旧数字。）"]
    return truncate("\n".join(lines), cfg.memory_inject_chars)


def search(conn, agent_id: str, query: str, k: int = 5, ts: int | None = None) -> list[dict]:
    """关键词检索（§8.4 pull 路径）。

    第一版不引 embedding：记忆量是千级，关键词够用、可解释、零依赖。
    匹配不到就返回空 —— **不退化成一堆不相关的记忆**，那比没有更糟。
    """
    kw = keyword(query)
    return repo.query_memory(conn, agent_id, limit=k, fresh_at=ts, contains=kw or None)


def keyword(query: str) -> str:
    """挑一个能进 SQL LIKE 的词。优先 ASCII 词（品种 `BTC/USDT`、指标 `rsi14` 这类
    恰好是最有检索价值的）；中文没有空格，硬切词反而不如直接匹配短语。"""
    tokens = re.findall(r"[A-Za-z0-9/._-]{2,}", query or "")
    if tokens:
        return max(tokens, key=len)
    return (query or "").strip()[:16]


# ============================================================
# 生命周期：衰减与淘汰（§8.7）
# ============================================================


def decay_and_prune(conn, agent_id: str, ts: int, cfg: Config = DEFAULT) -> dict:
    """`score = importance × recency`，超上限按 score 淘汰。

    **只追加不修改**的例外：`score` 是派生量，重算它不改变记忆内容，
    也不动 `importance` —— 原始记录始终留着（audit trail）。
    """
    rows = repo.query_memory(conn, agent_id, limit=cfg.memory_max_rows + 500)
    half_life = max(float(cfg.memory_episodic_ttl), 86400.0)

    scored: list[tuple[float, int, str, dict]] = []
    for r in rows:
        recency = 0.5 ** (max(0, ts - int(r["ts"])) / half_life)
        score = float(r["importance"]) * recency
        repo.update_memory_score(conn, r["memory_id"], score)
        scored.append((score, int(r["ts"]), r["memory_id"], r))

    over = len(rows) - cfg.memory_max_rows
    if over <= 0:
        return {"scored": len(rows), "pruned": 0}

    # 淘汰顺序必须确定：先按 score 低的，再按更老的，最后按 id —— 重跑才可复现
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    pruned = repo.delete_memories(conn, [t[2] for t in scored[:over]])
    return {"scored": len(rows), "pruned": pruned}


# ============================================================
# 内部
# ============================================================


def _last_reflection_ts(conn, agent_id: str) -> int:
    rows = repo.query_memory(conn, agent_id, kinds=[REFLECTION], limit=1)
    return int(rows[0]["ts"]) if rows else 0


def _recent_tags(conn, agent_id: str) -> list[str]:
    """反思的标签 = 最近碰过的品种。

    品种既可从"最近的提案"来，也要从"最近的成交"来 ——
    一次止损平仓可能根本没有提案记录（仓位是更早开的），
    只认提案会让最该被检索到的那条反思变成无标签的死记录。
    """
    tags: list[str] = []
    for r in repo.recent_decisions(conn, agent_id, 3):
        tags += list(_loads(r["target_ratio"]) or {})
    for f in repo.get_fills(conn, agent_id)[-5:]:
        tags.append(f["symbol"])
    return sorted(set(tags))


def _recent_fingerprint(conn, agent_id: str) -> str | None:
    for r in repo.recent_decisions(conn, agent_id, 3):
        snap = repo.get_snapshot(conn, r["decision_id"])
        if snap and snap["payload"]:
            fp = env_fingerprint(_loads(snap["payload"]) or {})
            if fp:
                return fp
    return None


def _fmt_stats(stats: dict) -> str:
    label = {"equity": "权益", "return": "累计收益", "max_drawdown": "最大回撤",
             "win_rate": "胜率(平仓原因口径)", "stop_outs": "被止损次数",
             "fills": "成交笔数", "fee_paid": "累计手续费", "slippage_paid": "累计滑点"}
    bits = []
    for key in ("equity", "return", "max_drawdown", "win_rate", "stop_outs",
                "fills", "fee_paid", "slippage_paid"):
        if key not in stats:
            continue
        val = float(stats[key])
        bits.append(f"{label[key]} {val:.2%}" if key in ("return", "max_drawdown", "win_rate")
                    else f"{label[key]} {val:,.2f}")
    return "- " + "｜".join(bits) if bits else "- （暂无）"


def _fmt_memory(row: dict, cur_atr_pct: float | None) -> str:
    when = utc_str(row["ts"])[:16]
    warn = _regime_warn(row["env_fingerprint"], cur_atr_pct)
    if row["kind"] == EPISODIC:
        body = format_episode(row["content"])
    else:
        body = " ".join(_oneline(row["content"], 400).split())
    return f"- [{when}]{warn} {body}"


def format_episode(content: str) -> str:
    d = _loads(content)
    if not isinstance(d, dict):
        # 不是结构化 JSON 的情节（人工塞的、迁移来的）只能原样呈现，
        # 总好过显示一个"?" —— 那等于告诉 LLM"这条记忆是空的"。
        return _oneline(content, 400)
    bits = [str(d.get("decision") or "?")]
    for sym, ratio in (d.get("target_ratio") or {}).items():
        try:
            bits.append(f"{sym} {float(ratio):+.2f}")
        except (TypeError, ValueError):
            bits.append(f"{sym} {ratio}")
    if d.get("reason"):
        bits.append(_oneline(d["reason"], 160))
    fills = d.get("fills") or []
    if fills:
        f = fills[0]
        bits.append(f"成交 {f.get('side')} {f.get('qty')}@{f.get('price')}")
        if f.get("close_reason"):
            bits.append(f"平仓原因 {f['close_reason']}")
    if d.get("note"):
        bits.append(f"备注 {_oneline(d['note'], 100)}")
    return "｜".join(b for b in bits if b)


def _oneline(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _loads(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def utc_str(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
