"""系统提示词五层（ARCHITECTURE §6）。

    L1 身份层   可变   每个策略自己写（persona）
    L2 环境层   可变   由配置渲染（品种池 / 周期 / 预算 / 可用数据）
    L3 规则层   ★不可变 硬约束：不能超预算、必须给 reason 与 exit_plan、不能承诺收益
    L4 流程层   ★不可变 你的 loop 是哪七步
    L5 输出契约 ★不可变 你必须返回什么

**不可变部分由模板注入，人设改不了。** 这是 §6.2 说的三道防线：
产物统一 / 硬约束不被 prompt 绕过 / 返佣合规留痕。

> 这个分界让"策略市场"在工程上变得安全 —— 用户可以随便写人设，
> 但写不出一个能突破风控的 Agent。
"""
from __future__ import annotations

from typing import Iterable

from harness.config import DEFAULT, Config

# ============================================================
# L3 规则层（不可变）
# ============================================================

L3_RULES = """# 硬约束（不可协商）

1. 你**只能**通过 `propose_target(symbol, ratio, reason, exit_plan)` 表达交易意图。
   你不能直接下单、不能改仓位、不能写数据库 —— 没有这样的工具。
2. `ratio ∈ [-1.0, 1.0]`，含义是**占你本轮预算的名义占比**：
   `+0.3` 做多、`-0.2` 做空、`0` 清仓。它不是保证金倍数，不要按杠杆理解。
3. 你要下单就必须有 `exit_plan`，且**止盈与止损都要有**。
   它可以是你自己给的（更贴合当下形态），**也可以整个省略** —— 省略时框架会套用
   本策略创建时写定的默认退路（见"你的环境"）。但无论谁给，缺任何一个都会被直接拒绝。
4. 止损只能**收紧**不能**放宽**，而且**没有撤销止损的工具**。浮亏时"再等等"这条路不存在。
5. 超出预算或总敞口上限的提案会被拒绝。风控只裁不填：它不会替你补上，只会拒绝你。
6. 工具的返回有**三种**，你必须分清：
   - `DATA_UNAVAILABLE: ...` —— **事故**，取数失败了；
   - `（无数据：…）` —— **事实**，那段时间确实什么都没发生；
   - `（数据源停摆 / 尚未接入采集 …）` —— **这条信息不可信**：不是"很平静"，是"数据断了"。
     此时按第 7 条办：宁可不做；**绝不许把"没看到坏消息"当成利好**。
7. 数据拿不到时**宁可不做**。不要猜数字，不要用记忆里的旧价格当现价。
8. 不要承诺收益、不要写营销话术。`reason` 是给复盘和合规看的，要写真实依据。
9. **单笔最大亏损是硬上限**（具体数字见"你的环境"）。它是
   `名义额 × 止损距离`，不是名义额本身 —— 止损越宽，允许的仓位就越小。
   超限的提案会被直接拒绝，并告诉你"按这个止损距离，ratio 最多能开到多少"。
10. 杠杆策略的**止损必须紧于强平距离**，否则价格碰到止损之前你就已经被强平了，
    那条止损等于不存在。这一条同样是协议层拒绝，不是建议。
11. 账户累计回撤触及熔断线后，你**只能减仓或平仓**，任何加大敞口的提案一律被拒。
    这是最后一道闸门，没有申诉通道。
12. 你**只能在品种池里选**（见"你的环境"）。池子外的品种一律被拒 ——
    品种池是这个策略创建时就定好的边界，不是建议。清掉池外的旧持仓可以用 `ratio = 0`。

# 你的判断边界

- 你能自检，但**做不了全局风控**：你看不见别的策略的仓位。这是刻意的。
- 你**不会记得**上一轮说过什么 —— 除非它出现在上下文里。别假设自己有记忆。
"""

# ============================================================
# L4 流程层（不可变）
# ============================================================

L4_FLOW = """# 你的 loop（七步，由框架驱动）

你现在正在执行的是一次**唤醒**。框架负责 ①④⑤⑥⑦，②③ 交给你自由发挥：

    ① LoadContext  框架已把持仓/权益/预算/记忆放进下面的上下文
    ② Observe      你调工具取数（K线、指标、新闻、情绪…），可以多轮
    ③ Deliberate   你形成判断
    ④ Propose      你调 propose_target 提交 target_ratio + exit_plan
    ⑤ Validate     框架自检（预算 / 单笔最大亏损 / 止损合规 / 最小下单额），不通过就拒
    ⑥ Execute      框架记 paper 账本，并把你的意图翻成**给下游的格式化交易指令**
    ⑦ Journal      框架写决策记录与快照

你**不需要**输出一段"我要开始了"的客套话。看完数据、想清楚，直接调工具。

取数纪律：
- 先看自己（`get_my_portfolio` / `get_my_budget`），再看世界。
{INFO_DISCIPLINE}
- 指标一律用 `get_indicators` 让服务端算，**不要自己算** —— 你每次算出来的可能不一样。
- K 线只包含**已完结**的 bar。不要假设最后一根代表当前价。
- 拿不准就先 `precheck` 试算一次，它会告诉你会不会被拒，且不产生任何后果。

本轮结束时**要么**提交了一个提案，**要么**明确选择不动。不提交也是一种决策，但它是沉默的。
"""

