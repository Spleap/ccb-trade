# 文本信息源接入指南

> 本文档回答一个问题：**每一个文本信息源，要接到 `info-feeds` 里，需要什么条件、要不要花钱、坑在哪。**
>
> 范围：新闻、社媒情绪、交易所/项目公告、预测市场，以及配套的行情/衍生品信号。
> 不含 K 线（K 线走 Bitget，见另一条线）。

状态标记：**当前决策 = 只做档 0；CoinGlass 暂不买。档 1 / 档 2 保留为后续可选项。** 档 0 的接线**已于 2026-09-27 落地**（详见 §8 第一步）：Google News 补进了 `get_news` / `get_global_news` 的厂商链，新增了 `get_sentiment_index`（Alternative.me F&G）与 `get_sentiment`（Reddit / StockTwits），Telegram 预览作为不经路由的独立取数模块。

> ⚠️ 本文档的 §2.1 / §2.2 / §7 已按 **2026-09-27 实测**更正；凡标"实测"的行，以实测为准，原调研口径已被推翻。

---

## §0 分档说明

| 档 | 含义 | 行动 |
|---|---|---|
| **档 0** | 完全免 key，今天就能接 | 无需你提供任何东西 |
| **档 1** | 免费，但要注册拿 key | 需要你花几分钟注册 |
| **档 2** | 付费 | 需要你拍板预算 |
| **档 D** | 2026 年已下线，别踩 | 仅作记录 |

---

## §1 总览表

| 源 | 档 | 补什么缺口 | key | 价格 | 有历史归档？ | 落地难度 |
|---|---|---|---|---|---|---|
| **cryptocurrency.cv** | 0 | 加密原生新闻、中文源、历史 | 无 | 免费 | 有（2017-09 ~ 2025-02，66 万篇）—— **但免 key 取不到，见 §2.1** | 低 |
| **Google News RSS** | 0 | 通用新闻兜底 | 无 | 免费 | 无 | 低 |
| **Alternative.me F&G** | 0 | 市场级情绪指数 | 无 | 免费 | 有 | 低 |
| **Telegram 公开预览** | 0 | 项目方/交易所频道一手消息 | 无 | 免费 | 无 | 中 |
| Reddit / StockTwits / Polymarket / Yahoo | 0 | 已在库 | 无 | 免费 | 无 | 低（仅需补挂路由） |
| **FRED** | 1 | 宏观数据 | `FRED_API_KEY` | 免费 | 有 | 低（已有实现，缺 key） |
| **Alpha Vantage** | 1 | 带情绪打分的新闻 | `ALPHA_VANTAGE_API_KEY` | 免费（额度小） | 有 | 低（已有实现，缺 key） |
| **CoinGecko Demo** | 1 | 行情兜底 | `COINGECKO_API_KEY` | 免费 | 1 年 | 低 |
| **CoinMarketCap Basic** | 1 | 行情兜底（另有免注册 public 端点） | `CMC_API_KEY` | 免费 | **无** | 低 |
| **Telegram MTProto** | 1 | 读频道全量 | `TELEGRAM_API_ID` / `API_HASH` | 免费 | 无 | 中 |
| **Santiment** | 1 | 链上 + 社媒 | 无（未鉴权） | 免费 1,000 calls/月 | 1 年（30 天滞后） | 中 |
| CryptoPanic | 2 | 加密新闻 + 情绪投票 | `auth_token` | 免费层已下线 | 有限 | 不建议 |
| **CoinGlass** | 2 | 衍生品（OI/资金费率/清算/多空比/ETF） | `COINGLASS_API_KEY` | $29 ~ $699/月 | 按套餐 | 中 |
| **LunarCrush** | 2 | 专业社媒情绪 | Bearer token | $90 ~ $900/月 | 有 | 低（有 MCP） |
| X (Twitter) | 2 | 社媒一手 | OAuth | 无免费读层 | 有限 | 不建议 |
| CryptoPanic 免费层 | D | — | — | 2026-04-01 下线 | — | — |
| CoinDesk Data 免费层 | D | — | — | 2026-05-21 退役 | — | — |

---

## §2 档 0：完全免 key

### §2.1 cryptocurrency.cv —— 本批最重要的一个

