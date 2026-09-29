"""exit_plan：开仓那一刻就写定的退路（ARCHITECTURE §9.4）。

你的原话是"做交易就必须得做好止盈止损"，这份文件把它变成**结构**而不是**提醒**。

三条设计要点
------------
1. **在提案时刻把相对口径（ATR / 百分比）解析成绝对价。**
   之后 watchdog 只做纯价格比较 —— 不碰指标、不碰 K 线、不碰 LLM。
   这是"保护性退出不依赖唤醒"能够成立的前提（§9.4 L2）。
2. **止损只能收紧，不能放宽。** `amend` 只接受朝有利方向的移动；
   想把止损推远必须由上层批准（§9.4 防作弊 1）。
3. 本模块只做"计划"的算术，**不碰 DB、不碰时钟**。

落库形态（`agent_positions.exit_plan`，JSON）
--------------------------------------------
    {
      "stop_loss":       63200.0,     # 绝对价，None = 未设
      "take_profit":     66400.0,
      "trailing_dist":   480.0,       # 移动止损距离（绝对量，= mult × ATR）
      "time_stop_seconds": 172800,    # 自 opened_at 起算的存活上限
      "spec":            {...}        # 原始提案，留痕用
    }
"""
from __future__ import annotations

import json
from typing import Any

from harness.config import DEFAULT, Config


class PlanError(ValueError):
    """exit_plan 不合规 —— ⑤ Validate 直接拒，不进入执行。"""


# 相对仓位的方向：止损在不利侧（-1），止盈在有利侧（+1）
_FAVOR = {"stop_loss": -1, "take_profit": +1}

# 触发后要进冷却期的原因（§9.4 防作弊 2：防报复性交易）
STOP_REASONS = ("stop_loss", "trailing_stop")


# ============================================================
# 解析：相对口径 -> 绝对价
# ============================================================


def resolve(spec: dict | None, side: int, entry_price: float,
            atr: float | None = None, bar_seconds: int | None = None) -> dict:
    """把提案解析成落库形态。

    spec: {"stop_loss": {"type": "atr"|"pct"|"price", "value": 2.0},
           "take_profit": {...},
           "trailing":   {"mult": 1.5},
           "time_stop":  {"max_bars": 48} 或 {"seconds": 3600}}
    side: +1 做多 / -1 做空
    """
    if side not in (1, -1):
        raise PlanError(f"side 必须是 +1 或 -1，收到 {side!r}")
    if entry_price <= 0:
        raise PlanError(f"开仓价必须为正，收到 {entry_price!r}")

    spec = spec or {}
    plan: dict[str, Any] = {
        "stop_loss": None,
        "take_profit": None,
        "trailing_dist": None,
        "time_stop_seconds": None,
        "spec": spec,
    }

    for key, favor in _FAVOR.items():
        if spec.get(key):
            plan[key] = _resolve_level(spec[key], entry_price, atr, side, favor)

    trailing = spec.get("trailing")
    if trailing and trailing.get("enabled", True):
        mult = trailing.get("mult")
        if mult is None or mult <= 0:
            raise PlanError("trailing 需要正的 mult")
        if atr is None or atr <= 0:
            raise PlanError("trailing 用 ATR 口径，但没有可用的 ATR")
        plan["trailing_dist"] = float(atr) * float(mult)

    time_stop = spec.get("time_stop")
    if time_stop:
        if "seconds" in time_stop:
            plan["time_stop_seconds"] = int(time_stop["seconds"])
        elif "max_bars" in time_stop:
            if not bar_seconds:
                raise PlanError("time_stop 用 max_bars 口径，但没有 bar_seconds")
            plan["time_stop_seconds"] = int(time_stop["max_bars"] * bar_seconds)
        else:
            raise PlanError("time_stop 需要 seconds 或 max_bars")

    return plan


