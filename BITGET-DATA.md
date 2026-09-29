# Bitget 行情数据接入设计（v1 数据平面）

> 本文是 `PLAN.md` 第 6.2 节的展开实现说明。
> **v1 范围**：Bitget 全部加密货币现货 + 全部加密货币永续合约；不下真实订单，只输出交易指令。
>
> ✅ = 已在本机实测确认（2026-09-29，VPN 出口）
> ⚠️ = 仍需在 M2a 精确定位

---

## 0. 实测结论摘要

| # | 结论 | 状态 |
| --- | --- | --- |
| 1 | **v3 API 完全够用**，不需要 v2 历史接口（v2 spot history-candles 实测全部 400） | ✅ |
| 2 | v1 可交易 **4,260 个** symbol_id（现货 3,387 + U 永续 804 + USDC 永续 49 + 币本位永续 20） | ✅ |
| 3 | 现货含 **2,813 个代币化股票**，用户决定**全部纳入** → 必须处理交易时段 | ✅ |
| 4 | v3 candles **包含未闭合的当前 bar** → 必须显式排除，否则 lookahead | ✅ |
| 5 | `ts` 是 bar 的**开始**时间；`startTime` **排他**、`endTime` **包含** | ✅ |
| 6 | `limit` 最大 **1000**（1001 直接 400）；`interval` **大小写敏感**（`1h` → 400） | ✅ |
| 7 | **1m 只能回溯约 30 天**；1H/4H/1D 可回溯数年 | ✅ |
| 8 | `tickers` 一次返回**全量**（含 fundingRate / openInterest）→ 4 次请求拿全市场 | ✅ |
| 9 | 代币化股票 **98% 成交额集中在 UTC 13:30–20:00**（美股时段），休市时价格冻结 | ✅ |
| 10 | `turnover24h ≈ price × volume24h`（比值 0.99–1.00），字段可信 | ✅ |

**三个改变架构的结论**：

- **#4 决定了 lookahead 防护的实现方式**——不能只做"时间戳截断"，必须做"bar 闭合判定"。
- **#3+#9 决定了 `focus_set` 与 Gate 的形态**——4,260 个标的无法全量喂 LLM，且**代币化股票必须交易时段感知**，
  否则凌晨唤醒时 Agent 会看到一屏冻结的股票代币。
- **#10 决定了筛选器可以放心用 `turnover24h` 打分**（但必须叠加时段权重，见 §1.4）。

---

## 1. v1 品种范围（已实测）

### 1.1 四条产品线（实测）

| `category` | 总标的 | 纳入 v1 | 排除 |
| --- | --- | --- | --- |
| `SPOT` | 3,393 | **3,387**（`status=online`；含 crypto 578 / stock 2,813 / metal 2） | 6 个 `limit_open` |
| `USDT-FUTURES` | 804 | **804**（全部 `type=perpetual`；crypto 477 / stock 317 / metal 7 / commodity 3） | — |
| `USDC-FUTURES` | 49 | **49** | — |
| `COIN-FUTURES` | 24 | **20**（`type=perpetual`） | 4 个 `type=delivery`（交割） |
| `MARGIN` | 321 | ❌ 不纳入 | v1 不做杠杆现货 |
| **合计** | 4,591 | **4,260** | |

> **用户决策（已确认）**：v1 **包含全部资产类别**——代币化股票（stock）、贵金属（metal）、商品（commodity）
> 与 crypto 同等对待。这是本设计里最影响架构的一个决定，见 §1.4。

### 1.2 纳入规则

```python
def is_tradable(inst: Instrument) -> bool:
    return (
        inst.category in {"SPOT", "USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES"}
        and inst.status == "online"
        and (inst.category == "SPOT" or inst.type == "perpetual")   # 排除交割
    )
```

**全部入库**（保持与交易所一致），`is_tradable()` 在读取时过滤，并把结果预计算进 `instruments.tradable` 列。
**不按 `symbolType` 过滤**——但要**把 `asset_class` 一路带到 Agent 面前**（见 §1.4）。

### 1.3 规范符号 `symbol_id`

**问题**：`BTCUSDT` 在 `SPOT` 与 `USDT-FUTURES` 下**同名但不同标的**。只用 `BTCUSDT` 会导致 Agent 无法表达"看多现货、看空永续"，持仓表也会撞主键。

**方案**：全系统内部一律用 `symbol_id = "{category}:{symbol}"`。

```
SPOT:BTCUSDT              → BTC/USDT (spot)
USDT-FUTURES:BTCUSDT      → BTC/USDT (perp)
COIN-FUTURES:BTCUSD_CM    → BTC/USD  (coin-perp)
SPOT:RSPYUSDT             → SPY/USDT (spot, tokenized stock)
```

**注意**：币本位合约的 `symbol` 自带 `_CM` 后缀（实测：`BTCUSD_CM`、`DOGEUSD_CM`），
不要试图剥离——它是交易所的正式 symbol。

`data/bitget/symbols.py`：

```python
def to_symbol_id(category: str, symbol: str) -> str      # ("SPOT","BTCUSDT") -> "SPOT:BTCUSDT"
def split_symbol_id(symbol_id: str) -> tuple[str, str]
def display_name(symbol_id: str) -> str                  # "BTC/USDT@spot"
def from_display_name(name: str) -> str
```

**硬性规则**：DB 主键、事件 payload、工具参数、Prompt 文本一律用 `symbol_id`；
只在渲染给人看时转 `display_name`。加测试 `tests/data/test_symbol_id_roundtrip.py`（对全量 catalog 双向转换）。

### 1.4 ⚠️ 资产类别与交易时段（**决定 focus_set 与 Gate 的形态**）

> 用户决定纳入全部资产类别后，**必须**处理下面这件事，否则系统会在凌晨给 Agent 喂一堆没法交易的股票代币。

#### 实测证据

**证据 A：代币化股票严格跟随美股时段**