**它补的缺口**：加密原生新闻站（CoinDesk / The Block / Decrypt / Cointelegraph / Blockworks / Messari / Bankless …）+ 中文源（8BTC、金色财经、Odaily、PANews、BlockBeats、吴说等 10 家）+ 历史归档。

**接入条件：无。** 不需要注册，不需要任何 header：

```
curl https://cryptocurrency.cv/api/news
```

**关键端点**（对 LLM 友好的 JSON）：

| 端点 | 用途 | 免 key 实测（2026-09-27） |
|---|---|---|
| `/api/news` | 最新新闻；参数 `limit`(1-100)、`source`、`category`、`lang` | ✅ 可用。**但每次最多 3 条**（无视 `limit`）；**无按标的过滤参数**（`ticker` 被忽略）；`lang` 只认 `en\|es\|fr\|de\|it\|pt\|ru\|ja\|ko\|zh\|ar`（`zh-CN` 报 400）。`category` 可用，`general` 里有中文源（BlockTempo） |
| `/api/search?q=&from=&to=` | 按日期区间全文搜索 | ❌ **HTTP 402**（x402 微支付） |
| `/api/archive?date=2024-01` | 历史归档按月取；也可 `?ticker=BTC`、`?q=` | ❌ 接受 `start_date`/`end_date`/`ticker` 但**恒返回 `count: 0`**，包括其宣称归档期内的区间 |
| `/api/breaking` | 近 2 小时突发 | ❌ **HTTP 402** |
| `/api/fear-greed` | 恐惧贪婪指数（可替代 Alternative.me） | 未实测（改用 alternative.me 原始源） |
| `/api/narratives` | 当前市场叙事 | ❌ **HTTP 402** |
| `/api/sentiment` | 情绪分析 | 未实测 |
| `/api/sources` | 全部源及状态 | 未实测 |

> ⚠️ **实测推翻上文口径**：原本以为它是"本批唯一能按日期窗口取历史"的源，但 2026-09-27 探测显示 —— **付费墙（x402）已挡在 `/search`、`/breaking`、`/narratives` 前面，`/archive` 恒空**。因此免 key 下它**只能服务"最新 3 条"**，历史窗口必须 `withhold`。
> 落地实现（`vendors/cryptocurrency_cv.py`）据此只服务"窗口覆盖到今日"，且**刻意不注册给 `get_news`** —— 它没有按标的过滤的能力，挂在 `get_news` 上会给出"市场标题却假装是标的相关"。

**限额**：官方口径是 fair-use，无硬性上限；但对端有服务端缓存，**没必要高于每 30 秒一次**。生产建议按 IP 自建缓存。

**归档深度**：662,047 篇，覆盖 2017-09 ~ 2025-02，英文 + 中文，100+ 源。其中 CryptoPanic 贡献 346,031 篇、Odaily 贡献 316,016 篇中文。

**防前视**：~~★★★★★ 这是本批**唯一能按日期窗口正常取历史**的源~~ → **实测为零**（见上表：`/search` 402、`/archive` 恒空）。历史窗口一律 `withhold`。归档量大但免 key 取不出来，等于没有。

**要留意的**：
- 它是**聚合器**，内容是转载，不是原创报道；关键信息仍应回原站核对。
- 由第三方个人开源项目运营（MIT / source-available）。生产环境的可用性依赖它，建议**自部署兜底**（支持 Vercel / Docker / K8s）。
- 提供 MCP server 与 Python SDK，若要给子 Agent 直接用工具可以省一层封装。

---

### §2.2 Google News RSS

**它补的缺口**：通用新闻兜底（非加密专属）。

```
https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en
```

支持 `site:`、`when:1d`、`OR`、引号等修饰符。

**实测要点（2026-09-27）**：`hl=zh-CN&gl=CN&ceid=CN:zh` 这个 locale **必须配中文查询词**才拿得到中文源 —— 拿英文词（`BTC`）去查 zh-CN，返回的仍是英文结果。落地实现里因此带了一张中文别名表（`BTC→比特币`、`ETH→以太坊`…），`get_news` 会**对同一标的一共查两次（en-US 一次、zh-CN 一次并换成中文词）**，这样一次调用就能同时返回英文源和中文源（`google_news_rss.py` 的 `_ZH_NAMES`）。