def _resolve_level(level: dict, ref_price: float, atr: float | None,
                   side: int, favor: int) -> float:
    kind = level.get("type")
    value = level.get("value")
    if value is None:
        raise PlanError(f"止盈/止损缺少 value：{level!r}")

    if kind == "price":
        return float(value)          # 已经是绝对价，不再换算

    if kind == "atr":
        if atr is None or atr <= 0:
            raise PlanError("用了 atr 口径，但没有可用的 ATR")
        dist = abs(float(atr)) * abs(float(value))
    elif kind == "pct":
        dist = ref_price * abs(float(value))
    else:
        raise PlanError(f"未知的止盈/止损口径 {kind!r}（只支持 price / atr / pct）")

    if dist <= 0:
        raise PlanError("止盈/止损距离必须为正")
    # 多头的止盈在上方、止损在下方；空头反之 —— 用 side × favor 统一表达
    return ref_price + (side * favor) * dist


# ============================================================
# 校验（⑤ Validate 的一部分，§7.4）
# ============================================================


def validate(plan: dict, side: int, entry_price: float,
             cfg: Config = DEFAULT, leverage: float = 1.0) -> tuple[bool, str | None]:
    """返回 (ok, reason)。不 ok 时 reason 会写进 agent_decisions.result。

    这里只管**退路本身合不合规**（有没有、方向对不对、离得多远）。
    "这一笔最多亏多少"是另一回事，由 `tools/decision.py` 的 ⑤ 用本函数返回的
    止损价去算 —— 那需要知道仓位大小，而本函数看不到。
    """
    stop = plan.get("stop_loss")
    if stop is None:
        return False, "缺少止损：做交易就必须在开仓那一刻写好退路（§9.4）"

    dist = abs(entry_price - stop)
    if dist <= 0:
        return False, "止损距离为 0（名义上设了，形同虚设）"
    if dist > abs(entry_price) * cfg.stop_distance_max_pct:
        return False, (f"止损距离 {dist:.6g} 超过上限 "
                       f"{cfg.stop_distance_max_pct:.0%} × 开仓价 {entry_price:.6g}")

    if side > 0 and stop >= entry_price:
        return False, "做多的止损必须低于开仓价"
    if side < 0 and stop <= entry_price:
        return False, "做空的止损必须高于开仓价"

    take = plan.get("take_profit")
    if take is None:
        return False, "缺少止盈：每一笔都要有止盈止损（§9.4）"
    if side > 0 and take <= entry_price:
        return False, "做多的止盈必须高于开仓价"
    if side < 0 and take >= entry_price:
        return False, "做空的止盈必须低于开仓价"

    # ★ 杠杆下：止损必须**紧于强平线**，否则价格还没走到止损就先被强平 ——
    # 那条止损等于不存在。这条以前只写在提示词里，靠劝不靠拦，必须补成硬校验。
    lev = max(1.0, float(leverage or 1.0))
    if lev > 1:
        liq_dist = abs(entry_price) * (1.0 / lev - cfg.maintenance_margin_rate)
        if dist >= liq_dist:
            return False, (f"止损距离 {dist:.6g} 已到/超过 {lev:g}x 的强平距离 "
                           f"{liq_dist:.6g}（≈ {(1.0 / lev - cfg.maintenance_margin_rate):.2%} 的不利波动）"
                           f"—— 价格碰到止损前会先强平，这条止损形同虚设")

    return True, None


# ============================================================
# 触发判定（watchdog 每分钟调用，纯比较）
# ============================================================


def load(exit_plan: str | dict | None) -> dict | None:
    if exit_plan is None:
        return None
    if isinstance(exit_plan, dict):
        return exit_plan
    try:
        return json.loads(exit_plan)
    except (TypeError, ValueError):
        return None


