"""实盘驱动（ARCHITECTURE §9.5）—— 单线程交错跑两个 Loop，并在终端持续输出。

| Loop | 频率 | 碰 LLM | 干什么 |
|---|---|---|---|
| 1 scheduler | 10s | ✅ | 定时唤醒到期的 Agent，跑七步 |
| 2 watchdog | 60s | ❌ | 扫止损/止盈/强平，**不依赖 LLM 可用性** |

**顺序有讲究**：先 Loop 2（保护性退出）、再 Loop 1（唤醒）——
先平仓再唤醒，LLM 看到的才是"平完之后的真实持仓"。

**为什么单线程交错**：两个 Loop 共用一个 SQLite 连接，而 sqlite3 连接不是线程安全的。
它们本来就都是秒级/分钟级的低频活儿，交错跑没有性能问题，却省掉了一整类并发 bug。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Callable, Iterable

from harness import agents, scheduler, signals, trace, watchdog
from harness.clock import RealClock
from harness.config import DEFAULT, Config
from harness.llm import LLMClient
from harness.paper import ledger
from harness.store import repo
from harness.tools.candles import last_close

# 终端每行的时间戳格式
_STAMP = "%H:%M:%S"


def _stdout(line: str) -> None:
    print(f"[{dt.datetime.now().strftime(_STAMP)}] {line}", flush=True)


def _clip(text, n: int) -> str:
    """把一段可能很长的自然语言压成一行，便于终端阅读。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _raw(text: str) -> None:
    """流式片段：不加时间戳、不换行，直接吐给终端 —— 这样才看得到"一个字一个字出来"。"""
    sys.stdout.write(text)
    sys.stdout.flush()


def _args(args) -> str:
    """把工具参数压成一行 `name=value`，长值（列表/字典）走一行 JSON。"""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
    return ", ".join(parts)