**限制（决定它只能做兜底）**：
- 单次约 100 条上限
- 返回的链接是 **Google 重定向**，不是原文 URL
- **没有摘要、没有缩略图**，只有标题 + 来源 + 时间
- 中位条目年龄约 6.6 天 —— 新鲜度差，做不了快讯

**防前视**：无归档 → 历史窗口一律 `withhold`。

---

### §2.3 Alternative.me Fear & Greed

**它补的缺口**：市场级情绪指数（现在是 0）。

```
https://api.alternative.me/fng/?limit=30
```

- 鉴权：无；限额：宽松
- 历史：`limit=N` 可取回过去 N 天 —— **这是本批第二稀有的能力**
- 实测（2026-09-27）：正常；`limit=N` 生效；返回体里 `timestamp` 是**纪元秒**（不是毫秒），解析时别搞错
- 注意：cryptocurrency.cv 也有 `/api/fear-greed`。若已接 c.v 可以不接这个；但 alternative.me 是**原始数据源**，链路更短更稳，建议保留。

---

### §2.4 Telegram 公开频道网页预览

**它补的缺口**：项目方 / 交易所官方频道的一手消息（上币、维护、快照）。

```
https://t.me/s/{channel}
```

- 免登录，返回 HTML，约**最近 20 条**
- 实测（2026-09-27）：`t.me/s/{channel}` 正常，约 20 条/页；解析锚点是 `data-post="…"` 与 `<time datetime="…">`
- 没有官方 API，靠页面解析；可能被限速
- 要读全量或非公开频道 → 必须走档 1 的 MTProto
- 落地实现（`vendors/telegram_preview.py`）**不经路由**：它的入参是**频道名列表**而不是 ticker，没有可挂的方法名，需要直接调用 `fetch_telegram_messages(channels, …)`

**防前视**：无归档 → `withhold`。

---

### §2.5 复用的现有四路

这四路已在库里，**不需要新接，只需要修正**：

| 文件 | 状态 | 要修的地方 |
|---|---|---|
| `vendors/yahoo/news.py` | 可用 | 无 |
| `vendors/reddit.py` | 可用 | ~~不在 `router.py` 路由表里~~ → **已补挂**为 `get_sentiment` 的厂商 |
| `vendors/stocktwits.py` | 可用 | ~~不在 `router.py` 路由表里~~ → **已补挂**为 `get_sentiment` 的厂商 |
| `vendors/polymarket.py` | 可用 | 已有正确的 `withhold` 逻辑，照抄即可 |

> StockTwits 是本批唯一有**现成 Bullish/Bearish 标签**的源，别浪费。
> Reddit / StockTwits 的 `fetch_*` 都是 `(ticker, …, start_date, end_date)` 形态，所以直接挂成 `get_sentiment` 的厂商，无需适配层（`router.py` 里注明）。
>
> **实跑补充（2026-09-27，采集进程首轮）**：`reddit` 通了但**频繁 429**——一轮 3 个标的里 2 个触发退避重试，单轮被拖到约 2.5 min；`stocktwits` 对本机 IP **恒 403**（换浏览器 UA 同样 403，疑为 IP 级封禁），目前该源在 `source_health` 里恒为失败态。这两点都在 `ARCHITECTURE.md` §12.6 留了待办。

---

## §3 档 1：免费，但要注册拿 key

### §3.1 FRED —— 已有实现，只差 key

- 注册：<https://fred.stlouisfed.org/docs/api/api_key.html>
- 变量名：`FRED_API_KEY`
- 费用：免费，提交即发 key
- 现状：`vendors/fred.py` 已写好，`default_config.py` 里 `macro_data` 类别已指向 `fred`。**没有 key 就跑不动。**

### §3.2 Alpha Vantage —— 已有实现，只差 key

- 注册：<https://www.alphavantage.co/support/#api-key>
- 变量名：`ALPHA_VANTAGE_API_KEY`
- 费用：免费但**每日额度很小**（具体数值以官网为准，历史上是 25 次/日）
- 价值：**唯一自带 sentiment score 的新闻源**（`vendors/alpha_vantage/news.py`），且返回原始 JSON，与 Yahoo 的 Markdown 形态不一致，接的时候要做归一化。
- 注意：额度小 ⇒ 只适合当"重点标的的精读源"，不适合全市场扫。

