# CCB Trade —— 架构说明

> **一句话**：一个**定时唤醒**、**自己看市场**、**自己决定仓位与止盈止损**的 AI 交易 Agent。
> 代码层面有三档硬风控兜底，它**不能亏太多**；它**不实际下单**，只产出格式化的交易指令。

---

## 0. 全景

```
                    ┌────────────────────────────────────┐
                    │ 进程 A：info-feeds（独立常驻）        │
                    │ 采集新闻/社媒/公告/预测市场/宏观/情绪  │
                    └─────────────────┬──────────────────┘
                                      │ 增量写入（幂等 upsert）
                                      ▼
                    ┌────────────────────────────────────┐
                    │ 共享 DB（SQLite，两个进程用同一个文件）│
                    │ 信息表 · 快照表 · 账本表 · 认知表 · 指令表 │
                    └─────────────────┬──────────────────┘
                                      │ 读
                                      ▼
                    ┌────────────────────────────────────┐
                    │ 进程 B：harness（调度器 + N 个 Agent）│
                    │ 七步决策 → 格式化交易指令             │
                    └─────────────────┬──────────────────┘
                                      │ 按需拉（K 线不落库）
                                      ▼
                      Bitget USDT-FUTURES 永续 candles v3
                                      │
                                      ▼
                          trade_signals（交付给下游执行方）
```

两个进程**只通过 DB 耦合**，各自可以独立重启。默认库路径都是
`ccb-sub-agents/ccb_subagents.db`，可用 `CCB_DB_PATH` 覆盖 —— **两边必须指向同一个文件**。

**Agent 自己不持有任何状态**：每次唤醒从 DB 读，用完丢弃。所以进程随时可重启，
复盘时也能把"当时的输入"和"当时的决策"对上。

---

## 1. 数据层：info-feeds（L0）

**它是什么**：一个**取数 + 格式化的库**。把行情、技术指标、财报、新闻、宏观、预测市场
统一成一套函数接口，调用方不需要知道数据来自哪个网站。
返回的是 **CSV / Markdown 形式的字符串**，设计目标就是能直接塞进提示词 ——
所以它服务的对象是 **AI Agent 的工具调用**。

**它不是**：不是交易策略，不是数据库（取数仍是实时拉的），不是 ORM。

### 1.1 三层结构

```
调用方          route_to_vendor("get_news", "BTC-USD", ...)
                        │
                        ▼
路由层          router.py —— 方法 → 类别 → 厂商链，失败自动换下一个
                        │        （全都失败就返回一句说明文字，不抛异常）
                        ▼
厂商实现层      vendors/ —— 一个数据源一个模块
                        │
                        ▼
外部 API        Yahoo / Alpha Vantage / SEC EDGAR / FRED / Polymarket /
                Reddit / StockTwits / Google News RSS /
                cryptocurrency.cv / Alternative.me / Telegram
```

**横切关注点**（所有厂商共用）：

| 模块 | 干什么 |
|---|---|
| `config.py` + `default_config.py` | 配置（三层优先级 + 环境变量覆盖） |
| `symbols.py` | 符号归一化（`XAUUSD+` → `GC=F`）+ 路径安全校验 |
| `date_window.py` | **防前视**：回测不许看到未来数据 ★ 最有价值的部分 |
| `errors.py` | 错误分类 —— 让路由按"行为"而不是按"厂商"处理失败 |
| `net.py` | HTTP 请求 + API key 脱敏 |

### 1.2 数据源清单