def triggers_exit(position: dict, mark_price: float, ts: int) -> str | None:
    """命中则返回 close_reason，否则 None。

    只看绝对价，不算任何指标 —— 指标在 resolve 那一刻就冻结进价格里了。
    """
    plan = load(position.get("exit_plan"))
    if not plan:
        return None
    qty = float(position.get("qty") or 0)
    if qty == 0:
        return None
    is_long = qty > 0

    stop = plan.get("stop_loss")
    if stop is not None:
        if (is_long and mark_price <= stop) or (not is_long and mark_price >= stop):
            return "stop_loss"

    take = plan.get("take_profit")
    if take is not None:
        if (is_long and mark_price >= take) or (not is_long and mark_price <= take):
            return "take_profit"

    dist = plan.get("trailing_dist")
    peak = position.get("peak_price")
    if dist is not None and peak is not None:
        trail = peak - dist if is_long else peak + dist
        if (is_long and mark_price <= trail) or (not is_long and mark_price >= trail):
            return "trailing_stop"

    secs = plan.get("time_stop_seconds")
    if secs is not None and ts - int(position.get("opened_at") or ts) >= secs:
        return "time_stop"

    return None


def effective_stop(position: dict) -> tuple[float, str] | None:
    """当前真正生效的止损线（初始止损与移动止损取更紧的那条）。

    取更紧的一条：多头取更高者，空头取更低者。
    """
    plan = load(position.get("exit_plan"))
    if not plan:
        return None
    is_long = float(position.get("qty") or 0) > 0
    candidates: list[tuple[float, str]] = []
    if plan.get("stop_loss") is not None:
        candidates.append((float(plan["stop_loss"]), "stop_loss"))
    dist, peak = plan.get("trailing_dist"), position.get("peak_price")
    if dist is not None and peak is not None:
        candidates.append((peak - dist if is_long else peak + dist, "trailing_stop"))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0]) if is_long else min(candidates, key=lambda c: c[0])


# ============================================================
# 修改：只能收紧（§9.4 防作弊 1）
# ============================================================


def amend(plan: dict, patch_spec: dict, side: int, entry_price: float,
          atr: float | None = None, bar_seconds: int | None = None) -> dict:
    """按 patch_spec 重算计划，但**只接受收紧**。放宽一律 PlanError。

    专门防 LLM 浮亏时"再等等"—— 这是它最危险的本能之一，
    必须在协议层禁掉，而不是靠提示词劝。

    patch 是**增量**的：没提到的条款沿用原计划，不会被顺手抹掉。
    原因是"只想挪一下止损"是最常见的诉求，若要求它每次把整份计划重写一遍，
    它迟早会漏抄一条，而漏抄的后果是**悄悄撤销一条保护**。
    想显式撤销某条（例如 `{"time_stop": null}`）也会被下面拦住。
    """
    merged = {**(plan.get("spec") or {}), **patch_spec}
    new = resolve(merged, side, entry_price, atr, bar_seconds)

    # 一条都没动就退回去：amend 的语义是"收紧"，不是"改写"。
    # 否则空 patch 会刷出一次"已收紧"的假象，面板上看着像做过风控。
    if all(new.get(k) == plan.get(k) for k in
           ("stop_loss", "take_profit", "trailing_dist", "time_stop_seconds")):
        raise PlanError("这次 patch 没有收紧任何一条，不必修改")

    old_stop, new_stop = plan.get("stop_loss"), new.get("stop_loss")
    if old_stop is not None:
        if new_stop is None:
            raise PlanError("不能撤销已有的止损")
        tighter = new_stop >= old_stop if side > 0 else new_stop <= old_stop
        if not tighter:
            raise PlanError(f"止损只能朝有利方向移动：{old_stop:.6g} → {new_stop:.6g} 是放宽")

    old_dist, new_dist = plan.get("trailing_dist"), new.get("trailing_dist")
    if old_dist is not None:
        if new_dist is None:
            raise PlanError("不能撤销已有的移动止损")
        if new_dist > old_dist:
            raise PlanError("移动止损的距离只能缩小，不能放大")

    old_secs, new_secs = plan.get("time_stop_seconds"), new.get("time_stop_seconds")
    if old_secs is not None:
        if new_secs is None:
            raise PlanError("不能撤销已有的超时退出")
        if new_secs > old_secs:
            raise PlanError("超时退出只能提前，不能延后")

    return new