RSPYUSDT（代币化 SPY）过去 168 根 1H bar 的成交额按 UTC 小时分布：

```
小时  00 01 02 03 04 05 06 07 08 09 10 11 | 12 13 14 15 16 17 18 19 20 21 | 22 23
占比   0  0  0  0  0  0  0  0  0  0  0  0 |  1  8 14  9 10  8  8 22 17  1 |  0  0
                                          └────── 美股时段 UTC 13:30–20:00 ──────┘
```

对照 BTCUSDT：24 小时均匀分布（每档 2%–9%）。

→ **代币化股票 98% 的成交额集中在 UTC 13:30–20:00**（= 美东 09:30–16:00）。
→ 北京时间 = **21:30–04:00**。

**证据 B：休市期间 bar 依然存在，但成交额极小**

RSPYUSDT 最近 96 根 15m bar：`bars=96`，跨度 23.8h **完全连续**，`成交量>0` 的 bar = 96/96。

→ **Bitget 的代币化股票名义上 24/7 都在产生 bar**，休市时价格几乎冻结、成交额萎缩到峰值的千分之几。
→ **这不是"数据缺口"**，是"休市"。两者必须区分，否则 `bars_gap` 会天天报警。

**证据 C：turnover 字段本身可信**

`turnover24h ≈ lastPrice × volume24h`，实测比值 0.99–1.00（BTCUSDT 0.9905、RSPYUSDT 1.0015、RNVDAUSDT 0.9963）。

→ 字段自洽，**可以用**。但量级差异极大：

| 标的 | 24h 成交额 |
| --- | --- |
| `SPOT:RSPYUSDT`（SPY 代币） | **$22.4 B** |
| `SPOT:RQQQUSDT` | $19.7 B |
| `SPOT:RNVDAUSDT` | $16.7 B |
| `SPOT:BTCUSDT`（BTC 现货） | $374 M |

**代币化股票头部的成交额是 BTC 现货的 60 倍。**

#### 三条强制设计后果

**① `focus_set` 筛选器必须交易时段感知**

若直接按 `turnover24h` 排序，股票代币会因美股时段的巨量成交**全天 24 小时霸占 focus_set**。
结果是：北京时间凌晨 3 点唤醒时，Agent 看到的 50 个标的里绝大部分是**价格冻结、无法有效成交**的股票代币。

修正后的筛选器：

```python
class FocusSelector:
    def select(self, inputs, n: int = 50) -> list[str]:
        crypto, session_open, session_closed = [], [], []
        for sid in candidates:
            ac = asset_class(sid)                       # crypto / stock / metal / commodity
            if ac == "crypto" or is_24_7(sid):
                crypto.append(sid)
            elif in_session(sid, now):                  # 美股/商品时段
                session_open.append(sid)
            else:
                session_closed.append(sid)

        # crypto 永远占基础名额；非 24/7 标的只在自身交易时段内参与竞争
        base   = crypto
        extra  = rank(session_open)
        result = forced + rank(base)[: n - len(forced)]
        result += [s for s in extra if s not in result][: max(0, n - len(result))]

        # session_closed 的标的：可以出现在上下文里，但必须显式标注「休市中」
        # 且**不占用** focus_set 名额
        return result
```

**规则**：
- `session_closed` 的标的**不占 focus_set 名额**，但若它有持仓 / 有活跃 task，则**强制保留并标注"休市中"**。
- 渲染时每个非 24/7 标的必须带 `session: open | closed | pre | post` 标记。

**② `MarketSlice` 需要新增两个字段**

```python
class MarketSlice(BaseModel):
    ...
    asset_class: Literal["crypto", "stock", "metal", "commodity"]
    session: Literal["open", "closed", "pre", "post", "24_7"]
    last_trade_age_seconds: float | None   # 距上一笔真实成交的时间
```

- `data_age_seconds` 衡量的是**数据新鲜度**（ticker 的 ts 是不是刚刚）；
- `last_trade_age_seconds` 衡量的是**标的活跃度**（上一次真实成交是多久前）。
- **休市时前者很小、后者很大**——只看前者会误判"数据很新，可以交易"。

**③ Gate 新增 `session_open` 检查**

非 24/7 标的在 `session == "closed"` 时**禁止 Open**（平仓/减仓仍允许）。理由：
休市时点差极大、成交额近乎为零，开仓的滑点不可控，产生的是不可执行的指令。

**④ 缺口处理必须区分两类**

| 类型 | 表现 | 处理 |
| --- | --- | --- |
| **休市 gap** | 非 24/7 标的在已知休市窗口内无成交 | **正常**，写 `session_calendar`，不进 `bars_gap` |
| **数据缺失 gap** | 24/7 标的有缺口，或交易时段内有缺口 | **异常**，进 `bars_gap`，`doctor` 告警 |

**⑤ 交易时段日历**

新增 `infofeeds/sessions.py`：

```python
# 美股（含代币化股票的 pre/post 延展）
US_EQUITY = SessionSpec(
    regular=("13:30", "20:00"),   # UTC, 夏令时；冬令时 14:30–21:00
    pre=("08:00", "13:30"),
    post=("20:00", "00:00"),
    darks=["2026-11-26", "2026-12-25"],       # 美股假日
)
```

- **必须处理夏令时切换**（EDT/EST），否则每年两次整体偏移一小时。
- 冬夏令时切换日期与美股假日需要一张可维护的表（v1 可先硬编码 + `doctor` 提醒）。
- **M2a 必须实测确认** Bitget 代币化股票是否跟随美股假日休市（用美股假日的 bar 成交额验证）。

---

## 2. 接口清单（仅 v3）

### 2.1 主链路