| 类别 | 方法 | 厂商 | 要 key |
|---|---|---|---|
| 行情 | `get_stock_data` | yfinance / alpha_vantage | 否 / 是 |
| 技术指标 | `get_indicators` | yfinance / alpha_vantage | 否 / 是 |
| 公司概况 | `get_fundamentals` | yfinance / alpha_vantage | 否 / 是 |
| 三张财报 | `get_balance_sheet` `get_cashflow` `get_income_statement` | yfinance / **sec_edgar** / alpha_vantage | 否 / **否** / 是 |
| 个股新闻 | `get_news` | **google_news** / alpha_vantage / yfinance | **否** / 是 / 否 |
| 全球新闻 | `get_global_news` | **cryptocurrency_cv** / **google_news** / yfinance / alpha_vantage | **否** / **否** / 否 / 是 |
| 内部人交易 | `get_insider_transactions` | yfinance / alpha_vantage | 否 / 是 |
| 宏观指标 | `get_macro_indicators` | **fred** | **是** |
| 预测市场 | `get_prediction_markets` | **polymarket** | **否** |
| 情绪指数 | `get_sentiment_index` | **alternative_me**（Fear & Greed） | **否** |
| 社媒情绪 | `get_sentiment` | **reddit** / **stocktwits** | **否** / **否** |

两个不经路由的模块：

- `vendors/telegram_preview.py` —— 项目方 / 交易所官方频道的一手消息（上币、维护）。
  入参是**频道名列表**而不是 ticker，所以没有可挂的方法名，需直接调用。
- `vendors/yahoo/snapshot.py` —— **确定性核验快照**（最新 OHLCV + 一组固定指标）。
  给 LLM 一个"精确数字的唯一真相来源"，防止它编造布林带数值。

环境变量：`ALPHA_VANTAGE_API_KEY`、`FRED_API_KEY`、`SEC_EDGAR_USER_AGENT`（可选）。

### 1.3 常驻采集进程 `collector/`

取数是实时的，但**"错过就没了"的东西必须落库** —— 这就是 collector 存在的理由。

三条硬要求：

| 要求 | 做法 |
|---|---|
| **幂等** | 全部走 `INSERT … ON CONFLICT DO UPDATE`，重启 / 重试不产生重复行 |
| **水位线持久化** | `collector_state(source, last_watermark)` 存 DB 不存内存，只增不减 |
| **单源故障不传染** | 逐源捕获异常、单独记 `source_health`；一个源挂了不影响别的源 |

源与节奏（第一轮）：

| 源 | 节奏 | 写进 |
|---|---|---|
| `cryptocurrency_cv` 加密快讯 | 180s | `news_items` |
| `google_news` 逐标的新闻 | 300s | `news_items` |
| `google_news_global` 宏观新闻 | 300s | `news_items` |
| `reddit` 讨论 | 600s | `social_items` |
| `stocktwits` 情绪 | 900s | `social_items` |
| `polymarket` 预测市场赔率 | 300s | `prediction_quote` |
| `fear_greed` 恐惧贪婪指数 | 3600s | `sentiment_index` |
| `macro` 宏观序列（FRED） | 43200s | `macro_series` |

跑法见 §6。

---

## 2. 决策层：harness

### 2.1 两个 Loop

| Loop | 频率 | 碰 LLM | 干什么 |
|---|---|---|---|
| **1 scheduler** | 10s | ✅ | 把到期的 Agent 拉起来跑一次七步 |
| **2 watchdog** | 60s | ❌ | 扫止损 / 止盈 / 移动止损 / 超时 / 强平 |

**顺序有讲究**：先 Loop 2 再 Loop 1 —— 先平仓再唤醒，LLM 看到的才是"平完之后的真实持仓"。
两者在 `live.py` 里**单线程交错**跑（共用一个 SQLite 连接，sqlite3 连接不是线程安全的；
都是秒级/分钟级低频活儿，交错跑没性能问题，却省掉一整类并发 bug）。

**Loop 2 存在的全部意义**：止损不依赖"LLM 恰好醒着"。
正因如此，**LLM 挂掉 = 什么都不做，而这是安全的** —— 安全靠止损线，不靠醒得勤。
`exit_plan` 在开仓那一刻就被解析成**绝对价**，watchdog 只做纯价格比较，不碰指标、不碰 K 线、不碰 LLM。

### 2.2 七步 loop

