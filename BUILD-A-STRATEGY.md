# 怎么用这个框架写一个交易策略

**一个新策略 = 一份 JSON 配置，不是一次写代码。** 这份文档只讲"从零到一个跑起来的策略"
需要做的每一步，以及每一步背后为什么这么设计。想了解整体架构看
[ARCHITECTURE.md](ARCHITECTURE.md)，想快速上手看 [README.md](README.md)。

---

## 0. 心智模型：框架替你做了什么，你只负责什么

```
info-feeds（常驻采集）──写──▶ 共享 SQLite ◀──读── harness（定时唤醒）
                                                      │
                                    ┌─────────────────┴─────────────────┐
                                    │ 每个唤醒跑一次七步 loop            │
                                    │ ① LoadContext ② Observe ③ Deliberate│
                                    │ ④ Propose ⑤ Validate ⑥ Execute ⑦ Journal│
                                    └───────────────────────────────────┘
```

- **框架负责**：定时唤醒、取数工具的规格与上限、风控三道闸门、止损扫描（60s 一轮，
  纯代码不碰 LLM）、Paper 账本、决策留痕、把意图翻成给下游的 `trade_signals`。
- **你负责**：`persona`（人设与风险偏好）、`universe`（允许做哪些品种）、
  `tools`（能看到世界的哪几个面）、风控参数（亏多少算亏太多）、`default_exit_plan`（默认退路）。

**唯一写出口是 `propose_target`。** LLM 不能下单、不能改仓位、不能写库 —— 它只能提交意图，
由框架裁决。所以"写提示词绕过风控"在结构上不存在。

---

## 1. 五分钟跑起来

### 1.1 装依赖

```bash
pip install openai python-dotenv pytest        # 决策层
pip install requests pandas yfinance           # 数据层（走 info-feeds 才需要）
```

### 1.2 配 LLM key

先复制模板（`.env` 已被 `.gitignore` 忽略，**别把真 key 提交进仓库**）：

```bash
cp .env.example ccb-sub-agents/.env
```