| 用途 | 接口 | 关键参数 | 限速 |
| --- | --- | --- | --- |
| 品种目录 | `GET /api/v3/market/instruments` | `category`(必填), `symbol`(可选) | 20/s |
| **全量行情快照** | `GET /api/v3/market/tickers` | `category`(必填), `symbol`(可选) | 20/s |
| **K 线** | `GET /api/v3/market/candles` | `category`, `symbol`, `interval`, `startTime`, `endTime`, `type`, `limit` | 20/s |
| 深度（v2 预留） | `GET /api/v3/market/orderbook` | `category`, `symbol` | 20/s |
| 最近成交（v2 预留） | `GET /api/v3/market/fills` | `category`, `symbol`, `limit≤100` | 20/s |

**不需要的接口**：`/api/v2/spot/market/history-candles`（实测所有 granularity 均返回 400）、
`/api/v2/mix/market/history-candles`（v3 已能深度回溯）、`/api/v2/mix/market/current-fund-rate`
与 `/open-interest`（tickers 已含，见 §2.4）。

### 2.2 K 线响应格式（已实测）

```json
{
  "code": "00000", "msg": "success", "requestTime": 1790668733806,
  "data": [
    ["1790666100000", "83983.77", "84072.44", "83920.33", "83994.19", "22.575309", "1896282.84793552"],
    ["1790667000000", "83994.19", "84202.15", "83960",    "84020.36", "24.283361", "2041842.28505639"]
  ]
}
```

`data` 元素是**定长 7 元数组**（全部为字符串）：

| 下标 | 字段 | 说明 |
| --- | --- | --- |
| 0 | `open_time` | **bar 开始时间**，Unix 毫秒 |
| 1 | `open` | |
| 2 | `high` | |
| 3 | `low` | |
| 4 | `close` | |
| 5 | `base_volume` | 基础币成交量（币本位合约为合约张数口径，实测值极小） |
| 6 | `quote_volume` | 计价币成交额 |

> 解析器必须做**长度断言**：`len(item) == 7`，否则抛 `BitgetFormatError`（fail-closed）。

### 2.3 ⚠️ **最重要的一条：v3 包含未闭合 bar**

实测证据：

```
requestTime = 1790668733806          (服务器"现在")
最后一根 15m bar = 1790667900000
   → bar 区间 [1790667900000, 1790668800000)
   → 1790668800000 > 1790668733806  ⇒ 这根 bar 还没走完（还剩 66 秒）
   结论：ts 是 bar 开始时间，且 v3 candles 返回了"正在走"的当前 bar
```

**这意味着**：
- 简单做"取 `ts <= as_of` 的 K 线"是**错的**——会把一根尚未闭合的 bar 当成已完成的事实；
- 该 bar 的 `high`/`low`/`close` 会随行情继续变化，等于**把未来信息当历史**；
- 在 `as_of = 09:07` 时，`09:00–09:15` 这根 bar 的 `close` 是"此刻的瞬时价"，不是"09:15 的收盘价"。

**正确做法**（`BitgetDataPort.chart()`）：

```python
def chart(self, symbol_id: str, interval: str, n: int) -> list[Bar]:
    as_of_ms = int(self.clock.now().timestamp() * 1000)
    # 只保留"已闭合"的 bar：open_time + interval <= as_of
    upper_open_time = as_of_ms - INTERVAL_MS[interval]
    rows = query_bars(symbol_id, interval, open_time__lte=upper_open_time, limit=n)
    return [Bar(**r) for r in rows]
```

**配套三个约束**：
1. 未闭合的那根通过 `partial_bar()` **单独**提供，且 `Bar.partial = True` 显式标注；
2. **Gate 用价格做风控计算时，只允许用 `ticker.last_price` 或已闭合 bar 的 `close`**，
   **禁止**用 partial bar 的 `close`（它是跳动中的瞬时值）；
3. `ingestion` 写入时用 `closed` 标记：`open_time + interval <= now` 才算闭合；
   未闭合的 bar **允许落库但标记 `closed=0`**，由增量任务在闭合后 `INSERT OR REPLACE` 覆盖。

### 2.4 全量行情快照（一次拿全市场）

实测：`GET /api/v3/market/tickers?category=SPOT` **一次返回全部 3,393 条**（无分页）。
`USDT-FUTURES` 804 条、`COIN-FUTURES` 24 条、`USDC-FUTURES` 49 条。

**4 次请求即可拿到全市场实时快照**——这是整个采集管线里最便宜也最有价值的一环。

**返回字段（已实测，全部为字符串）**：

| 字段 | 说明 |
| --- | --- |
| `symbol`, `category`, `ts` | ts 为毫秒 |
| `lastPrice`, `openPrice24h`, `highPrice24h`, `lowPrice24h` | |
| `ask1Price`, `bid1Price`, `ask1Size`, `bid1Size` | 盘口一档（可用于估算点差/滑点） |
| `price24hPcnt` | 24h 涨跌幅（小数，如 `0.01139` = +1.139%） |
| `volume24h`, `turnover24h` | 成交量 / 成交额 |
| `indexPrice` | 仅合约 |
| `markPrice` | 仅合约 |
| **`fundingRate`** | 仅合约 ← 直接可得，无需单独接口 |
| **`openInterest`** | 仅合约 ← 直接可得 |

**结论**：`DerivativesSlice` 的 funding / OI / basis **在 v1 可以零额外请求获得**
（basis 可由 `(lastPrice - indexPrice) / indexPrice` 自行算出）。

**补充接口（仅在需要资金费历史时用）**：
`GET /api/v2/mix/market/current-fund-rate?symbol=&productType=USDT-FUTURES`
→ 额外返回 `fundingRateInterval`（8 小时）、`nextUpdate`（下次结算时间）、`minFundingRate`/`maxFundingRate`。
→ **`nextUpdate` 对 Agent 很有价值**（"距下次资金费结算还有多久"），v1 值得拉一次。

### 2.5 ⚠️ 分页边界语义（实测）

```
请求: startTime=1790666100000, endTime=1790667900000, interval=15m
返回: ["1790667000000", "1790667900000"]
      ↑ 不含 start          ↑ 含 end
```

**结论**：`startTime` 是**排他**的（`>`），`endTime` 是**包含**的（`<=`）。