```
① LoadContext   读自己：仓位 / 权益 / 本次预算 / 记忆              [代码]
② Observe       调工具取数                                    [LLM·ReAct]
③ Deliberate    形成判断                                      [LLM·ReAct]
④ Propose       调 propose_target 提交 ratio + exit_plan       [LLM·结构化]
⑤ Validate      预算 / 总敞口 / 单笔亏损 / 止损合规 / 最小下单额    [代码]
⑥ Execute       记 paper 账本 + **产出格式化交易指令**            [代码]
⑦ Journal       写决策 + 落快照 + 派生记忆                         [代码]
```

**只有 ②③ 放开 LLM，其余五步全由代码控制。** 纯 ReAct 会漏步骤、会忘记输出；
固定管线又退化成代码策略。**骨架 + 自由段**两头都要。

关于降级：**LLM 挂掉 → 直接放弃本 tick**，即使它在失败前已提交过提案也不执行。
`"宁可不做，不可做错。"` 每种结局都有名字
（`traded / amended / no_action / rejected / degraded`），绝不静默。

### 2.3 唯一的出口：`propose_target`

**整个系统里 LLM 能触发的"写"只有一处**：`propose_target`。而它也不直接写库 ——
它把意图塞进 `ctx.proposal`，由 loop 在 ⑤⑥ 统一裁决与执行。
所以"绕过校验直接下单"在结构上就不存在。

刻意**没有** `cancel_exit_plan`：止损只能收紧、不能删。
不是"没实现"，是**这个能力就不该存在** —— 少一个工具就少一条绕过风控的路径。

④ 的提案在 ⑥ 执行前会用**执行时刻的价再裁决一次**（一个 tick 内的行情漂移也算漂移）。

### 2.4 工具（A/B/C/D 四类）

| 类 | 模块 | 工具 |
|---|---|---|
| A 数据 | `tools/data.py` | `get_candles` `get_indicators` `get_derivatives` `get_news` `get_global_news` `get_sentiment` `get_sentiment_index` `get_market_events` `get_prediction_market` `get_macro` |
| B 账户 | `tools/account.py` | `get_my_portfolio` `get_my_budget` `get_my_recent_decisions` |
| C 记忆 | `tools/memory.py` | `recall` |
| D 决策 | `tools/decision.py` | `propose_target` `get_exit_plan` `amend_exit_plan` `precheck` |

A 类的行情分两条来源：

- **`get_candles` / `get_indicators`** —— K 线与指标，经 `ctx.candles` 注入。
  实盘走 `BitgetCandleSource`：**USDT-FUTURES 永续** v3 candles
  （`/api/v3/market/candles`，`category=USDT-FUTURES`）。用永续而不是现货，
  是为了跟 Agent 实际交易的口径对齐 —— 现货与合约有基差，止损/强平都按合约价算。
  三条实测踩过的坑都写在 `candles.py` 里：`interval` 的 **H/D 必须大写**（`1h` 直接 400）、
  单次上限 **1000 根**（1500 → 400）、v3 **会返回正在走的那根 bar** 所以防前视过滤不是可选项。
- **`get_derivatives`** —— OI / 资金费率 / 标记价 / 指数价 / 基差，直接打 Bitget（`derivatives.py`）。
  只做永续：这两个指标只在合约上存在。**它是杠杆策略的必看项** ——
  费率是持仓成本也是多空拥挤度的直接读数，OI 是这个方向上有多少钱在下注。
  两个实测结论决定了它的形状：
  - **资金费率有历史序列**（`/api/v2/mix/market/history-fund-rate`，8 小时一期，实测可回溯约 33 天），
    接口给倒序，要排回升序；结算时间晚于 `as_of` 的期数一律剔除（与 K 线同一条防前视纪律）。
  - **OI 只有当前值，没有历史序列。** Bitget 没有 OI 历史接口；而
    `/api/v3/market/candles?type=open_interest` 是个**陷阱** —— 它不报错，
    而是**静默回落成普通成交价 K 线**（实测返回值与 `type=normal` 逐字相同，
    而真实 OI 是前者量级的 1/3）。所以代码**绝不请求那个 type**，
    并且在返回文本里**明说"没有序列"** —— 让 LLM 知道自己看不到趋势，
    而不是让它把一条假序列当趋势。真要看 OI 的变化，靠每轮把现值落进
    `market_snapshot`，序列由快照表自然积累。