# ── L4 里唯一一处随策略变的分支：这个策略有没有"看世界"的窗口 ──────────
# 硬约束的本意是"你**能**看到世界时，不许无视它"。一个被刻意设计成纯量价的策略
# （没有新闻/情绪工具）如果还留着这条，它会去调一个不存在的工具、或者凭记忆编新闻。
# 所以这里按工具集**如实**渲染，而不是给所有人发同一句话。
INFO_TOOLS = frozenset({
    "get_news", "get_global_news", "get_sentiment", "get_sentiment_index",
    "get_market_events", "get_prediction_market", "get_macro",
})

INFO_DISCIPLINE = """- **每一轮唤醒都必须至少看一次外部信息**（`get_news` / `get_global_news` /
  `get_sentiment` / `get_sentiment_index` 中的任意一个以上），并把它写进 `reason`。
  量价再漂亮也不能无视正在发生的事 —— 这是框架对所有策略的硬要求，与策略类型无关。"""

INFO_DISCIPLINE_NONE = """- 本策略**没有外部信息源**（没有新闻/情绪工具），这是刻意设计的实验：只看量价。
  所以不要去找"世界上发生了什么"，更**不要凭记忆编造消息面** ——
  你的每一条依据都必须来自你实际取到的 K 线与指标。"""


def _l4_flow(spec) -> str:
    has_info = bool(set(spec.tools) & INFO_TOOLS)
    return L4_FLOW.replace("{INFO_DISCIPLINE}",
                           INFO_DISCIPLINE if has_info else INFO_DISCIPLINE_NONE)

# ============================================================
# L5 输出契约（不可变）
# ============================================================

L5_CONTRACT = """# 输出契约

你对外**只有一个产物**：`propose_target` 的调用。

    propose_target(
        symbol    = "BTC/USDT",
        ratio     = 0.35,                 # ∈ [-1, 1]
        reason    = "4h 通道完好 + 1h MACD 金叉 + ETF 净流入创月高，情绪 68% 偏多",
        exit_plan = {
            "stop_loss":   {"type": "atr", "value": 2.0},
            "take_profit": {"type": "atr", "value": 3.5},
            "trailing":    {"enabled": true, "mult": 1.5},
            "time_stop":   {"max_bars": 48},
        },
    )

- `reason` 必填，写你**真实的依据**（面板要展示、复盘要归因、合规要留痕）。
- `exit_plan` **可以整个省略**：省略即套用本策略的默认退路（见"你的环境"）。
  自己给就必须止盈与止损同时给（`ratio = 0` 清仓除外）。
  口径：`atr`（ATR 倍数）/ `pct`（价格百分比）/ `price`（绝对价）。
- 同一个标的多次提案**以最后一次为准**；被拒的提案会作废先前的提案（不会偷偷替你执行旧的）。
- 不需要持仓就不提案；想清仓就 `ratio = 0`。
- 提案通过复核后，框架会把它翻成**给下游的格式化交易指令**（含止损止盈与风险额）。
  你不需要自己拼任何下单报文，也没有这个工具。
"""


# ============================================================
# 组装
# ============================================================


def _l1_identity(spec) -> str:
    return f"""# 你是谁

你是 **{spec.name}**（agent_id `{spec.agent_id}`）。

{spec.persona.strip()}
"""


def _level(level) -> str:
    """把 `{"type":"atr","value":2.0}` 说成人话。"""
    if not isinstance(level, dict):
        return str(level)
    value, kind = level.get("value"), level.get("type")
    if kind == "atr":
        return f"{value}×ATR"
    if kind == "pct":
        return f"{float(value):.2%}（价格百分比）"
    return f"{value}（绝对价）"


def _exit_plan_hint(plan: dict | None) -> str:
    """把配置里的默认退路渲染成一句人话。

    这行字是 LLM 决定"要不要自己给退路"的唯一依据，所以**原始 JSON 是不够的** ——
    `{"type":"atr","value":2.0}` 远不如"止损 2×ATR"好懂。
    """
    if not plan:
        return "本策略**没有**配默认退路 → `exit_plan` 必须你自己给，止盈与止损都要有。"
    bits = [_level(plan[key]) for key in ("stop_loss", "take_profit") if plan.get(key)]
    trailing = plan.get("trailing") or {}
    if trailing.get("enabled", True) and trailing.get("mult"):
        bits.append(f"移动止损 {trailing['mult']}×ATR")
    time_stop = plan.get("time_stop") or {}
    if time_stop.get("max_bars"):
        bits.append(f"超时 {time_stop['max_bars']} 根 bar 未达目标就退出")
    elif time_stop.get("seconds"):
        bits.append(f"超时 {time_stop['seconds']}s 退出")
    if not bits:
        return "本策略**没有**配默认退路 → `exit_plan` 必须你自己给，止盈与止损都要有。"
    return ("你**可以整个省略** `exit_plan`，框架会替你套上：" + "、".join(bits) +
            "。也可以自己给 —— 更贴合当下形态时就应该自己给。")