**对采集器的直接影响**：

| 场景 | 正确写法 |
| --- | --- |
| **增量抓取** | `startTime = 最后一根的 open_time` → 天然不重复，直接拿到更新的 bar ✅ |
| **反向回填翻页** | `endTime = 当前批最早的 open_time` → **会重复拿到那一根**，靠主键 `INSERT OR REPLACE` 去重 ✅ |
| **正向回填翻页** | `startTime = 当前批最晚的 open_time` → 天然不重复 ✅ |

**禁止**：用 `endTime = 最早 open_time - 1` 这种"手动错位"写法。用主键去重更稳（多拿一根的成本远低于漏一根）。

### 2.6 interval 枚举（实测比文档更宽）

| interval | 实测 | 备注 |
| --- | --- | --- |
| `1m` | ✅ | **只能回溯约 30 天**（见 §2.7） |
| `3m` `5m` `15m` `30m` | ✅ | |
| `1H` `4H` `6H` `12H` | ✅ | **必须大写 H**。`1h` → HTTP 400 |
| `1D` `3D` `1W` `1M` | ✅ | 文档只写到 `1D`，实际更宽 |
| `6Hutc` `1Dutc` | ✅ | UTC 对齐变体（另有 `12Hutc`/`3Dutc`/`1Wutc`/`1Mutc`，未逐一验证） |

`data/bitget/intervals.py`（**唯一映射出口，禁止散落**）：

```python
INTERVALS = ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]     # 内部规范集
INTERVAL_MS = {"1m":60_000, "5m":300_000, "15m":900_000,
               "30m":1_800_000, "1H":3_600_000, "4H":14_400_000, "1D":86_400_000}
V3_INTERVAL = {i: i for i in INTERVALS}                       # v3 直接用规范值
```

> v1 内部**只用这 7 个**。`3D/1W/1M/utc 变体`虽然可用，但引入它们会让 `INTERVAL_MS` 与渲染逻辑复杂化。
> 需要时再加（加的时候必须同步补 `INTERVAL_MS`）。

### 2.7 ⚠️ 保留窗口（实测发现）

```
1m, endTime = 30 天前   → 返回 2 根 ✅
1m, endTime = 365 天前  → 返回 0 根 ❌
1D, endTime = 2024-01-01 → 返回 2023-12-29/30/31 ✅
```

**结论**：
- **`1m` 有保留窗口限制（≈30 天）**，且**过期后不可回补**——这是不可逆的数据损失。
- `1H`/`4H`/`1D` 可回溯数年，无此问题。

**架构后果**：
1. **1m 数据必须"从今天开始持续采集"**，否则以后想用也没有。
2. 如果长期策略需要 1m，采集器必须**从第一天就跑 1m 增量**（对未来标的全量）。
3. 但 1m 全量（4,260 标的 × 1,440 根/天 = 613 万行/天）成本过高 →
   **v1 决策：只对 `focus_set` 采 1m**，并明确记录这个取舍（损失的是"事后回溯长尾标的的 1m"能力）。
4. M2a 必须**精确定位这个窗口**（33 天？30 天？45 天？），写进 `doctor` 的告警。

---

## 3. 数据量估算与分层策略

### 3.1 分层

| 层 | 覆盖 | 周期 | 用途 | 每日新增行 |
| --- | --- | --- | --- | --- |
| **Tier A** | 全部 4,260 个 | `1H` + `1D` | 全市场发现、筛选器打分、长期结构 | ~106,500 |
| **Tier A′** | 由 Tier A 派生 | `4H` | 中期结构 | **聚合生成，不额外采集** |
| **Tier B** | `focus_set`（30–60 个） | `15m` `5m` | 决策细节 | ~19,200 |
| **Tier C** | Agent 显式请求 | `1m` 或任意 | 临时深挖 | 视情况 |

合计约 **12.6 万行/天**，一年 ~4,600 万行（约 5 GB）。SQLite + 复合索引仍可承受，
但需要：① 按 `interval` 分表或分区；② 定期 `VACUUM`。**仍不引入 TimescaleDB / ClickHouse**。

#### 为什么 `4H` 不单独采集（实测依据）

Bitget 的 `1H` 与 `4H` 边界都是 **UTC epoch 对齐**的（实测 `open_time` 均为 3,600,000 / 14,400,000 的整数倍），
所以 **`4H` 可由已闭合的 `1H` 精确聚合**，不引入任何偏差。

**但 `1D` 必须原生采集**——实测 Bitget 的 `1D` bar 边界是 **UTC+8 的零点**（`open_time` 不是 86,400,000 的整数倍），
自己聚合需要处理时区偏移，容易出错。**1D 直接取交易所的原生数据。**

省下 4H 的采集量 = 每天 25,560 行、回填时 12,780 次请求（约 21 分钟）。

### 3.2 冷启动回填成本（按 10 req/s 计）

| 任务 | 请求数 | 耗时 |
| --- | --- | --- |
| Tier A · 1D · 365 天 | 4,260 × 1 | ~7 分钟 |
| Tier A · 1H · 365 天 | 4,260 × 9 | ~64 分钟 |
| Tier B · 15m · 30 天 | 50 × 3 | ~15 秒 |
| Tier B · 5m · 30 天 | 50 × 9 | ~45 秒 |
| Tier B · 1m · 30 天 | 50 × 44 | ~4 分钟 |

**冷启动总计约 75 分钟**，可接受。全部用 `INSERT OR REPLACE`，**中断可续跑**。
**建议按 category 分批跑**（先 crypto，再 stock），这样 Tier A 的 crypto 部分 ~15 分钟就能先用起来。

### 3.3 Tier 流转规则（确定性）

- 标的进入 `focus_set` → 纳入 Tier B，触发一次 `15m/5m` 回填（回溯 30 天）。
- 标的离开 `focus_set` 超 7 天 → 保留 Tier A，`15m/5m` **冻结不再增量**（不删除）。
- **有持仓或有活跃 task 关联的标的永远在 Tier B**，不参与降级。

