"""采集器配置：写哪个库、盯哪些标的 / 主题。

与 ``info_feeds/config.py`` 是两回事：那份决定"取数时选哪个厂商"（三层配置 + 厂商链），
这份决定"常驻进程写哪个库、对谁采集"。

所有项都可用环境变量覆盖，默认值只是让 ``python -m info_feeds.collector`` 开箱即跑。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# info-feeds/info_feeds/collector/config.py → 上溯到工作区根，再进 harness 那个库。
# 信息层与账本层共用一个库，所以 harness 侧也读同一个 CCB_DB_PATH（harness/config.py）。
_WORKSPACE = Path(__file__).resolve().parents[3]
_HARNESS_DB = _WORKSPACE / "ccb-sub-agents" / "ccb_subagents.db"
_AGENTS_DIR = _WORKSPACE / "ccb-sub-agents" / "agents"

DB_PATH = os.getenv("CCB_DB_PATH") or str(_HARNESS_DB)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(v.strip() for v in raw.split(",") if v.strip())


# ── 交易口径 → 信息层（Yahoo）口径 ──────────────────────────
# ★ 这张表必须与 `ccb-sub-agents/harness/tools/data.py` 的 `_INFO_ALIASES` 保持一致：
#   采侧用它决定"去采谁"，读侧用它决定"按什么名字查"。两边翻出来的字符串一旦不同，
#   `get_news` 的 LIKE 就永远查回空 —— 而空会被 LLM 当成"今天很平静"。同上，改一边就要改另一边。
_INFO_ALIASES = {
    # 美股 / ETF（Yahoo 就用裸代码）
    "AAPL": "AAPL", "NVDA": "NVDA", "TSLA": "TSLA", "MSFT": "MSFT",
    "META": "META", "GOOGL": "GOOGL", "AMZN": "AMZN", "NFLX": "NFLX",
    "AMD": "AMD", "INTC": "INTC", "COIN": "COIN", "MSTR": "MSTR",
    # 指数
    "SPX": "^GSPC", "SPX500": "^GSPC", "US500": "^GSPC",
    "NDX": "^NDX", "NDX100": "^NDX", "NAS100": "^NDX",
    "HSI": "^HSI", "HK50": "^HSI",
    # 贵金属（Yahoo 上黄金/白银是 COMEX 期货）
    "XAU": "GC=F", "GOLD": "GC=F", "XAG": "SI=F", "SILVER": "SI=F",
    # 外汇
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
}


def agent_symbol(raw: str) -> str:
    """把 Agent 的交易口径符号（`AAPL/USDT`）翻成信息层口径（`AAPL`）。"""
    match = re.match(r"[A-Za-z]+", str(raw).strip())
    if not match:
        return str(raw)
    base = match.group(0).upper()
    return _INFO_ALIASES.get(base) or f"{base}-USD"


def watchlist_from_agents(directory: Path | None = None) -> tuple[str, ...]:
    """把各策略 `universe` 的并集当作采集清单。

    **采集清单必须从策略派生，不能手工维护**：手工维护意味着"给 Agent 加了标的，
    却忘了让采集器去采它"，而后果是那次 `get_news` 查回空 —— LLM 不会知道是漏采，
    只会读成"这个标的最近很安静"。派生之后，加标的只改一处，永远不会失配。
    """
    d = directory or _AGENTS_DIR
    if not d.exists():
        return ()
    symbols: dict[str, None] = {}
    for path in sorted(d.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                       # 单份配置坏掉不该拖垮采集清单
        for symbol in spec.get("universe") or ():
            symbols.setdefault(agent_symbol(symbol), None)
    return tuple(symbols)


# 需要逐标的采集社媒 / 个股新闻的标的。
# 默认 = 各策略 universe 的并集（自动跟随策略变化）；`CCB_WATCHLIST` 可显式覆盖。
WATCHLIST = _csv_env("CCB_WATCHLIST",
                     watchlist_from_agents() or ("BTC-USD", "ETH-USD", "SOL-USD"))

# 预测市场的查询主题（Polymarket 按关键词搜索）。
PREDICTION_TOPICS = _csv_env(
    "CCB_PREDICTION_TOPICS", ("Fed rate cut", "recession 2026", "bitcoin 2026")
)

# 要采集的 FRED 序列，写别名（fred.py 的 MACRO_SERIES 能解析成 series id）。
MACRO_SERIES = _csv_env(
    "CCB_MACRO_SERIES", ("fed_funds_rate", "cpi", "unemployment_rate", "10y_treasury")
)

# 个股新闻的回溯天数。Google News 没有归档，只服务"当前搜索结果"，
# 所以每次都按最近几天重取一遍，靠 (source, external_id) 去重。
NEWS_LOOKBACK_DAYS = int(os.getenv("CCB_NEWS_LOOKBACK_DAYS") or 3)