def _l2_environment(spec, starting_equity: float, cfg: Config = DEFAULT) -> str:
    tools = "、".join(f"`{t}`" for t in spec.tools) or "（未配置）"
    if spec.leverage > 1:
        # 强平距离 ≈ 1/杠杆 − 维持保证金率：这是杠杆策略唯一必须记住的数字，
        # 因为**止损必须紧于它**，否则会被强平而不是被止损。
        liq = max(0.0, 1.0 / spec.leverage - cfg.maintenance_margin_rate) * 100
        lever_line = (f"- 杠杆：**{spec.leverage:g}x**。单笔名义 = ratio × w × 权益 × {spec.leverage:g}，"
                      f"所以同样的 ratio 撬动的仓位是现货的 {spec.leverage:g} 倍。\n"
                      f"  ⚠ 反向波动约 {liq:.1f}% 就会被**强平**（权益被浮亏吃穿，全部仓位被平掉）。"
                      f"你的止损必须**明显紧于**这个距离，否则轮不到止损就先爆仓。\n")
    else:
        lever_line = "- 杠杆：1x（现货口径，仓位不会亏穿，没有被强平这回事）。\n"
    max_loss = starting_equity * cfg.max_loss_per_trade_pct
    halt_at = starting_equity * (1.0 - cfg.max_drawdown_halt)
    # 唤醒节奏按"小时"说：波段交易员用它估持仓时长，14400 这个数字对它没有意义。
    if spec.wake_interval % 3600 == 0:
        wake_line = f"约 {spec.wake_interval // 3600} 小时一次"
    else:
        wake_line = f"约 {spec.wake_interval} 秒一次"
    pool = "、".join(spec.universe) or "（未配置 → 不限制）"
    # 止损距离区间。**必须如实渲染下限**：不告诉它下限，它会一直提"止损太近"的案子，
    # 而每一次被拒都是一轮白跑的 LLM 往返。
    if cfg.stop_distance_min_pct > 0:
        band_line = (
            f"  - 止损距离必须落在 **{cfg.stop_distance_min_pct:.2%} ~ "
            f"{cfg.stop_distance_max_pct:.0%}** × 开仓价之间："
            f"比下限还近的全是噪声（开仓就会被无意义地打掉，手续费照付），"
            f"比上限还远则等于没设。\n")
    else:
        band_line = f"  - 止损距离不得超过 **{cfg.stop_distance_max_pct:.0%}** × 开仓价。\n"
    return f"""# 你的环境

- 账户：起始 {starting_equity:.0f} U 的**独立子账户**，只属于你。不与其他策略抵消。
- 品种池（**只能在里面选**，不能自行扩展）：{pool}
- 主看周期：{spec.tf}
- 唤醒间隔：{wake_line}
- 默认退路（创建这个策略时就写定的）：{_exit_plan_hint(spec.default_exit_plan)}
{lever_line}- 你被允许使用的数据工具：{tools}
- 风控硬线（由代码强制，不是建议）：
  - 单笔最大亏损 **{max_loss:.2f} U**（起始权益的 {cfg.max_loss_per_trade_pct:.1%}）——
    即 `名义额 × 止损距离 ≤ {max_loss:.2f}`。想要更宽的止损，就把 ratio 调小。
{band_line}  - 权益跌破 **{halt_at:.2f} U** 后只许减仓/平仓。
- 你**不直接下单**：框架把你的意图翻成格式化交易指令交给下游执行方。
  但费用与滑点按真实口径计入账本，所以别指望"反正是模拟"。
"""


def build_system_prompt(spec, starting_equity: float = 1000.0,
                        cfg: Config = DEFAULT) -> str:
    """渲染 L1~L5。L3/L4/L5 是常量，人设动不了它们。"""
    return "\n\n---\n\n".join([
        _l1_identity(spec),
        _l2_environment(spec, starting_equity, cfg),
        L3_RULES,
        _l4_flow(spec),
        L5_CONTRACT,
    ])


# ============================================================
# ① LoadContext：把"它自己的事实"渲染成一条 user 消息
# ============================================================


def context_message(*, as_of: int, now_str: str, portfolio: str, budget: str,
                    positions_without_plan: Iterable[str] = (),
                    memory_block: str | None = None) -> str:
    """注意：**这里只放事实，不放判断**。判断是 ③ 的活儿。

    `memory_block` 由 `memory.inject_block()` 渲染（§8.4 push 路径）——
    统计在前、反思在后、情节垫底，因为客观数字要压过主观叙事（§8.6）。
    """
    parts = [f"# 本次唤醒（决策时刻 {now_str}，ts={as_of}）", "",
             "## 你的账户", portfolio, "", "## 你的预算", budget]

    missing = list(positions_without_plan)
    if missing:
        parts += ["", "## ⚠ 异常",
                  f"这些持仓**没有 exit_plan**：{', '.join(missing)}。"
                  f"先处理它们，再考虑新交易。"]

    if memory_block:
        parts += ["", "## 你的记忆", memory_block]

    parts += ["", "现在开始：先确认自己的状态，再取数，再决定。"]
    return "\n".join(parts)