三条**框架级**约定（不靠 LLM 自觉）：

1. **返回必须有长度上限** —— 服务端强制截断，签名里的 `limit` 只是"上限的上限"。
2. **失败返回哨兵，绝不返回空** —— `DATA_UNAVAILABLE: ...`。返回空会让 LLM **编造数据**。
   信息类工具的返回因此有**三种**，混不得：`DATA_UNAVAILABLE`（事故）、
   `（无数据：…）`（事实，源是活的、那段时间确实没事）、
   `（数据源停摆／尚未接入采集…）`（**这条信息不可信**，不是"平静"是"数据断了"）。
   第二种与第三种混为一谈，LLM 会把采集中断读成"市场很安静"然后放心下单。
3. **行情数值自动落快照** —— ⑦ 一次性落库（K 线可以不存，但"你当时看到了什么"必须存）。

**工具集按策略裁剪**：能裁的只有 A 类。B 类（看自己）、C 类（记忆）、D 类（出口）**永远在** ——
"先看自己"是框架强制的第 ① 步，出口更不可能靠配置裁掉。
配置能决定的只是"它能看到世界的哪几个面"。

**防前视统一在数据源层**（`tools/candles.py`）：一个 bar 只有
`bar.ts + tf秒数 <= as_of` 才算"已完结、可当收盘价用"。这条不写死，任何基于它的判断都是假的。

### 2.5 系统提示词五层

提示词在 `harness/prompts/system.py`，按五层拼装：

| 层 | 内容 | 可变性 |
|---|---|---|
| **L1 身份** | persona，每个策略自己写 | 可变 |
| **L2 环境** | 品种池 / 周期 / 预算 / 杠杆 / **默认退路 / 止损距离区间 / 风控硬线的具体数字** | 可变（由配置渲染） |
| **L3 规则** | 不可协商的硬约束（12 条） | ★不可变 |
| **L4 流程** | 你的 loop 是哪七步、取数纪律 | ★不可变 |
| **L5 输出契约** | 必须返回什么（`propose_target` 的形态） | ★不可变 |

L3 的 12 条硬约束（摘）：只能通过 `propose_target` 表达意图；`ratio ∈ [-1,1]` 是**预算占比**
不是杠杆倍数；下单必须有 `exit_plan`（**可省略** -> 框架套用策略的**默认退路**），
且**止盈止损都要有**；止损只能收紧不能放宽；**只能在品种池里选**（清掉池外旧持仓用 `ratio=0`）；
超预算会被拒；工具失败返 `DATA_UNAVAILABLE`；拿不到数据宁可不做；
**单笔最大亏损是硬上限**（`名义 × 止损距离`）；杠杆策略的止损必须紧于强平距离；
账户累计回撤触及熔断线后只能减仓。

**不可变部分由模板注入，人设改不了。** 所以一个策略的 `persona` 可以随便写，
但**写不出一个能突破风控的 Agent** —— 硬约束的落地靠代码（⑤ Validate），
提示词只是让它**提前知道**会撞哪面墙。

L4 里有一处随策略变的分支：**有信息类工具的策略，被强制"每轮至少看一次外部信息"**；
被刻意设计成纯量价的策略则明确告诉它"没有外部信息源，不要编消息面"。
按工具集**如实渲染**，而不是给所有人发同一句话。

① 的上下文由 `context_message()` 渲染成一条 user 消息：
账户 → 预算 → 异常持仓（没有 exit_plan 的）→ 记忆。**只放事实，不放判断** —— 判断是 ③ 的活儿。

### 2.6 Paper 账本

```
equity = cash + Σ(qty × mark_price)        # qty 带符号，空头为负，一样成立
```

**这套口径天然是保证金口径**：买入 5 倍名义的仓位时现金会变成负数（＝借来的钱），
而 `equity` 依旧等于"自有资金 + 浮动盈亏"，所以价格反向走 1% 就亏掉 5% 的权益 ——
这正是 5 倍杠杆该有的样子，**不需要为杠杆另开一个账户**。

