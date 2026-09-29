# CCB Trade

一个**最简的 AI 交易 Agent 框架**：定时唤醒 → 自己看市场 → 自己决定仓位与止盈止损 →
产出**格式化的交易指令**。

- **不实际下单**，不持有 API key。它只给出指令，执行由下游负责。
- **代码层面兜底风控**，Agent 不可能亏太多：单笔最大亏损、止损必须紧于强平线、累计回撤熔断。
- **每一笔单子都带止盈止损**，止损只能收紧、不能放宽。

---

## 文档导航

| 文档 | 什么时候读 |
|---|---|
| **README.md**（本文件） | 先跑起来 |
| **[BUILD-A-STRATEGY.md](BUILD-A-STRATEGY.md)** | **想写自己的策略时，读这一份** |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 想改框架本身（提示词分层 / 七步 loop / 表结构） |
| [BITGET-DATA.md](BITGET-DATA.md) | 对量价数据源的口径有疑问 |
| [info-feeds/TEXT_SOURCES.md](info-feeds/TEXT_SOURCES.md) | 对新闻 / 情绪源有疑问 |

### 想写自己的策略，看 [BUILD-A-STRATEGY.md](BUILD-A-STRATEGY.md)

**不用写代码，加一份 `ccb-sub-agents/agents/*.json` 就是一个新策略。**
那份文档是"从零到跑通"的完整流程，按顺序读一遍即可上手：

| 它的章节 | 解决什么问题 |
|---|---|
| §0 心智模型 | 框架替你做了什么、你只负责什么 |
| §1 五分钟跑起来 | 装依赖 / 配 key / 起采集 / `--ticks 6` 预检 |
| §2 配置全貌 | 可直接抄的完整 JSON + 两张字段速查表 |
| §3 写 persona | 人设与**风险偏好**就写在这里 |
| §4 选标的 | 品种池的符号口径（加密 / 美股 / 指数 / 黄金） |
| §5 挑工具 | 只看世界的哪几个面，以及为什么只能裁数据类工具 |
| §6 标定风控 | 三道闸门怎么定、**杠杆与止损宽度的耦合公式** |
| §7 调试与验收 | CLI 用法、四个调试抓手、测试覆盖表 |
| §8 加一个数据源 | 两步接一个新源，以及唯一的约束 |
| §9 六个常见的坑 | 症状 → 怎么办 |

---

## 它长什么样

```
info-feeds（数据层，常驻采集）  ──写──▶  共享 SQLite  ◀──读──  harness（决策层，定时唤醒）
   新闻 / 情绪 / 宏观 / 预测市场                                     │
                                                                   ▼
                                            trade_signals（格式化交易指令 → 下游执行方）
```

Agent 自己不持有状态，每次唤醒从 DB 读、用完丢弃。两个进程只通过 DB 耦合，可各自独立重启。

---

## 快速开始

### 1. 装依赖

```bash
pip install openai python-dotenv pytest     # 决策层 harness
pip install requests pandas yfinance        # 数据层 info-feeds
```

### 2. 配 key

先复制模板，再填自己的 key：

```bash
cp .env.example ccb-sub-agents/.env
```

最少只要一行 —— LLM 的 key（默认 provider 是 DeepSeek）：

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

其余变量都有默认值或可缺省，全部说明在 [.env.example](.env.example) 里。
**`.env` 已被 `.gitignore` 忽略，绝不要把真 key 提交进仓库。**

### 3. 跑起来

```bash
# 决策层：一直跑到 Ctrl-C
cd ccb-sub-agents
python -m harness

# 只跑 6 个 tick（预检）
python -m harness --ticks 6

# 不接 LLM：只验证止损扫描与账本
python -m harness --no-llm
```

```bash
# 数据层：常驻采集（另开一个终端）
cd info-feeds
python -m info_feeds.collector          # 常驻
python -m info_feeds.collector --once   # 只跑一轮
```