---

## 4. 采集管线（`info-feeds` 侧）

**归属决策**：采集**不放在 runtime 内**，独立成 `info-feeds` 进程。理由：
1. 与你既有的"`info-feeds` 独立采集器 + 共享 `CCB_DB_PATH`"架构一致；
2. wake 只读 DB，**不阻塞在采集上**（对 `max_wall_seconds: 120` 的预算很关键）；
3. 采集器 7×24 跑，runtime 可以跑在任何地方。

### 4.1 四个任务

```
① universe_refresh      每天 03:00
   → 4 次 instruments 调用 → upsert instruments
   → 重算 symbol_flags（newly_listed / delisted / non_crypto / non_perpetual / status_changed）
   → 写 universe.snapshot 事件（审计"某天可交易哪些标的"）

② ticker_snapshot       每小时 1 次 + 每次 wake 前 1 次
   → 4 次 tickers 调用拿全市场（4,260 条）→ upsert ticker_snapshots
   → 成本极低，可以跑得很密

③ bars_incremental      每 1 分钟（Tier B）/ 每 5 分钟（Tier A）
   → startTime = 该 (symbol,interval) 已存的最大 open_time（排他，天然不重复）
   → 拿到未闭合 bar 也写入，标 closed=0；闭合后由下一轮 REPLACE 覆盖为 closed=1

④ bars_backfill         冷启动一次 + 每天补缺口
   → 按 §3.2 策略翻页
   → 写 bars_gap 表，后续优先补 gap
```

### 4.2 限速与重试

```python
class TokenBucket:
    def __init__(self, rate_per_sec: float = 10.0, burst: int = 10): ...
    def acquire(self, n: int = 1) -> None: ...
```

- 默认 **10 req/s**（官方 20，留 50% 余量）。
- 429 → 指数退避 `1s, 2s, 4s, 8s, 16s`，最多 5 次；仍失败 → 写 `ingestion_runs.status="failed"`，**不抛穿**。
- 超时 10s，连接错误重试 2 次。**全程串行**（不做并发），换确定性与不被封。

### 4.3 幂等

`bars` 主键 `(category, symbol, interval, open_time)`，写用 `INSERT OR REPLACE`。
→ 重复采、乱序采、中断续采结果都一致。这是可重放的基石。

### 4.4 缺口处理

期望根数 = `(t1 - t0) / INTERVAL_MS[interval]`，实际根数不等 → 记 gap。
**不做插值填充**。`chart()` 遇缺口必须如实暴露（`DataGapError`），绝不静默补齐——
否则会伪造跳空（SharpeArena 的 `data_blocks` 正是为此）。

---

## 5. 存储 schema

沿用共享库（`CCB_DB_PATH`）。**只增表，不改既有表。**

```sql
CREATE TABLE IF NOT EXISTS instruments (
  category TEXT NOT NULL, symbol TEXT NOT NULL,
  base_coin TEXT, quote_coin TEXT,
  status TEXT,               -- online / limit_open / limit_close / offline / listed
  contract_type TEXT,        -- perpetual / delivery / NULL(现货)
  symbol_type TEXT,          -- crypto / metal / stock / commodity
  is_rwa TEXT, is_reality TEXT,
  max_leverage REAL, min_leverage REAL,
  price_precision INTEGER, quantity_precision INTEGER,
  price_multiplier REAL, quantity_multiplier REAL,
  min_order_qty TEXT, max_order_qty TEXT, min_order_amount TEXT,
  maker_fee_rate REAL, taker_fee_rate REAL,
  launch_time INTEGER, fund_interval TEXT,
  buy_limit_price_ratio REAL, sell_limit_price_ratio REAL,
  tradable INTEGER NOT NULL,          -- 由 is_tradable() 预计算，避免每次扫 4591 行
  raw_json TEXT NOT NULL, refreshed_at TEXT NOT NULL,
  PRIMARY KEY (category, symbol)
);

CREATE TABLE IF NOT EXISTS bars (
  category TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,
  open_time INTEGER NOT NULL,          -- ms, bar 开始时间
  open REAL, high REAL, low REAL, close REAL,
  base_volume REAL, quote_volume REAL,
  closed INTEGER NOT NULL,             -- 1=已闭合 0=未闭合
  source TEXT NOT NULL,                -- v3_candles
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (category, symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup
  ON bars(category, symbol, interval, open_time DESC);

CREATE TABLE IF NOT EXISTS ticker_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  last_price REAL, mark_price REAL, index_price REAL,
  funding_rate REAL, open_interest REAL,
  price_24h_pcnt REAL, turnover_24h REAL, volume_24h REAL,
  high_24h REAL, low_24h REAL, bid1_price REAL, ask1_price REAL,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (category, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ticker_lookup
  ON ticker_snapshots(category, symbol, ts DESC);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, category TEXT, symbol TEXT, interval TEXT,
  range_start INTEGER, range_end INTEGER,
  request_count INTEGER, rows_written INTEGER,
  status TEXT NOT NULL, error TEXT,
  started_at TEXT NOT NULL, ended_at TEXT
);

CREATE TABLE IF NOT EXISTS bars_gap (
  category TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL,
  gap_start INTEGER NOT NULL, gap_end INTEGER NOT NULL,
  expected INTEGER NOT NULL, actual INTEGER NOT NULL,
  detected_at TEXT NOT NULL,
  PRIMARY KEY (category, symbol, interval, gap_start)
);

CREATE TABLE IF NOT EXISTS symbol_flags (
  category TEXT NOT NULL, symbol TEXT NOT NULL, flag TEXT NOT NULL,
  detail TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (category, symbol, flag)
);
-- flag: newly_listed / delisted / non_perpetual / status_changed /
--       stale_data / frozen_price / session_closed / news_source_degraded

CREATE TABLE IF NOT EXISTS session_calendar (
  asset_class TEXT NOT NULL,        -- stock / metal / commodity / crypto
  session TEXT NOT NULL,            -- regular / pre / post
  open_utc TEXT NOT NULL,           -- "13:30"
  close_utc TEXT NOT NULL,          -- "20:00"
  dst_aware INTEGER NOT NULL,       -- 1 = 随夏令时偏移
  holidays_json TEXT,               -- ["2026-11-26", ...]
  updated_at TEXT NOT NULL,
  PRIMARY KEY (asset_class, session)
);
```