class WakeView:
    """把 loop 的事件流渲染成终端上的实时输出（Claude Code 那种观感）。

    它**只负责"看得见"**：不写库、不进 summary、不参与决策。
    """

    def __init__(self, printer: Callable[[str], None] = _stdout, *,
                 stream: bool = True, stamp: Callable[[int], str] | None = None):
        self.printer = printer
        self._raw = _raw if stream else (lambda _t: None)
        self.stamp = stamp
        self._open = False          # 上一段流式文字还没收尾（得补一个换行）

    def _close(self) -> None:
        if self._open:
            self._raw("\n")
            self._open = False

    def __call__(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "wake_start":
            self._close()
            self.printer(f"-- {ev['agent']} 唤醒中 {self._when(ev)}")
        elif kind == "text":
            # 流式正文：首片带缩进，后续接着写
            if not self._open:
                self._raw("      ")
                self._open = True
            self._raw(ev["text"])
        elif kind == "round_end":
            # 这一轮的话说完了 —— 收个尾，让段落断开（后面可能紧跟工具调用，也可能是收场）
            self._close()
        elif kind == "tool_call":
            self._close()
            self._raw(f"   > {ev['name']}({_args(ev.get('args'))})\n")
        elif kind == "tool_result":
            self._raw(f"     = {_clip(ev.get('result'), 220)}\n")   # ★ 220 字符截断在这里，事件本身是完整的
        elif kind == "degraded":
            self._close()
            self._raw(f"   ! {ev.get('reason')}\n")

    def _when(self, ev) -> str:
        ts = ev.get("ts")
        return f"（{self.stamp(int(ts))}）" if (self.stamp and ts) else ""


def _tee(*observers: Callable[[dict], None] | None) -> Callable[[dict], None] | None:
    """把事件流同时发给多个观察者（终端渲染 + 轨迹落库）。

    每个观察者**各自**吞掉自己的异常：一个坏了不该让另一个也瞎掉，
    更不能让它们影响到 `loop`（那里面已经有兜底，这里是双保险）。
    """
    live = [o for o in observers if o is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def fan_out(ev: dict) -> None:
        for observer in live:
            try:
                observer(ev)
            except Exception:
                pass
    return fan_out


def banner(specs: Iterable[agents.AgentSpec], cfg: Config, llm_desc: str,
           printer: Callable[[str], None] = _stdout) -> None:
    specs = list(specs)
    printer("=" * 72)
    printer(f"CCB Sub-Agents · Paper Trading + 交易指令   DB={cfg.db_path}")
    printer(f"LLM={llm_desc}   节奏: Loop1={cfg.scheduler_interval}s "
            f"Loop2={cfg.watchdog_interval}s")
    for s in specs:
        printer(f"  [{s.agent_id}] {s.name}  品种={'/'.join(s.universe)}  "
                f"周期={s.tf}  唤醒={s.wake_interval}s  起始权益={s.starting_equity:.0f}U")
        rc = s.risk_cfg(cfg)
        printer(f"      风险画像：杠杆={s.leverage:g}x  单笔≤{rc.max_loss_per_trade_pct:.1%}  "
                f"熔断={rc.max_drawdown_halt:.0%}  "
                f"止损距离={rc.stop_distance_min_pct:.2%}~{rc.stop_distance_max_pct:.0%}  "
                f"冷却={rc.cooldown_after_stop}s")
        printer(f"      默认退路：{json.dumps(s.default_exit_plan, ensure_ascii=False) if s.default_exit_plan else '（未配 → exit_plan 必须自己给）'}")
    printer("=" * 72)


def run(conn, *, specs: Iterable[agents.AgentSpec], llm: LLMClient, candles,
        cfg: Config = DEFAULT, ticks: int | None = None,
        printer: Callable[[str], None] = _stdout,
        stream: bool = True) -> dict:
    """跑起来。`ticks` 给定时跑完这么多 tick 就返回（预检用），否则一直跑。

    `stream=True` 时会把 LLM 的思考过程与每次工具调用实时打在终端上。
    同时（`cfg.trace_enabled` 时）把它们落进 `agent_trace` —— 终端上滚过去的
    东西是**一次性**的，落库之后才有人事后复盘的抓手（`python -m harness --trace last`）。
    """
    specs = list(specs)
    clock = RealClock()
    view = WakeView(printer, stream=stream)
    recorder = trace.Recorder(conn, cfg=cfg, enabled=cfg.trace_enabled)
    on_event = _tee(view, recorder)
    with conn:
        for s in specs:
            agents.register(conn, s, clock.now())

    # 每个品种用哪个周期：从 spec 反查，免得 watchdog 拿错周期的价
    tf_by_symbol: dict[str, str] = {}
    for s in specs:
        for sym in s.universe:
            tf_by_symbol.setdefault(sym, s.tf)

    # watchdog 的强平判定要按 agent 各自的杠杆来（杠杆 > 1 才启用）
    leverage_by_agent = {s.agent_id: s.leverage for s in specs}
    # 风险偏好也是按 agent 各写一份的 —— 冷却时长尤其，它必须跟自己的唤醒节奏匹配
    cfg_by_agent = {s.agent_id: s.risk_cfg(cfg) for s in specs}

    def price(symbol: str, ts: int) -> float | None:
        try:
            return last_close(candles, symbol, tf_by_symbol.get(symbol, "1h"), ts)
        except Exception as exc:                 # 一次网络抖动不该让整个驱动挂掉
            printer(f"! 取价失败 {symbol}：{exc}")
            return None

    stats = {"ticks": 0, "wakes": 0, "signals": 0, "closed": 0, "statuses": {}}
    last_watchdog = 0
    try:
        while True:
            now = clock.now()

            # ── Loop 2：保护性退出（每 watchdog_interval 一轮）──────────
            if now - last_watchdog >= cfg.watchdog_interval:
                last_watchdog = now
                with conn:
                    closed = watchdog.run_once(conn, clock, price, cfg,
                                               leverage_of=leverage_by_agent.get,
                                               cfg_of=cfg_by_agent.get)
                for c in closed:
                    stats["closed"] += 1
                    printer(f"退出 {c['agent_id']} {c['symbol']}  原因={c['reason']}  "
                            f"@ {c['price']:.6g}  费={c['fee']:.4f}")

            # ── Loop 1：唤醒决策 ─────────────────────────────────────
            results = scheduler.run_once(conn, clock, llm, specs=specs,
                                         candles=candles, cfg=cfg, on_event=on_event)
            for r in results:
                stats["wakes"] += 1
                stats["statuses"][r.status] = stats["statuses"].get(r.status, 0) + 1
                if r.signal:
                    stats["signals"] += 1
                _log_wake(conn, r, printer)

            # 落一次权益点（§7.4）：Loop 1 只在成交时写，驱动补齐"每个 tick 一条曲线"
            _record_equity(conn, specs, price, now, cfg)
            _heartbeat(conn, specs, now, printer)

            stats["ticks"] += 1
            if ticks and stats["ticks"] >= ticks:
                break
            clock.sleep(cfg.scheduler_interval)
    except KeyboardInterrupt:
        printer("收到中断，停止。")
    return stats


# ============================================================
# 终端输出
# ============================================================


def _log_wake(conn, r, printer: Callable[[str], None]) -> None:
    """把一次唤醒的产物摊开 —— 状态、依据、读了什么、成交了什么、**发了什么指令**。"""
    dec = repo.get_decision(conn, r.decision_id) or {}
    said = r.reason or dec.get("reasoning")
    # 把 decision_id 一起印出来 —— 它是事后 `python -m harness --trace <id>` 的把手
    head = f"唤醒 {r.agent_id} -> {r.status}  ｜ id={r.decision_id}"
    if said:
        head += f"  ｜ {_clip(said, 240)}"
    printer(head)

    if dec.get("inputs_summary"):
        printer(f"    读了 {_clip(dec['inputs_summary'], 220)}")
    if r.target_ratio:
        printer("    目标 " + ", ".join(f"{k} ratio={v:+.2f}" for k, v in r.target_ratio.items()))
    for f in r.fills:
        printer(f"    成交 {f['side']} {f['qty']:.6g} {f['symbol']} @ {f['price']:.6g}  "
                f"名义={f['notional']:.2f} 费={f['fee']:.4f} 滑点={f['slippage']:.4f}")
    if r.signal:
        # 这是本框架对外的**唯一交付物**，必须显式打出来
        printer("    " + signals.format_line(r.signal))
    if r.reflection:
        written = r.reflection.get("written")
        printer(f"    反思 {'已写' if written else '未写'}（{r.reflection.get('reason', '')}）")


def _record_equity(conn, specs, price, now: int, cfg: Config) -> None:
    for s in specs:
        prices: dict[str, float] = {}
        for p in repo.get_positions(conn, s.agent_id):
            m = price(p["symbol"], now)
            if m:
                prices[p["symbol"]] = float(m)
        with conn:
            ledger.write_equity(conn, s.agent_id, now, prices, cfg)


def _heartbeat(conn, specs, now: int, printer: Callable[[str], None]) -> None:
    parts = []
    for s in specs:
        rt = repo.get_runtime(conn, s.agent_id) or {}
        left = max(0, int(rt.get("next_wake_at") or 0) - now)
        eq = repo.get_last_equity(conn, s.agent_id)
        eq_s = f"{float(eq['equity']):.2f}U" if eq else "—"
        parts.append(f"{s.agent_id}: 权益 {eq_s} / 下次唤醒 {left}s")
    printer("· " + "  ｜  ".join(parts))


def summary(conn, specs: Iterable[agents.AgentSpec],
            printer: Callable[[str], None] = _stdout) -> None:
    """小结：权益、成交、持仓、指令、决策分布。"""
    printer("=" * 72)
    printer("小结")
    printer("=" * 72)
    for s in specs:
        curve = repo.get_equity_curve(conn, s.agent_id)
        fills = repo.get_fills(conn, s.agent_id)
        positions = repo.get_positions(conn, s.agent_id)
        sigs = repo.recent_signals(conn, s.agent_id, 5)
        start = float(curve[0]["equity"]) if curve else s.starting_equity
        cur = float(curve[-1]["equity"]) if curve else s.starting_equity
        ret = (cur - start) / start * 100.0 if start else 0.0

        printer(f"[{s.agent_id}] {s.name}")
        printer(f"  权益 {cur:.2f} U（起始 {start:.2f}，{ret:+.2f}%）"
                f"  峰值 {max((float(r['equity']) for r in curve), default=cur):.2f} U")
        printer(f"  权益点 {len(curve)} 个  成交 {len(fills)} 笔  手持 {len(positions)} 个仓位")
        for p in positions:
            printer(f"    持仓 {p['symbol']}  qty={float(p['qty']):.6g}  "
                    f"均价={float(p['avg_price']):.6g}")
        for f in fills[-5:]:
            tag = f"（{f['close_reason']}）" if f["close_reason"] else ""
            printer(f"    成交 {f['side']} {float(f['qty']):.6g} {f['symbol']} "
                    f"@ {float(f['price']):.6g} {tag}")
        for sig in reversed(sigs):
            printer(f"    {signals.format_line(sig)}  [{sig['status']}]")
        counts = repo.count_decisions_by_result(conn, s.agent_id)
        if counts:
            printer("  决策 " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        stats = repo.get_stats(conn, s.agent_id, "all")
        if stats:
            printer("  统计 " + ", ".join(f"{k}={v:.4g}" for k, v in sorted(stats.items())))
    printer("=" * 72)