两个进程**必须指向同一个库**：默认 `ccb-sub-agents/ccb_subagents.db`，
可用环境变量 `CCB_DB_PATH` 覆盖。

采集清单**自动跟随策略**：`CCB_WATCHLIST` 不设时，取各 `agents/*.json` 里 `universe`
的并集（并翻成信息层口径，如 `AAPL/USDT → AAPL`、`SPX/USDT → ^GSPC`）。
所以给 Agent 加标的不用再去改采集器，两边不会失配。想手工指定就设 `CCB_WATCHLIST`。

### 4. 跑测试

```bash
cd ccb-sub-agents
python -m pytest tests -q        # 67 个
```

---

## 配一个自己的策略

不用写代码，加一份 `ccb-sub-agents/agents/*.json` 就行：

> 下面是**最小可用版本**。完整的构建流程 —— 字段含义、品种池符号口径、
> 风控数字怎么标定、怎么调试与验收、怎么加新数据源 —— 见
> **[BUILD-A-STRATEGY.md](BUILD-A-STRATEGY.md)**。

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
    "take_profit": {"type": "atr", "value": 4.0}
  },
  "tools": ["get_candles", "get_indicators", "get_derivatives", "get_news", "get_sentiment"]
}
```

| 字段 | 含义 |
|---|---|
| `persona` | 人设 / 交易理念 / **风险偏好**，唯一完全自由的部分 |
| `universe` | 允许交易的品种池，**不能超出它** |
| `tf` | 主看周期 |
| `wake_interval` | 唤醒间隔（秒） |
| `w` | 仓位权重：单笔名义 = `ratio × w × 权益 × 杠杆` |
| `gross_cap` | 总敞口上限：`Σ|名义| ÷ 权益` |
| `leverage` | 杠杆倍数，风险偏好的主力旋钮之一。1 = 现货口径 |
| `max_loss_per_trade_pct` | 单笔最大亏损（占起始权益），"不能亏太多"的兜底 |
| `max_drawdown_halt` | 账户累计回撤熔断线，破了只许减仓 |
| `stop_distance_min_pct` / `max_pct` | 止损距离的允许区间：太近是噪声，太远形同虚设 |
| `cooldown_after_stop` | 止损后同方向的冷却时长（秒），防报复性交易 |
| `default_exit_plan` | 默认退路：LLM 省略 `exit_plan` 时框架替它套上 |
| `tools` | 允许用的数据工具子集（账户/记忆/下单工具永远可用） |

**风险偏好是每个 Agent 自己写一份的**（上面那 6 个风控字段），而且**全部可选、平铺在顶层**：
没写的字段回落全局默认，所以老配置不用改。全局一份值套在所有策略上，等于没有策略画像 ——
同一个人设写"激进"还是"保守"，落在代码里就是这几个数字的差别。

杠杆不是随手填的：它和 `w` 一起决定**满仓时止损最宽能放到几%** ——

```
满仓（ratio=1）允许的最大止损% = max_loss_per_trade_pct × 权益 ÷ (w × 权益 × 杠杆)
```

`w=0.5`、权益 1000、单笔上限 2%（= 20U）、杠杆 2x → 满仓名义 1000U → 止损最宽 **2%**。
1h 波段的 2×ATR 止损大约 1.1~2.5%（BTC 1.1%、XRP 2.5%），所以 2x 刚好放得下；
填 10x 的话上限会被压到 0.4%，那不是波段止损，是噪声。

**品种池不限加密。** Bitget 的 `USDT-FUTURES` 合约里就有美股（`AAPLUSDT` / `NVDAUSDT`）、
指数（`SPXUSDT` / `NDX100USDT`）、贵金属（`XAUUSDT`）与外汇（`EURUSDUSDT`）——
不需要额外的行情源，`universe` 里直接写 `AAPL/USDT`、`SPX/USDT` 就行（符号自动映射）。

**人设可以随便写，但写不出一个能突破风控的 Agent** —— 风控由代码执行，提示词只是让它提前知道。

---

## 它有哪些工具

分四类。**能裁的只有 A 类**（配置里 `tools` 字段）—— 看自己、记忆、下单出口永远在。

| 类 | 工具 |
|---|---|
| A 数据 | `get_candles` `get_indicators` `get_derivatives` `get_news` `get_global_news` `get_sentiment` `get_sentiment_index` `get_market_events` `get_prediction_market` `get_macro` |
| B 账户 | `get_my_portfolio` `get_my_budget` `get_my_recent_decisions` |
| C 记忆 | `recall` |
| D 决策 | `propose_target` `get_exit_plan` `amend_exit_plan` `precheck` |

**行情来自 Bitget USDT-FUTURES 永续**（不是现货 —— 止损与强平都按合约价算）：

- **K 线**：只含**已完结**的 bar，正在走的那根永远被挡在外面（防前视）。
- **技术指标**：服务端算，不让模型自己算（同一个 RSI 两次算出不同值，就没法复盘了）。
  目前支持 `sma20` `sma50` `ema12` `ema26` `rsi14` `atr14` `macd` `boll20`。
- **派生品指标** `get_derivatives`：持仓量 OI、资金费率（当期 + 最近若干期）、标记价、指数价、基差。
  杠杆策略的必看项 —— 费率是持仓成本与多空拥挤度，OI 是这个方向上有多少钱在下注。
  ⚠ **OI 只有当前值**：Bitget 不提供 OI 历史序列（`type=open_interest` 那个接口是个陷阱，
  它会静默返回普通 K 线），所以别指望它给趋势。

**信息类工具（`get_news` 那一组）的返回有三种，别混：**

| 返回 | 含义 |
|---|---|
| `DATA_UNAVAILABLE: …` | **事故** —— 取数失败了 |
| `（无数据：…）` | **事实** —— 源是活的，那段时间确实什么都没发生 |
| `（数据源停摆 / 尚未接入采集 …）` | **这条信息不可信** —— 不是"很平静"，是"数据断了" |

第三种是刻意加的：`news_items` 查回空可能是世界很安静，也可能是采集进程死了三天 ——
只看表本身，这两件事长得一模一样。判定靠 `source_health` 表（采集侧写、决策侧读），
超过 `Config.info_stale_seconds`（默认 6h）没成功过就报"停摆"。不这么分，
LLM 一定会把事故读成"今天很安静"，然后在一个它看不见的世界里下单。

---

## 它产出什么

一次通过复核的决策 → 一行 `trade_signals`，同时打在终端上：

```
📤 指令 [open    ] BTC/USDT long  qty=+0.000737@84815.7 10x SL=83200 TP=88500 risk=1.19
```

| 字段 | 含义 |
|---|---|
| `action` | `open` / `increase` / `reduce` / `close` / `reverse` |
| `qty` | **目标持仓量**（带符号），不是本次买卖量 |
| `entry_price` | 参考价，下游按自己的盘口成交 |
| `stop_loss` `take_profit` | 止盈止损，绝对价 —— 每笔都有 |
| `leverage` `risk_amount` | 杠杆；触发止损时的亏损额 |
| `reason` `evidence` | 决策理由；这一刻它看到了什么 |

同标的上一条未覆盖的指令会被标成 `superseded`（只增不改）—— 下游据此判断哪条还有效。

---

## 目录

```
ccb-trade/
├─ ARCHITECTURE.md      # 完整设计说明
├─ README.md
├─ BITGET-DATA.md       # Bitget 行情接口说明
├─ ccb-sub-agents/      # 决策层：harness + agents/*.json + tests
└─ info-feeds/          # 数据层：路由 + 各数据源 + 常驻采集进程
```

---

## 注意

- **不构成投资建议**，这只是个技术框架。
- `ccb-sub-agents/.env` 含私钥性质的内容，**已在 .gitignore 中排除，不要提交**。