### §3.3 CoinGecko Demo

- 注册：<https://www.coingecko.com/en/api>
- 变量名：`COINGECKO_API_KEY`
- 免费额度：**10,000 calls/月，100 calls/min**，1 年历史，**非商用**
- 计价：1 次成功调用 = 1 credit，与返回体量无关（简单）
- 鉴权：Demo key 走 `demo_api_key` 参数（不是 `api_key`，后者指向付费端点）
- 加分项：有 MCP
- 付费升级：Basic $35/月（年付 $29），100,000 calls、300/min、商用许可

### §3.4 CoinMarketCap Basic

- 注册：<https://pro.coinmarketcap.com>
- 变量名：`CMC_API_KEY`
- 免费额度：**15,000 credits/月，50 req/min**
- 鉴权：`X-CMC_PRO_API_KEY` header，Base `https://pro-api.coinmarketcap.com`
- **三条硬限制**：仅个人用途、**不含历史数据**、无商用许可
- 计价坑：credit 不是 1:1 —— 返回条数、数据点、币种换算都会增耗 credit，成本不好估
- **另一个入口（免注册）**：它还有一个 keyless public API，40+ 端点、无需账号无需 header，适合快速验证。**如果只是拿行情兜底，这个免注册端点可能就够了，可以不注册。**
- 背景：CMC 2020 年被 Binance 收购 —— 你在做交易系统，独立性上要心里有数。

### §3.5 Telegram MTProto（要读频道全量才需要）

- 注册：<https://my.telegram.org> → API development tools
- 变量名：`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
- 为什么必须单独说：**Bot API 读不了自己不是管理员的频道**。要读频道/群，只能走 MTProto（Telethon / Pyrogram）。
- **风险**：这是 userbot 路径，用的是你的个人账号凭据。**强烈建议用小号**，不要用主号。
- 结论：档 0 的 `t.me/s/` 预览够用的话，先别开这个。

### §3.6 Santiment（免费可先试）

- 免费额度：**1,000 calls/月，100/min，未鉴权即可调用**
- 历史：1 年，但有 **30 天滞后**
- 付费：Pro $49/月（5,000 calls）、Max $249/月
- 定位：链上 + 社媒，Free 层只能做研究，做不了实时信号

---

## §4 档 2：付费

### §4.1 CryptoPanic —— 不建议

**2026 年的关键变化**：免费 Developer API 层已下线（2026-04-01）。现在程序化访问需要付费计划，**具体档位和价格以 developers/api/plans 为准**。

**一个常见误解**：它那个 `$49/年` 的 **PLUS 订阅不是 API 计划**，官方页面明确写了"PLUS does not provide access to the CryptoPanic API"。别买错。

**接口形态（若将来真要用）**：

```
GET https://cryptopanic.com/api/v1/posts/?auth_token=XXX
    &filter=rising|hot|bullish|bearish|important
    &currencies=BTC,ETH
    &regions=en,zh
    &format=rss