然后在 `ccb-sub-agents/.env` 里填（默认 provider 是 DeepSeek，任何 OpenAI 兼容端点都能接）：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx
# 可选：换 provider / 模型
# CCB_LLM_PROVIDER=openai
# OPENAI_API_KEY=sk-xxxxxxxx
```

其余变量（`CCB_DB_PATH` / `CCB_WATCHLIST` / `FRED_API_KEY` 等）都有默认值或可缺省，
全部说明在 [`.env.example`](.env.example) 里。key **只从环境变量读，不落任何配置文件**。

### 1.3 起采集进程（另开一个终端）

```bash
cd info-feeds
python -m info_feeds.collector --once    # 先冒烟跑一轮
python -m info_feeds.collector           # 确认没问题就常驻
```

采集侧和决策侧**必须指向同一个库**（默认 `ccb-sub-agents/ccb_subagents.db`，
可用 `CCB_DB_PATH` 覆盖）。

> 采集清单是**自动跟随策略**的：你不设 `CCB_WATCHLIST` 时，它取所有 `agents/*.json`
> 里 `universe` 的并集，并翻成信息层口径。所以你给策略加标的，不用再去改采集器。

### 1.4 写第一份策略并跑起来

```bash
cd ccb-sub-agents
python -m harness --ticks 6 --agent my-agent-01   # 跑 6 个唤醒就停，预检用
python -m harness --agent my-agent-01             # 没问题就常驻
```

没配 key 也能预检账本与止损扫描：

```bash
python -m harness --no-llm --ticks 6    # 所有唤醒走降级，只验证风控与账本
```

---

## 2. 配置全貌

放在 `ccb-sub-agents/agents/<agent_id>.json`。下面这份可以直接抄：

```json
{
  "agent_id": "my-agent-01",
  "name": "我的策略",
  "persona": "你是……（人设与风险偏好都写在这里）",

  "universe": ["BTC/USDT", "ETH/USDT", "AAPL/USDT", "SPX/USDT"],
  "tf": "1h",
  "wake_interval": 14400,

  "w": 0.5,
  "gross_cap": 3.0,
  "leverage": 2,
  "starting_equity": 1000.0,

  "max_loss_per_trade_pct": 0.02,
  "max_drawdown_halt": 0.30,
  "stop_distance_min_pct": 0.004,
  "stop_distance_max_pct": 0.5,
  "cooldown_after_stop": 14400,
  "default_exit_plan": {
    "stop_loss":   {"type": "atr", "value": 2.0},
    "take_profit": {"type": "atr", "value": 4.0},
    "trailing":    {"enabled": true, "mult": 1.5}
  },

  "tools": ["get_candles", "get_indicators", "get_derivatives",
            "get_news", "get_global_news", "get_sentiment"]
}
```

### 字段速查

| 字段 | 必填 | 含义 |
|---|---|---|
| `agent_id` | ✅ | 唯一标识，也是 DB 里的主键 |
| `name` | ✅ | 展示名 |
| `persona` | ✅ | 人设 + **风险偏好**，唯一完全自由的部分 |
| `universe` | — | 允许交易的品种池（见 §4） |
| `tf` | — | 主看周期：`1m` `5m` `15m` `30m` `1h` `4h` `1d`，默认 `1h` |
| `wake_interval` | — | 唤醒间隔（秒）。看 1h 线就配 `14400`（4h 一次） |
| `w` | — | 仓位权重：单笔名义 = `ratio × w × 权益 × 杠杆` |
| `gross_cap` | — | 总敞口上限：`Σ|名义| ÷ 权益` |
| `leverage` | — | 名义放大倍数，`1` = 现货口径 |
| `starting_equity` | — | 子账户起始权益，累计回撤熔断的基准 |
| `tools` | — | 允许用的**数据**工具子集（见 §5） |

风控字段（**全部可选，平铺顶层，不写就回落全局默认**，所以老配置不用改）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_loss_per_trade_pct` | `0.02` | 单笔最大亏损（占起始权益） |
| `max_drawdown_halt` | `0.30` | 账户累计回撤熔断线，破了只许减仓 |
| `stop_distance_min_pct` | `0.0` | 止损距离下限（防"紧到只是噪声"），`0` = 不限 |
| `stop_distance_max_pct` | `0.5` | 止损距离上限（防"名义上设了但形同虚设"） |
| `cooldown_after_stop` | `14400` | 止损后同方向冷却秒数，防报复性交易 |
| `default_exit_plan` | 无 | 默认退路：LLM 省略 `exit_plan` 时框架替它套上 |

> `cooldown_after_stop` 要 **≥ 一个唤醒周期**，否则它在下一轮唤醒前就过期了，等于没有。

---

## 3. 写 persona：风险偏好就写在这里

`persona` 是唯一没有格式约束的部分，但要记住一件事：
**它改不了规则，只是让 LLM 提前知道会撞哪面墙。**

系统提示词分五层，你只能动 L1，L2 由配置渲染，**L3 规则 / L4 流程 / L5 输出契约是模板注入的、
人设改不了**。所以你可以随便写"我要 aggressive"，但真正决定它有多 aggressive 的是
`leverage` / `w` / `max_loss_per_trade_pct` 那几个数字。

一份够用的 persona 包含四件事：

1. **你是谁、做什么周期** —— "做中低频波段跟随，主看 1h，用 4h 确认方向"
2. **你怎么看世界** —— "只认图形与量价结构，不因为是币还是股就区别对待"
3. **你的风险胃口** —— "清晰信号一次下够，宁可重仓也不分十次追"
4. **你的纪律** —— "判断被证伪就干脆认错，绝不为面子死扛"

现成样本见 [`agents/trend-scout-01.json`](ccb-sub-agents/agents/trend-scout-01.json)。

---

## 4. 选标的：符号口径

`universe` 里写**交易口径**的符号，推荐 `BASE/USDT` 这种写法：

```json
"universe": ["BTC/USDT", "ETH/USDT", "AAPL/USDT", "SPX/USDT", "XAU/USDT"]
```

框架会自动做两边映射，你不需要关心：

| 你写的 | Bitget（量价） | Yahoo（新闻/情绪） |
|---|---|---|
| `BTC/USDT` | `BTCUSDT` | `BTC-USD` |
| `AAPL/USDT` | `AAPLUSDT` | `AAPL` |
| `SPX/USDT` | `SPXUSDT` | `^GSPC` |
| `XAU/USDT` | `XAUUSDT` | `GC=F`（COMEX 黄金期货） |
| `EUR/USD` | `EURUSDUSDT` | `EURUSD=X` |

**不限加密。** Bitget 的 `USDT-FUTURES` 里有美股（`AAPL` `NVDA` `TSLA` `MSFT` `META`
`GOOGL` `AMZN`）、指数（`SPX` `NDX100` `HSI`）、贵金属（`XAU` `XAG`）、
外汇（`EURUSD` `GBPUSD` `USDJPY`）—— 同一套 USDT 合约，**不需要额外的行情源**。

两条硬约束：

- **池外的品种会被协议层直接拒**（提示词里写着不算数）。清掉池外的旧持仓用 `ratio = 0`，永远放行。
- 标的必须在 Bitget 上有合约，否则取不到 K 线 → 拿不到参考价 → 该轮不下单。
  查一下有没有：

```bash
python -c "import requests,json;d=requests.get('https://api.bitget.com/api/v3/market/tickers',params={'category':'USDT-FUTURES'},timeout=15).json()['data'];print(len(d));print([x['symbol'] for x in d if x['symbol'].startswith(('AAPL','NVDA','SPX'))])"
```

**没列到的非加密品种**：量价照常走 Bitget，但新闻/情绪会查回空。所以新接一个非加密
标的时，记得同时把它的 Yahoo 口径加进 `harness/tools/data.py` 的 `_INFO_ALIASES`
和 `info-feeds/.../collector/config.py` 的那份副本（两边**逐字相同**，改一边就要改另一边）。

---

## 5. 挑工具：只看世界的哪几个面

四类工具，**你能裁的只有 A 类**：

| 类 | 工具 | 能裁吗 |
|---|---|---|
| A 数据 | `get_candles` `get_indicators` `get_derivatives` `get_news` `get_global_news` `get_sentiment` `get_sentiment_index` `get_market_events` `get_prediction_market` `get_macro` | ✅ `tools` 字段 |
| B 账户 | `get_my_portfolio` `get_my_budget` `get_my_recent_decisions` | ❌ 永远在 |
| C 记忆 | `recall` | ❌ 永远在 |
| D 决策 | `propose_target` `get_exit_plan` `amend_exit_plan` `precheck` | ❌ 永远在 |

**为什么只裁 A 类**："先看自己"是框架强制的第 ① 步，出口更不可能靠配置裁掉。
配置能决定的只是"它能看到世界的哪几个面"。

三条选型经验：

- **做杠杆就必须给 `get_derivatives`**。资金费率是持仓成本、也是多空拥挤度的直接读数；
  只看 K 线的杠杆策略是在闭着眼睛付费。
- **给信息工具的策略会被强制"每轮至少看一次外部信息"**（L4 规则）。如果你确实想做
  纯量价的对照实验，就**别给**新闻类工具 —— 框架会按工具集如实渲染，
  告诉它"你没有外部信息源，不要凭记忆编消息面"。
- **不要给不需要的工具**。工具越多，幻觉面越大。

**信息类工具的返回有三种，读日志时别混**：`DATA_UNAVAILABLE`（事故，取数失败）、
`（无数据：…）`（事实，源是活的、那段时间确实没事）、
`（数据源停摆／尚未接入采集…）`（**这条信息不可信**）。第三种意味着采集进程可能挂了 ——
去查 `source_health` 表。

---

## 6. 标定风控：数字怎么定

三道闸门，从紧到松：

```
① 单笔最大亏损   名义 × 止损距离 ≤ max_loss_per_trade_pct × 起始权益
② 止损 vs 强平   止损必须紧于强平距离，否则价格碰止损前你已经被强平了
③ 累计回撤熔断   权益跌破起始的 (1 - max_drawdown_halt) → 只许减仓
```

外加一条**止损距离区间**：`stop_distance_min_pct ~ stop_distance_max_pct`。
比下限还近的全是噪声（开仓就会被无意义地打掉，手续费照付），比上限还远则等于没设。

### 杠杆和仓位是耦合的

```
满仓（ratio=1）允许的最大止损% = max_loss_per_trade_pct ÷ (w × 杠杆)
```

`w=0.5`、`max_loss_per_trade_pct=0.02`：

| 杠杆 | 满仓时止损最宽 |
|---|---|
| `1` | 4.0% |
| `2` | **2.0%** ← 1h 波段合适 |
| `10` | 0.4% ← 那不是波段止损，是噪声止损 |

**杠杆填错，止损宽度就被结构性锁死** —— persona 里写"退路要宽"也没用。

`gross_cap` 要跟着杠杆标定：它限制 `Σ|名义| ÷ 权益`，而单笔名义上限是
`w × 权益 × 杠杆`。杠杆降下来后 `gross_cap` 不跟着降，这个组合层闸门就永远不会触发。

### 默认退路怎么给

`default_exit_plan` 是**兜底，不是天花板**：LLM 省略 `exit_plan` 时框架替它套上；
它自己给了更贴合当下形态的，就以它为准。三种口径（`stop_loss` / `take_profit` 通用）：

```jsonc
{"type": "atr",   "value": 2.0}    // 2 倍 ATR —— 推荐，自适应波动
{"type": "pct",   "value": 0.02}   // 开仓价的 2%
{"type": "price", "value": 58000}  // 绝对价
```

参考：1h 尺度上 BTC 的 ATR14 约 0.56%、ETH 0.75%、SOL 0.98%、XRP 1.25% ——
所以 `2×ATR` 波段止损大致落在 1.1%~2.5%，`leverage=2` 刚好装得下。

---

## 7. 调试与验收

```bash
# 预检：6 个唤醒就停，看清它每步在干什么（默认流式打在终端上）
python -m harness --ticks 6 --agent my-agent-01

# 不接 LLM：只验证止损扫描与账本
python -m harness --no-llm --ticks 6

# 换 LLM
python -m harness --provider openrouter --model openai/gpt-4o-mini
```

调试时有几个抓手：

- **被拒的原因会回灌给 LLM**，它有一次改正机会。日志里 `rejected: …` 那句话就是设计给它的。
- **`precheck` 是 LLM 的自我试算**，不产生任何后果 —— 它会先撞墙再正式提，省一轮往返。
- **`market_snapshot` 表记录"它当时看到了什么"**。止损为什么在这个位置、它是根据哪根
  K 线做的决定，都在这张表里。
- **`trade_signals` 是唯一交付物**，你接下游就消费这张表。

跑测试（改完框架代码再跑，改配置不用）：

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

## 8. 加一个新数据源

框架不区分"官方源"和"你自己接的源"，**加源只要两步**：

1. 在 `info-feeds/info_feeds/collector/sources.py` 写一个
   `collect_x(conn) -> Collected`，把 vendor 的行映射成表列，调 `store.insert_*()` 幂等写库。
2. 往同文件底部的 `SOURCES` 元组加一行：
   `Source("x", 300, collect_x, "说明")` —— `300` 是它自己的采集节奏（秒）。

`store.insert_*` 全是 `INSERT … ON CONFLICT DO UPDATE`，**幂等**：进程重启、重试都不会写出重复行。
源出问题**不传染**：runner 逐个源捕获异常、单独记 `source_health`，一个源挂了不影响别的源。

> **唯一的约束**：新源必须落进已定的 6 张表之一 ——
> `news_items` / `social_items` / `market_events` / `prediction_quote` /
> `macro_series` / `sentiment_index`。要凭空多一种"内容类型"，得同时动表、读工具
> 和**两份必须逐字一致的 schema**（`info-feeds/.../collector/schema.sql` 与
> `ccb-sub-agents/schema.sql`）。

接完源，把读工具用到的源名登记进 `harness/config.py` 的 `INFO_SOURCES` ——
那样"采集挂了"才会被读侧认出来，报"数据源停摆"而不是"没有新闻"。

---

## 9. 六个常见的坑

| 坑 | 症状 | 怎么办 |
|---|---|---|
| 采集进程没起 | 信息工具全报"从未成功采集" | `cd info-feeds && python -m info_feeds.collector` |
| 两个进程指向不同的库 | LLM 永远看不到新闻 | 两边设同一个 `CCB_DB_PATH` |
| 杠杆填太大 | 止损宽度被压到噪声级，开仓即被打掉 | 用 §6 的公式反推，1h 波段一般 `leverage ≤ 3` |
| `cooldown_after_stop` < `wake_interval` | 冷却期在下次唤醒前就过期，等于没有 | 设成 ≥ 一个唤醒周期 |
| 给品种池加了非加密品，却忘了加信息层映射 | 量价正常，但新闻/情绪查回空 | 两份 `_INFO_ALIASES` 都要加 |
| 用 `tf` 之外的周期名 | 取 K 线直接报错 | 只能 `1m 5m 15m 30m 1h 4h 1d` |