- **撮合防呆**：`ledger.apply_order` **没有**"用决策时刻的价格成交"这个选项 ——
  调用方必须传入决策**之后**的参考价，成交价由 `fees` 在它基础上加减滑点。
  这是靠 **API 形状**拦住，不是靠注释提醒。
- **费用与滑点分开记**：这是"策略赚的是价差还是被手续费吃了"的唯一依据。
- **减仓 / 平仓永远允许**：`min_notional` 只约束"敞口变大"的下单，
  否则一个跌到 5U 的仓位会因"不够最小下单额"而永远平不掉 —— 那正好把保护性退出堵死了。
- 各子策略独立记账、**不做多空抵消**。

---

## 3. 风控兜底

**"不能让他亏太多"是代码，不是提示词。** 从紧到松三档：

| # | 闸门 | 判据 | 落点 |
|---|---|---|---|
| 1 | **单笔最大亏损** | `目标仓位 × 止损距离 > 起始权益 × 2%` → 拒 | `decision.evaluate` |
| 2 | **止损必须紧于强平线** | 杠杆 > 1 时 `止损距离 ≥ 开仓价 × (1/杠杆 − 维持保证金率)` → 拒 | `exit_plan.validate` |
| 3 | **累计回撤熔断** | 权益 < 起始权益 × 70% → 只许减仓 / 平仓 | `decision.evaluate` |

外加三条结构性约束：

- **每一笔都要有止盈止损**：`exit_plan` 的 `required` 就是 `["stop_loss", "take_profit"]`，
  缺任何一个直接拒（`ratio = 0` 清仓除外）。
- **止损只能收紧**：`amend` 只接受朝有利方向的移动，放宽一律拒绝 ——
  防的是 LLM 浮亏时"再等等"那个本能。
- **止损后冷却期**（默认 1800s）：刚被打掉不许立刻加大敞口，防报复性交易。
  冷却期与熔断都**只挡"加大敞口"，减仓 / 平仓永远放行**。

被拒时**必须说清为什么**，原因回灌给 LLM 让它改正一次。
比如触到单笔上限时，会明确告诉它"按当前止损距离，ratio 最多能开到多少"——
否则它只会反复撞墙。

---

## 4. 指令出口：`trade_signals`

**这是整套框架对外的唯一交付物。** 一次通过 ⑤ 复核的决策被翻成一行指令：

| 字段 | 含义 |
|---|---|
| `signal_id` | 由 `decision_id` 派生 —— 同一次决策任何时候产出同一个 id |
| `action` | `open` / `increase` / `reduce` / `close` / `reverse`（给人看的标签） |
| `symbol` `side` | 标的；`long` / `short` / `flat` |
| `qty` | **带符号的目标持仓量**（不是本次买卖量） |
| `entry_price` | 参考价，下游按自己的盘口成交 |
| `stop_loss` `take_profit` `trailing_dist` `time_stop_sec` | 退路，绝对价 |
| `leverage` `risk_amount` | 杠杆；**触发止损时的亏损额**（下游可据此做二次风控） |
| `reason` `evidence` | 决策理由；这一刻它看到了什么 |
| `status` | `emitted` / `superseded` |

**指令与成交是两件事**：`trade_signals` 是给下游的契约，`agent_fills` 是内部 paper 账本。
刻意分开，因为两者回答的问题不同 —— 账本回答"如果真按它说的做，现在会怎样"，
指令回答"我想让下游做什么"。下游有自己的盘口、最小下单单位和资金规模，
所以**指令里的 `qty` 是目标持仓量，从"当前仓位"到"目标仓位"之间的路怎么走由执行方决定**。

**只增不改**：新指令来了，同标的上一条未覆盖的标成 `superseded`，不删 ——
下游据此判断哪条还有效，复盘时也看得见"我们先说了什么、后来改成了什么"。
**目标是现状一致时不发指令**（那是噪音，不是信号）。

