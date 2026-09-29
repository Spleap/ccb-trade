"""采集器配置：写哪个库、盯哪些标的 / 主题。

与 ``info_feeds/config.py`` 是两回事：那份决定"取数时选哪个厂商"（三层配置 + 厂商链），
这份决定"常驻进程写哪个库、对谁采集"。

所有项都可用环境变量覆盖，默认值只是让 ``python -m info_feeds.collector`` 开箱即跑。
"""

from __future__ import annotations

import os
from pathlib import Path

# info-feeds/info_feeds/collector/config.py → 上溯到工作区根，再进 harness 那个库。
# 信息层与账本层共用一个库，所以 harness 侧也读同一个 CCB_DB_PATH（harness/config.py）。
_WORKSPACE = Path(__file__).resolve().parents[3]
_HARNESS_DB = _WORKSPACE / "ccb-sub-agents" / "ccb_subagents.db"

DB_PATH = os.getenv("CCB_DB_PATH") or str(_HARNESS_DB)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(v.strip() for v in raw.split(",") if v.strip())


# 需要逐标的采集社媒 / 个股新闻的标的。
WATCHLIST = _csv_env("CCB_WATCHLIST", ("BTC-USD", "ETH-USD", "SOL-USD"))

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