**注意**：`ticker_snapshots` 主键用 `(category, symbol, ts)` 而不是自增 id——
同一时刻重复采集天然幂等。`ts` 使用交易所返回的时间戳（实测 `tickers.ts` 是毫秒字符串）。

**`session_calendar` 的必要性**：`doctor` 与 `bars_gap` 都需要判断"这个缺口是休市还是丢数据"。
没有这张表，`bars_gap` 会因为代币化股票的每日休市而天天告警 2,813 条，噪音淹没真实问题。

---

## 6. 服务给 runtime 的读接口（`BitgetDataPort`）

runtime **只读**，不写行情表。

```python
class BitgetDataPort:
    def __init__(self, db: SqliteConn, clock: Clock): ...

    def catalog(self) -> list[Instrument]              # 只返回 tradable=1，不联网
    def ticker(self, symbol_id: str) -> Ticker         # 最新一条 ts <= as_of
    def chart(self, symbol_id, interval, n) -> list[Bar]      # 只返回已闭合 bar
    def partial_bar(self, symbol_id, interval) -> Bar | None  # 未闭合，partial=True
    def derivatives(self, symbol_id) -> DerivativesSlice      # 仅 futures，来自 ticker
    def news(self, since, limit) -> list[NewsItem]            # 见 §8
```

### 6.1 lookahead 正确性（**本设计最关键的实现点**）

见 §2.3。核心一行：

```python
upper_open_time = as_of_ms - INTERVAL_MS[interval]     # 只取已闭合
```

**必须有的测试** `tests/data/test_point_in_time_bitget.py`：

| # | 输入 | 断言 |
| --- | --- | --- |
| 1 | `as_of = 09:07`，取 15m | 最后一根是 `08:45–09:00`，**不含** `09:00–09:15` |
| 2 | `as_of = 09:15`（整点刚闭合） | 最后一根是 `09:00–09:15`（应包含） |
| 3 | `as_of = 09:14:59.999` | 最后一根仍是 `08:45–09:00` |
| 4 | 数据中有缺口 | 抛 `DataGapError`，**不插值** |
| 5 | partial bar | `chart()` 不返回它；`partial_bar()` 返回且 `partial=True` |
| 6 | ticker | 只取 `ts <= as_of` 的最新一条 |
| 7 | 用 **真实 fixture** 的 v3 响应（含未闭合 bar） | 解析后 `chart()` 天然排除它 |

第 7 条最重要——用 §9 抓下来的真实响应做 fixture，这是把"实测结论"固化成回归防线。

---

## 7. 新架构点：`universe → focus_set` 漏斗

**背景**：4,260 个标的无法全量渲染进 Mandatory Context（爆上下文 + 稀释注意力 + 每 wake 都烧 token）。

```
universe（静态目录，每日刷新）     4,260 个 tradable symbol_id
        │  确定性筛选器（无 LLM，纯 Harness）—— **含交易时段感知**
        ▼
focus_set（每 wake 动态，N=50）
        │  渲染进 Mandatory Context
        ▼
   Agent 看到 focus_set
        │  可用 list_universe() 查全量目录
        │  可用 focus_on(symbol_id) 把标的拉进本轮 watch_list
        ▼
   提案（symbol_id 必须 ∈ universe，Gate 检查 session_open）
```

### 7.1 筛选器（写死在 Harness，不可被 Agent 影响）

**完整实现见 §1.4 的「① focus_set 筛选器必须交易时段感知」。** 三层候选池：

```python
class FocusSelector:
    def select(self, inputs, n: int = 50) -> list[str]:
        forced = held_symbols | task_related_symbols | watchlist_symbols   # 永不淘汰。

        # 分层：crypto（24/7） → 交易中的非 24/7 → 休市中的非 24/7
        pool_247          = [s for s in candidates if is_247(s)]
        pool_session_open = [s for s in candidates if not is_247(s) and in_session(s, now)]
        # 休市标的**不参与** focus_set 竞争（除非在 forced 里），只进上下文并标注「休市中」

        score = (0.5 * rank(turnover_24h)
               + 0.3 * rank(abs(price_24h_pcnt))
               + 0.2 * rank(abs(vol_zscore_24h)))
        # 排序键必须完全确定
        key = lambda s: (-score[s], -turnover_24h[s], s)

        picked = list(forced)
        for pool in (pool_247, pool_session_open):          # crypto 优先占位
            for s in sorted(pool, key=key):
                if len(picked) >= n: break
                if s not in picked: picked.append(s)
        return picked
```

**为什么必须在 Harness 里**：如果让 LLM 决定"我要看哪些标的"，就违背了整份设计的核心意图——
"**Harness 决定你睁眼看到什么世界，Agent 决定接下来怎么想**"。筛选器就是"睁眼"这一步的一部分。

**为什么必须时段感知**：代币化股票头部标的的 24h 成交额（$22 B）是 BTC 现货（$374 M）的 60 倍。
不做时段隔离，凌晨 3 点唤醒时 focus_set 会被**价格冻结、无法成交**的股票代币占满，
Agent 会对着 50 个不能交易的标的做研究——这比不看更糟（浪费预算 + 产生幻觉性判断）。

### 7.2 新增两个工具

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `list_universe` | `category?`, `quote_coin?`, `min_turnover_24h_usd?`, `limit≤100` | 查全量目录 |
| `focus_on` | `symbol_id`，≤5 个/wake | 加入本轮 watch_list，触发 Tier B 数据可用 |

### 7.3 Gate 新增检查项