---

## 5. 记忆

三条写入路径，**没有一条是"LLM 自由写"**：

| 路径 | 谁生成 | 触发 | kind |
|---|---|---|---|
| A 情节 | 纯代码派生 | 每次决策后自动 | `episodic` |
| B 反思 | LLM 生成，代码决定**何时写、什么格式** | 代码设定的触发点 | `reflection` |
| C 统计 | 纯代码聚合 | 每次决策后 | `agent_stats` 表 |

读取两条路：**注入**（harness 主动 push，保证基本盘）+ **检索**（`recall`，给主动性）。
注入顺序固定为**统计在前、反思在后、情节垫底** —— 客观数字要压过主观叙事。

**为什么对 LLM 这么不信任**：LLM 写自己的记忆 = 给自己下达未来的行为指令。
如果它能写"下次可以放宽止损"，那条"止损只能收紧"下一轮就被绕过了。
所以唯一放开的"反思"要过闸门（最短长度、频率、格式），**参数建议只入库、不生效**。

---

## 6. 怎么跑

### 6.1 决策层（harness）

```bash
cd ccb-sub-agents

python -m harness                       # 一直跑到 Ctrl-C
python -m harness --ticks 6             # 跑 6 个 tick 就停（预检）
python -m harness --agent trend-scout-01
python -m harness --no-llm              # 不接 LLM：只验证止损扫描与账本
```

LLM 走 OpenAI 兼容协议，默认 provider 是 `deepseek`（key 从环境变量读，见 `.env`）。

配置一个策略 = 写一份 `agents/*.json`，不是写代码：

```json
{
  "agent_id": "trend-scout-01",
  "name": "阿岚 · 1h 波段侦察",
  "persona": "你是阿岚……（人设随便写；风险偏好也写在这里）",
  "universe": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AAPL/USDT", "NVDA/USDT",
               "SPX/USDT", "NDX100/USDT", "XAU/USDT"],
  "tf": "1h",
  "wake_interval": 14400,
  "w": 0.5, "gross_cap": 3.0, "leverage": 2,
  "starting_equity": 1000.0,

  "max_loss_per_trade_pct": 0.02,
  "max_drawdown_halt": 0.30,
  "stop_distance_min_pct": 0.004,
  "stop_distance_max_pct": 0.5,
  "cooldown_after_stop": 14400,
  "default_exit_plan": {
    "stop_loss":   {"type": "atr", "value": 2.0},
    "take_profit": {"type": "atr", "value": 4.0}
  },
  "tools": ["get_candles", "get_indicators", "get_derivatives", "get_news", "..."]
}
```

`persona × universe × tools 子集 × 风控参数` = 一个新策略。

**风险偏好是策略画像的一部分，创建时就写定**（§9.4）。下面这些字段**全部可选、平铺在顶层**，
没写的回落 `Config` 全局默认，所以老配置不用改：

| 字段 | 管什么 |
|---|---|
| `max_loss_per_trade_pct` | 单笔最大亏损（占起始权益），"不能亏太多"的那层兜底 |
| `max_drawdown_halt` | 账户累计回撤熔断线，破了只许减仓 |
| `stop_distance_min_pct` / `stop_distance_max_pct` | 止损距离的允许区间：太近是噪声，太远形同虚设 |
| `cooldown_after_stop` | 止损后同方向的冷却时长，防报复性交易 |
| `default_exit_plan` | 默认退路：LLM 省略 `exit_plan` 时框架替它套上 |

`AgentSpec.risk_cfg()` 把本策略的画像盖到全局配置上，下游（⑤ 校验、止损扫描、watchdog、
提示词渲染）只认这一份 cfg。**同一个人设写"激进"还是"保守"，落在代码里就是这几个数字的差别**
—— 全局一份值套在所有策略上，等于没有策略画像。

`leverage` 仍然是表达风险偏好的主力旋钮之一，而且它和 `w` 是耦合的 ——
两者一起决定"满仓时止损最宽能放到几%"：

