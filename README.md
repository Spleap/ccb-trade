# CCB Trade

一个**最简的 AI 交易 Agent 框架**：定时唤醒 → 自己看市场 → 自己决定仓位与止盈止损 →
产出**格式化的交易指令**。

- **不实际下单**，不持有 API key。它只给出指令，执行由下游负责。
- **代码层面兜底风控**，Agent 不可能亏太多：单笔最大亏损、止损必须紧于强平线、累计回撤熔断。
- **每一笔单子都带止盈止损**，止损只能收紧、不能放宽。
- 详细设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。

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

在 `ccb-sub-agents/.env` 里写 LLM 的 key（默认 provider 是 DeepSeek）：

```
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

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

### 4. 跑测试

```bash
cd ccb-sub-agents
python -m pytest tests -q        # 57 个
```

---

## 配一个自己的策略

不用写代码，加一份 `ccb-sub-agents/agents/*.json` 就行：

```json
{
  "agent_id": "my-agent-01",
  "name": "我的策略",
  "persona": "你是……（人设随便写）",
  "universe": ["BTC/USDT", "ETH/USDT"],
  "tf": "15m",
  "wake_interval": 3600,
  "w": 0.5,
  "gross_cap": 3.0,
  "leverage": 1,
  "starting_equity": 1000.0,
  "tools": ["get_candles", "get_indicators", "get_derivatives", "get_news", "get_sentiment"]
}
```

| 字段 | 含义 |
|---|---|
| `persona` | 人设 / 交易理念，唯一完全自由的部分 |
| `universe` | 允许交易的品种池，**不能超出它** |
| `tf` | 主看周期 |
| `wake_interval` | 唤醒间隔（秒） |
| `w` | 仓位权重：单笔名义 = `ratio × w × 权益 × 杠杆` |
| `gross_cap` | 总敞口上限：`Σ|名义| ÷ 权益` |
| `leverage` | 杠杆倍数，1 = 现货口径 |
| `tools` | 允许用的数据工具子集（账户/记忆/下单工具永远可用） |

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