`universe_member` 改为检查 `symbol_id ∈ instruments(tradable=1)`。
若 `symbol_id` **不在**本次 `focus_set` 内，**仍可放行**（universe 是全量目录），
但在 `GateDecision` 里记 `outside_focus_set: true` 供审计
（用于发现"Agent 老去长尾搏杀"这类行为模式）。

---

## 8. 新闻源（v1 最小可行方案）

Bitget 不提供新闻。而项目硬约束是"任何交易策略在决策前必须至少调用一次外部信息源"。

| 优先级 | 源 | 可得性 |
| --- | --- | --- |
| P0 | Bitget 公告页（上币/下架/维护/调整杠杆） | 需自建适配器 |
| P0 | 主流加密媒体 RSS | 需自建适配器 |
| P1 | 社交情绪 | ⚠️ 历史上有 IP 封禁（403）教训，需 `source_health` 监控 |

**契约不变**：一律转成 `NewsItem(news_id, published_at, source, title, summary, symbols)`，
且 **`published_at <= as_of` 硬过滤**。

**健康度**：每源写 `source_health`（成功率 / 最后成功时间 / 连续失败数），
连续失败 → 打 `news_source_degraded` 标 + Gate 告警。
**v1 不允许因新闻源不可用而放行交易**——只允许"拒单 + 告警"。

---

## 9. Fixture 固化（唯一允许打真网络的地方）

第二次实测脚本抓下来的响应，**必须立刻存成离线 fixture**，之后 CI 全部离线跑：

```
tests/fixtures/bitget/
  instruments_SPOT.json
  instruments_USDT-FUTURES.json
  instruments_USDC-FUTURES.json
  instruments_COIN-FUTURES.json
  tickers_SPOT.json
  tickers_USDT-FUTURES.json
  candles_v3_SPOT_BTCUSDT_15m_with_partial.json     ← 含未闭合 bar，最关键
  candles_v3_USDT-FUTURES_BTCUSDT_15m.json
  candles_v3_SPOT_BTCUSDT_1D_deep_history.json      ← 2023-12-29 那批
  candles_v3_COIN-FUTURES_BTCUSD_CM_15m.json
  error_v3_candles_interval_1h_lowercase_400.json   ← 错误语义也要固化成契约
  error_v3_candles_limit_1001_400.json
```

命令：
```bash
python -m pytest tests/data/test_bitget_contract.py --record-fixtures -q   # 只跑一次
python -m pytest tests/data -q                                            # 之后全离线
```
`conftest.py` 里加 `autouse` fixture 拦截真实网络，任何真请求 → 测试失败。

---

## 10. 对 `PLAN.md` 的影响

| PLAN.md 位置 | 原本 | 改为 |
| --- | --- | --- |
| §1 v1 范围 | 未限定市场 | **Bitget 现货 + 永续，全资产类别，4,260 个**；交易时段感知 |
| §3.2 目录 | `data/live.py` | 拆为 `data/bitget/{client,catalog,bars,tickers,intervals,symbols}.py` + 独立 `info-feeds` |
| §4 契约 | `symbol: str` | `symbol_id: str`（`{category}:{symbol}`）全链路 |
| §6.2 数据平面 | 通用 DataPort | 见本文 §6；新增 §7 的 `focus_set` 漏斗 |
| §6.5.1 上下文 | 渲染 universe 全量 | 渲染 `focus_set`（30–60）+ "全量目录可查"入口 |
| §6.6 执行平面 | `BrokerPort` + 下单 | **`InstructionEmitter`**，只输出指令 |
| §11 陷阱 | — | 新增：未闭合 bar / 同名跨 category / 1m 保留窗 / 缺口插值 |
| M2 | 单个里程碑 | 拆成 **M2a 采集器** + **M2b BitgetDataPort** |

---

## 11. 执行平面改为 `InstructionEmitter`（v1）

不下真实订单，但**必须保留闸门**——否则风控就是空谈。

```python
class Instruction(BaseModel):
    instruction_id: str
    wake_id: str
    created_at: datetime
    action: Literal["open", "close", "modify"]
    symbol_id: str                      # 如 "USDT-FUTURES:BTCUSDT"
    side: Side
    size: SizeIntent
    leverage: float | None
    order_type: Literal["market"]
    reference_price: float              # 生成时的 ticker.last_price
    thesis: str
    invalidation: list[InvalidationPredicate]
    hard_stop: float
    take_profit: float | None
    time_stop_hours: int | None
    confidence: float
    gate_decision: GateDecision         # 必须携带，证明过了闸门
    status: Literal["emitted", "shadow_filled", "expired", "cancelled"]
```

**三条输出通道（都要有）**
1. DB `instructions` 表（权威、可审计）
2. `~/.ccb-runtime/instructions.jsonl`（append-only，供外部程序 tail）
3. `traderuntime instructions --since <ts> [--json]`

**真实下单在 v1 的形态**：`LiveBroker.submit()` 只 `raise NotImplementedError`，
或 `execution.mode == "dry_run"` 时写日志不发单。

### 11.1 影子账本（Shadow Ledger）—— ⚠️ 待你确认

**问题**：如果只输出指令、不维护持仓，那么 `Position 生命周期 / Watchdog / 记忆闭环 / 反思`
**整条链路全部失效**——系统退化成"信号打印机"，而不是"AI 基金经理"。

**建议方案**：指令发出后，由 Harness 用 `as_of` 的 `ticker.last_price` 记一次**影子成交**，
持仓进入正常的 `Position` 生命周期，PnL 用市场价逐 wake 更新。
**不接触任何真实资金与 API Key**，纯本地记账。

这样 Watchdog 能触发、thesis 能被检查、平仓后能写 `outcome`/`reflection`、记忆能闭环。

### 11.2 ⚠️ 现货做空的建模问题