```

- 限速 5 req/sec（部分计划 10）
- 服务端有缓存，**高于每 30 秒一次没意义**
- 情绪过滤标签（bullish/bearish/important）确实是它的特色，但~~加密货币.cv 的归档本身就含 CryptoPanic 数据，等于白拿了它 34 万篇。~~ **→ 该理由已失效**：cryptocurrency.cv 的归档免 key 取不出来（§2.1 实测），那 34 万篇 CryptoPanic 数据也一并拿不到

**结论：暂时都不接。** 原本的结论是"用 cryptocurrency.cv 替代 CryptoPanic"，但实测显示加密货币.cv 的免 key 检索层也被 x402 挡住了，所以现在两条路都不可用 —— 那就先都不买，靠 Google News 兜底；等归因显示"确实缺带情绪标签的加密新闻"再回来评估。

`$29-299/月` 这个区间在多个二手评测里出现过，但**官方页面未确认**，不要当依据。

### §4.2 CoinGlass —— 暂不买（已决策）

**没有免费 API 层。**

| 计划 | 月付 | 端点 | 限速 | 商业使用 |
|---|---|---|---|---|
| Hobbyist | $29 | 80+ | 30 次/分 | ✗ 仅个人 |
| Startup | $79 | 130+ | 80 次/分 | ✗ 仅个人 |
| Standard | $299 | 150+ | 300 次/分 | ✓ |
| Professional | $699 | 160+ | 1,200 次/分 | ✓ |
| Enterprise | 定制 | 定制 | 定制 | ✓ |

**四个必须知道的坑**：

1. **商用门槛是 Standard $299**，不是 $29。Hobbyist / Startup 明确限定"personal use only"。一旦数据进入你对外提供的产品，$29 那个就违约了。
2. **年付不省钱** —— 宣传的"省 $72/$192/$960"是对着一个从不实际收取的挂牌价算的；标准档年付 $3,588 正好等于 $299 × 12。
3. **限速是"每分钟"不是"每天"** —— 低频策略用 Hobbyist 就够，高频会瞬间打满。
4. **历史粒度按档次分层** —— 尤其 **1min 粒度数据只有 Standard 起才有**（Standard 6 天、Professional 12 天）。Hobbyist 最高频只到 4h（180 天）。

**鉴权**：Base `https://open-api-v4.coinglass.com`，认证头 `CG-API-KEY`（V4 起；旧版 `coinglassSecret` 已废弃）。

**覆盖**：30+ 交易所，**含 Bitget**（与你 K 线源同所，这点有价值）。

**结论**：暂不买。等子策略的盈亏归因里**确实显示出"缺 OI / 资金费率 / 清算信号"造成了可量化的损失**，再上 Standard $299。

### §4.3 LunarCrush

| 计划 | 月付 | 年付折算 |
|---|---|---|
| Individual | $90 | $72 |
| Builder | $300 | $240 |
| Scale | $900 | $720 |
| Enterprise | 定制 | — |

- Base `https://lunarcrush.ai`，鉴权 `Authorization: Bearer <API_KEY>`
- **默认返回 Markdown**（`?format=json` / `?format=csv` 可换）—— 对喂 LLM 极其友好
- 有 MCP：`https://lunarcrush.ai/mcp`
- 覆盖：X、Reddit、YouTube、TikTok、Instagram、News；Galaxy Score™ / AltRank™ 是它的招牌指标

**注意**：网上能看到 `$24/月` 之类的旧价格，官方定价页现在是 $90/月 起，以官网为准。

**结论**：后续若要做专业社媒情绪再上，先从 Individual 试。

### §4.4 X (Twitter) —— 不接官方

- **已无免费读取层**：免费层只剩写入（1,500 posts/月，读取为 0）
- Basic $200/月已**关闭新注册**
- 现为 pay-per-use：读 1 条 $0.005、profile $0.01、发帖 $0.015、含 URL 发帖 $0.20；月读上限 300 万条

**第三方替代**：TwitterAPI.io（约 $0.00015/次）、Xpoz（免费一次性 75,000 条，Pro $20/月）。

**结论**：不接官方。要么走第三方，要么不做 —— 社媒情绪的边际信息量，LunarCrush 已经帮忙聚合了。

### §4.5 其他可选项（记录用，不建议现在上）

| 源 | 免费层 | 首个付费档 | 备注 |
|---|---|---|---|
| CoinStats | 20,000 credits/月 | $49/月 | 行情+钱包+组合，有 MCP |
| Messari | 月度不限量（200/min 限速） | $30/月 | 额度慷慨但需核实当前条款 |
| Nansen | 100 试用 credits + 每日 10 | $49/月 | 链上，有 MCP |
| Glassnode | **无 API 免费层** | $49/月 | 免费仅限 Studio 网页 |
| Coinpaprika | 1,000 次/日，**免 key** | — | 最省事的兜底 |
| GoldRush | 14 天试用 | $10/月 | 多链 |

---

## §5 档 D：2026 已下线，别踩