```
满仓（ratio=1）允许的最大止损% = max_loss_per_trade_pct × 权益 ÷ (w × 权益 × leverage)
```

按 `w=0.5` / 权益 1000 / 单笔上限 2% 算：`leverage=2` → 止损最宽 2.0%，正好装得下
1h 波段的 2×ATR 止损（实测 BTC 1.1%、SOL 2.0%、XRP 2.5%）；`leverage=10` → 上限被压到
0.4%，那不是波段止损而是噪声止损，每条 1h bar 都能打掉它。
换句话说**杠杆填错，止损宽度就被结构性锁死**，persona 里写"退路要宽"也没用。

同理 `gross_cap` 要跟着杠杆标定：它限制 `Σ|名义| ÷ 权益`，而单笔名义上限是
`w × 权益 × leverage` —— 杠杆降下来后 `gross_cap` 不跟着降，这个组合层闸门就永远不会触发。

**品种池不限加密。** Bitget 的 `USDT-FUTURES` 实测有 804 个合约，其中就包含美股
（`AAPLUSDT` / `NVDAUSDT` / `TSLAUSDT`…）、指数（`SPXUSDT` / `NDX100USDT` / `HSIUSDT`）、
贵金属（`XAUUSDT` / `XAGUSDT`）与外汇（`EURUSDUSDT` / `GBPUSDUSDT`）——
**不需要额外的行情源**，只需要符号映射：`candles.bitget_symbol()` 管交易口径，
`data._info_symbol()` 管信息层（Yahoo）口径。池外的品种在 ⑤ 被硬拦，提示词里写着不算数。

### 6.2 数据层（info-feeds）

```bash
cd info-feeds
python -m info_feeds.collector            # 常驻，按各源节奏循环
python -m info_feeds.collector --once     # 只跑一轮（冒烟用）
```

写库位置默认 `../ccb-sub-agents/ccb_subagents.db`，用 `CCB_DB_PATH` 覆盖。
盯哪些标的 / 主题见 `collector/config.py`
（`CCB_WATCHLIST` / `CCB_PREDICTION_TOPICS` / `CCB_MACRO_SERIES` / `CCB_NEWS_LOOKBACK_DAYS`）。

**采集清单从策略派生，不手工维护。** `WATCHLIST` 不设时取各 `agents/*.json` 里 `universe`
的并集，并翻成信息层口径（`watchlist_from_agents()`，`AAPL/USDT → AAPL`、`SPX/USDT → ^GSPC`）。
理由很直接：手工清单意味着"给 Agent 加了标的，却忘了让采集器去采它"，后果是那次
`get_news` 查回空 —— 而 LLM 不会知道是漏采，只会读成"这个标的最近很安静"。
**加标的只改一处，写入与读取两侧的口径由 `_INFO_ALIASES` 保证一致**
（`data.py` 有一份逐字相同的副本，改一边就要改另一边）。

**新鲜度闸门：把"停摆"和"平静"分开。** 采集侧一直在写 `source_health`，
决策侧现在会读它：某个读工具依赖的源全部超过 `Config.info_stale_seconds`（默认 6h）
没成功过，返回的就不再是"没有新闻"，而是"**数据源停摆**"；表若压根没有采集源
（如 `market_events` 目前还没有），则如实说"**尚未接入采集**"。
`news_items` 查回空可以是世界很安静，也可以是采集进程死了三天 ——
只看表本身这两件事长得一模一样，而 LLM 一定会选那个更舒服的解释。

### 6.3 测试

```bash
cd ccb-sub-agents
python -m pytest tests -q
```

| 文件 | 验收什么 |
|---|---|
| `test_s1_ledger.py` | 撮合、费用、滑点、权益口径 |
| `test_s2_watchdog.py` | 止损/止盈/移动止损被自动执行，**全程不涉及 LLM** |
| `test_s3_tools.py` | 返回上限、失败哨兵、防前视、快照落库、**信息源的停摆/平静之分** |
| `test_s4_loop.py` | 七步 loop 跑通、提案被拒的几种情形、指令出口 |
| `test_s5_memory.py` | 三条记忆路径、反思闸门、防自我强化 |
| `test_s6_risk_signals.py` | 三档风控兜底 + 指令落库/覆盖语义 |