现货**无法做空**（除非走 MARGIN，而 v1 不纳入）。所以：
- `SPOT:*` 的提案只允许 `side=long`；
- Gate 新增一条 `venue_side_supported`：`SPOT` + `short` → 拒绝；
- 做空只能通过 `USDT-FUTURES` / `USDC-FUTURES` / `COIN-FUTURES`。

这一条必须写进宪法（`forbidden`）与 Gate，否则会产出物理上无法执行的指令。

---

## 12. 网络出口（已解决）

本机首轮实测（未开 VPN）：`api.bitget.com` TLS 握手被重置（`WinError 10054`），
`baidu.com` 200，`api.binance.com` / `api.bybit.com` 均 000 → 典型的 SNI 阻断。
开 VPN 后全部接口 200 可达。

| 方案 | 做法 | 适用 |
| --- | --- | --- |
| **A. 本机走代理（当前阶段）** | VPN/Clash 常开，`info-feeds` 读 `HTTPS_PROXY` | 用户决定：**先用本机把框架跑通** |
| **B. 采集器境外部署（目标形态）** | `info-feeds` 放境外小机器，写回共享 DB | 用户决定：**框架跑通后迁过去** |
| C. 换数据源 | 走别的可达交易所 | ❌ 成交价与标的必须与 Bitget 一致 |

> **用户决策（已确认）**：**最终上境外服务器，但先在本机把框架做出来能跑。**
> 因此代码必须做到：**采集器的运行位置只由配置决定**，迁移时只改 `runtime.yaml`，不改一行代码。

**为此必须满足的三个约束**：

1. **`base_url` 与 `proxy` 全部走配置**，不硬编码、不读死环境变量（`proxy: ${HTTPS_PROXY}` 由配置层展开）。
2. **采集器不依赖本机路径**：DB 路径由 `CCB_DB_PATH` 决定；trace/fixture 目录由配置决定。
3. **采集器与 runtime 之间只通过 DB 交互**，不通过本地文件、不通过本地 socket。
   → 将来把 `info-feeds` 放境外时，只需解决 DB 同步（或让 runtime 直接读远端 DB），
      **runtime 侧零改动**。

**配置项**：
```yaml
data:
  bitget:
    base_url: https://api.bitget.com
    proxy: ${HTTPS_PROXY}          # 空 = 直连（境外部署时留空）
    timeout_seconds: 10
    rate_limit_per_sec: 10
    retry: { max_attempts: 5, backoff_base: 1.0 }
```

> **给执行者的提醒**：`info-feeds` 必须正确处理"代理掉了"这种情况——
> 表现为大批 `URLError` / `SSLError` / `ConnectionReset`（本机实测首轮就是这个现象，`WinError 10054`）。
> 此时应**降级为告警 + 停止采集**，由 `doctor` 报告"数据新鲜度超标"，
> 让 runtime 的 Gate 因 `data_fresh` 失败而拒单。
> **绝不允许**用旧数据假装新数据。
>
> 另注：本机实测 `curl.exe` 走 VPN 时返回 `000`（Schannel/SNI 问题），
> 而 Python `urllib` 正常。**采集器统一用 Python 的 httpx，不要用 shell 调 curl。**

---

## 13. 验收命令（数据侧）

```bash
# ① 目录刷新
python -m infofeeds refresh-universe --config config/runtime.yaml

# ② 冷启动回填
python -m infofeeds backfill --tier A --interval 1D,4H,1H --days 365
python -m infofeeds backfill --tier B --interval 15m,5m --days 30

# ③ 增量
python -m infofeeds incremental --once

# ④ 体检
python -m infofeeds doctor --config config/runtime.yaml

# ⑤ 契约 fixture（唯一允许打真网络，且只跑一次）
python -m pytest tests/data/test_bitget_contract.py --record-fixtures -q
```

**`doctor` 输出要求**
- 各 category：总标的 / tradable 数 / 各 `symbolType`（`asset_class`）分布
- 各 interval：覆盖区间、覆盖标的数、根数、**最近一根是否为已闭合 bar**
- 缺口清单（`bars_gap` 条数 + 抽样），**并区分「休市 gap」与「真缺口」**
- **交易时段检查**：每个 `asset_class` 当前 `session` 状态；休市标的的 `frozen_price` 计数
- `ticker_snapshots` 最新 `ts` 距今秒数
- 1m 数据最早时间 → **距 30 天窗口还剩几天**（周知会丢失）
- 各新闻源健康度

**`doctor` 验收标准**
- SPOT tradable = **3,387**、USDT-FUTURES = **804**、USDC-FUTURES = **49**、COIN-FUTURES = **20**（±10，允许交易所上下架）
- 按 `asset_class` 拆分：SPOT 下 crypto **578** / stock **2,813** / metal **2**
- Tier A 覆盖率 ≥ 99%（排除停牌/新上线）
- 抽查 100 个 `(symbol, interval)`：**无一根未闭合 bar 混入 `closed=1`**
- **`bars_gap` 中不含休市窗口内的缺口**（用 `session_calendar` 交叉验证）
- **交易时段判定正确**：UTC 13:30–20:00 时 `stock` 类 `session=open`，UTC 03:00 时 `session=closed`
- `ticker_snapshots` 最新 `ts` 距今 < 120s

---

## 14. 里程碑调整

`PLAN.md` 的 M2 拆成两个：

| 里程碑 | 内容 | 依赖网络 |
| --- | --- | --- |
| **M2a 采集器（`info-feeds`）** | universe_refresh / ticker_snapshot / bars 回填 / 增量 / 令牌桶 / `doctor`；§9 的 fixture 固化 | ✅ 需要 |
| **M2b `BitgetDataPort`（runtime 侧）** | 只读接口 + `focus_set` 筛选器 + §6.1 的七条 point-in-time 测试 | ❌ 全离线 |

**风险缓解**：M2a 是**唯一需要网络**的里程碑。
若网络出口不稳，可先用 §9 的离线 fixture 把 M2b 及后续里程碑**全部打通**，
最后再接回真实采集。**建议就这么做**——不要让网络阻塞主线开发。