| 源 | 下线时间 | 影响 |
|---|---|---|
| CryptoPanic 免费 Developer API | 2026-04-01 | 程序化访问需付费 |
| CoinDesk Data 免费 API 层 | 2026-05-21 | 旧 CryptoCompare 免费层一并消失 |
| X (Twitter) 免费读取层 | — | 免费层现在只有写 |
| cryptocurrency.cv 免 key 检索层 | 2026-09-27（本次实测） | `/search`、`/breaking`、`/narratives` 变成 **x402 微支付**；`/archive` 恒空。它从"唯一可回测的文本源"退化成"只能给最新 3 条" |

**趋势判断**：免费行情/新闻 API 的免费层在系统性收缩（连本批最被看好的 cryptocurrency.cv 都在本次实测中确认已被 x402 挡住检索层）。所以 §2 里那些**免 key 且可自部署**的源价值更高 —— 自部署是唯一不受对方定价变更影响的路径。

---

## §6 你要给我的东西（环境变量清单）

| 变量名 | 档 | 去哪拿 | 免费额度 | 现在需要吗 |
|---|---|---|---|---|
| —— | 0 | 无 | —— | **不需要，档 0 全部免 key** |
| `FRED_API_KEY` | 1 | <https://fred.stlouisfed.org/docs/api/api_key.html> | 免费 | 想跑宏观就需要 |
| `ALPHA_VANTAGE_API_KEY` | 1 | <https://www.alphavantage.co/support/#api-key> | 额度小 | 想要情绪打分的新闻就需要 |
| `COINGECKO_API_KEY` | 1 | <https://www.coingecko.com/en/api> | 10,000/月 | 可选 |
| `CMC_API_KEY` | 1 | <https://pro.coinmarketcap.com> | 15,000/月 | 可选（另有免注册端点） |
| `TELEGRAM_API_ID` / `API_HASH` | 1 | <https://my.telegram.org> | 免费 | 要读频道全量才需要 |
| `COINGLASS_API_KEY` | 2 | <https://www.coinglass.com/pricing> | 无免费层 | **已决策：暂不买** |
| `LUNARCRUSH_API_KEY` | 2 | <https://lunarcrush.com/pricing> | 免费层仅行情 | 后续 |
| `CRYPTOPANIC_AUTH_TOKEN` | 2 | <https://cryptopanic.com/developers/api/plans> | 已下线 | **不建议** |

---

## §7 防前视处理矩阵（接线前必须先定这张表）

**没定这张表就去回测，结果全是假的。**

| 源 | 有历史归档 | 历史窗口策略 | 理由 |
|---|---|---|---|
| cryptocurrency.cv | **免 key 取不到** | `withhold` | `/search` 被 402 挡住、`/archive` 恒空（§2.1 实测），只能给最新 3 条 |
| Google News RSS | 无 | `withhold`（已实现，**抛 `NoMarketDataError`**） | 只服务"当前"，无 vintage |
| Alternative.me F&G | 有 | 取回后按日期过滤（已实现） | 序列本身带日期 |
| Telegram 预览 | 无 | `withhold`（返回说明文字） | 只返回最近 20 条 |
| Polymarket | 无 | `withhold`（已实现） | 官方口径：只有实时赔率 |
| Reddit | 无 | `in_window` 已实现 | 只能搜近 7 天 |
| StockTwits | 无 | `in_window` 已实现 | 只服务最新 30 条 |
| Alpha Vantage 新闻 | 有 | 按 `time_from`/`time_to` 取 | 已实现 |
| CoinGlass | 有（按套餐） | 按时间戳区间取 | 付费后可用 |

**配套三条铁律**（沿用 info-feeds 现有约定）：

1. **绝不返回空字符串** —— 抓取失败一律返回明确哨兵文案（如 `DATA_UNAVAILABLE: ...`），防 LLM 编造。
2. **区分两种"没有"** —— 用 `coverage_gap` 区分"确实没这条新闻"和"这个源没覆盖"，两者对策略的含义不同。
3. **链成员"没数据"要 `raise`，不要 `return`** —— 路由在第一个 `return` 的厂商处停下。链成员返回一段说明会**挡住链上后面的厂商对这个窗口的合法服务**。所以 `withhold` 与"窗口内无文章"一律构造成 `NoMarketDataError` 抛出，让链路继续往下走（`ARCHITECTURE.md` §3.4 第 3 条）。

---

## §8 建议落地顺序

### 第一步（档 0，零成本，无需你提供任何东西）—— ✅ **已落地（2026-09-27）**