---

## 7. 数据表（`ccb-sub-agents/schema.sql`）

| 层 | 表 | 谁写 |
|---|---|---|
| 信息 | `news_items` `social_items` `market_events` `prediction_quote` `macro_series` `sentiment_index` `source_health` `collector_state` | info-feeds |
| 快照 | `market_snapshot`（决策时刻的行情指纹） | harness |
| 账本 | `agents` `agent_runtime` `agent_budget` `agent_positions` `agent_fills` `equity_curve` | harness |
| 指令 | `trade_signals` | harness |
| 认知 | `agent_decisions` `agent_memory` `agent_stats` | harness |

快照表的存在理由：**K 线体量大、不落库，但"你当时看到了什么"必须落库** ——
没有它，K 线不落库就无法复盘归因。

---

## 8. 目录

```
ccb-trade/
├─ ARCHITECTURE.md            ← 本文
├─ README.md
├─ BITGET-DATA.md             ← Bitget 行情接口说明（数据参考）
├─ ccb-sub-agents/            ← 进程 B：决策层
│  ├─ agents/                 # 策略配置（一份 json = 一个策略）
│  ├─ harness/
│  │  ├─ __main__.py          # python -m harness 入口
│  │  ├─ live.py              # 实盘驱动：单线程交错两个 Loop + 终端实时输出
│  │  ├─ scheduler.py         # Loop 1：定时唤醒
│  │  ├─ watchdog.py          # Loop 2：止损 / 强平扫描（不碰 LLM）
│  │  ├─ loop.py              # 七步 loop
│  │  ├─ signals.py           # ★ 交易指令出口
│  │  ├─ exit_plan.py         # 退路的解析 / 校验 / 只能收紧
│  │  ├─ memory.py            # 三条记忆路径
│  │  ├─ prompts/             # 五层系统提示词
│  │  ├─ tools/               # A/B/C/D 四类工具
│  │  ├─ paper/               # 撮合、费用、账本
│  │  └─ store/               # SQLite 连接与仓储
│  ├─ tests/
│  └─ schema.sql
└─ info-feeds/                ← 进程 A：数据层
   ├─ info_feeds/
   │  ├─ router.py            # 路由层（唯一对外入口）
   │  ├─ vendors/             # 各数据源实现
   │  └─ collector/           # 常驻采集进程
   ├─ TEXT_SOURCES.md         ← 文本数据源说明（数据参考）
   └─ _smoke*.db              # 采集器冒烟产物（可删）
```

---

## 9. 关键配置

`ccb-sub-agents/harness/config.py`（全部魔术数字集中于此，代码里不许出现裸常量）：

```python
# 下面这一节里，除"强平阈值"外都是**可被 agents/*.json 按策略覆盖**的默认值 ——
# 风险偏好是策略画像的一部分，全局值只是"没写时的兜底"（AgentSpec.risk_cfg）。
cooldown_after_stop      = 14400    # 止损后冷却秒数（= 一个唤醒周期）
stop_distance_min_pct    = 0.0      # 止损距离下限，防"止损紧到只是噪声"（0 = 不限）
stop_distance_max_pct    = 0.5      # 止损距离上限，防"名义上设了但形同虚设"
max_loss_per_trade_pct   = 0.02     # ★ 单笔最多亏权益的 2%
max_drawdown_halt        = 0.30     # ★ 累计回撤 30% -> 只许减仓
maintenance_margin_rate  = 0.005    # 强平阈值（按名义算）；交易所口径，不开放覆盖
scheduler_interval       = 10       # Loop 1
watchdog_interval        = 60       # Loop 2
max_concurrency          = 8        # LLM 限速
max_tool_rounds          = 16       # ②③ 自由段的轮数上限
tool_max_chars           = 6000     # 单次工具返回的字符上限
llm_temperature          = 0.0      # 保持 0，让同样的输入得到同样的输出
```