1. `cryptocurrency.cv` → 新 vendor（`vendors/cryptocurrency_cv.py`）。**偏离原计划**：只挂 `get_global_news`，**没有挂 `get_news`** —— 它的免费层没有按标的过滤的参数，挂在 `get_news` 上会给出"市场标题却假装是标的相关"；且窗口不到今日一律 `withhold`（§2.1 实测推翻其归档可用性）
2. `Google News RSS` → 新 vendor（`vendors/google_news_rss.py`），挂到 `get_news` **首位**与 `get_global_news`（兜底位）；en-US + zh-CN 双查询，中文覆盖靠它
3. `Alternative.me F&G` → 新方法 `get_sentiment_index`，新类别 `sentiment`（`vendors/alternative_me.py`）
4. `Telegram 预览` → 新 vendor（`vendors/telegram_preview.py`），独立取数模式（照 `reddit.py` 的样子），**不经路由**
5. **修漏挂**：`reddit.py` / `stocktwits.py` 已补进 `router.py` 的 `VENDOR_METHODS`，成为新方法 `get_sentiment`（新类别 `social_sentiment`）的厂商
6. **常驻采集进程** → `info_feeds/collector/`（`schema.sql` / `store.py` / `sources.py` / `runner.py`），`python -m info_feeds.collector` 起停。配套改造：上述各 vendor 各拆出一个 `fetch_*_rows()` 结构化出口（原 `get_*` 降为薄壳）。首轮跑通：新闻 / 社媒 / 情绪指数 / 宏观接口 / 预测市场 5 类里，除 `stocktwits`（403）与 `macro`（缺 `FRED_API_KEY`）外全部入库，harness 侧已能读到。详见 `ARCHITECTURE.md` §12

**验收结果**：
- ✅ 对同一个币种，`get_news` 同时返回英文源和中文源（`BTC-USD`：7807 字、CJK 109 字；`ETH-USD`：6951 字、CJK 52 字）
- ✅ 历史窗口调用返回哨兵而不是空串（`get_global_news 2024-01-05` → `NO_DATA_AVAILABLE: …(cryptocurrency.cv news is withheld for 2024-01-05: its search and archive endpoints are paywalled (HTTP 402) …)`）
- ✅ `get_sentiment_index today, 7` → `Latest: 70 (Greed) on 2026-09-27` + 7 期序列；`get_sentiment BTC-USD` → Reddit 通了
- 施工中修正的两处路由行为：`NoMarketDataError` 改记**第一条**（否则链尾把限流包装成 no-data，会盖掉链首的 withhold 说明）；链成员"没数据"改为 `raise`（`ARCHITECTURE.md` §3.4）

### 第二步（档 1，你注册 key）

- 补 `FRED_API_KEY` / `ALPHA_VANTAGE_API_KEY`（**已有实现，插上就能跑**）
- 行情兜底二选一：CoinGecko Demo 或 CMC Basic（后者可先试免注册端点）

**验收标准**：宏观类别不再报 `VendorNotConfiguredError`。

### 第三步（档 2，按归因需要再上）

- 若子策略归因显示缺**衍生品信号** → CoinGlass Standard $299
- 若缺**专业社媒情绪** → LunarCrush Individual $90

**不用提前买。**

---

## §9 与其它文档的关系

| 文档 | 关系 |
|---|---|
| `info-feeds/ARCHITECTURE.md` | 讲 **info-feeds 内部怎么运作**（路由 / 符号归一化 / 防前视 / 配置） |
| `../ARCHITECTURE.md` | 主规格，讲**整个交易系统** |
| `../SYSTEM_ARCHITECTURE.md` | 讲**主 Agent = 副驾驶 + 子策略独立交易** |
| **本文档** | 讲**每个信息源怎么接、要什么条件、花不花钱** |

---

## §10 待核实项

以下是**单一来源或口径不一致**的信息，落地前需复核：

- CryptoPanic 的当前 API 档位与价格（官方 plans 页未在本次调研中打开）
- Alpha Vantage 免费层的准确每日额度
- Messari Lite 层"月度不限量"的当前条款
- CoinGecko Demo 的限速口径：定价页写 100/min，API 文档写 30/min
