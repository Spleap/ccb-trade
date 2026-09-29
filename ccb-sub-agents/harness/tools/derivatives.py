"""派生品数据：持仓量（OI）与资金费率 —— Bitget 永续合约。

**只做永续合约**（Bitget `USDT-FUTURES`）：OI 与资金费率只存在于合约，现货没有这两个概念。
K 线也已经切到永续口径（见 `candles.py`），所以这里的价格与 K 线是同一个标的、
同一个市场，不存在"现货 K 线 + 合约指标"的基差混用。

三条实测结论决定了这个模块的形状
--------------------------------

1. **OI 只有当前值，没有历史序列。** Bitget 没有 OI 历史接口；
   而 `/api/v3/market/candles?type=open_interest` 是个**陷阱** —— 它不报错，
   而是**静默回落成普通成交价 K 线**（实测返回值与 `type=normal` 逐字相同：
   `84310/84318.3/83973.1/84150.5`，而同期真实 OI 是 `31451`）。
   所以这里**绝不请求那个 type**，OI 只给现值，并在返回文本里明说"没有序列" ——
   让 LLM 知道自己看不到趋势，而不是让它把一条假序列当成趋势。
   （真要看趋势，靠我们自己积累：每轮把 OI 现值和行情一起落进 `market_snapshot`。）

2. **资金费率有历史序列**：`/api/v2/mix/market/history-fund-rate`，
   一页 100 条、8 小时一期（实测可回溯约 33 天），接口给的是**倒序**，这里排回升序。

3. **v3 tickers 一次就把 OI / 费率 / 标记价 / 指数价全给了**，不用调四个接口；
   但它**不含**结算节奏与下次结算时间，那要另打一次 `current-fund-rate`。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from harness.tools.candles import bitget_symbol

BASE = "https://api.bitget.com"
PERP = "USDT-FUTURES"                  # USDT 本位永续，符号与现货同名（BTCUSDT）
TIMEOUT = 10.0
MAX_PERIODS = 100                      # history-fund-rate 单页上限（实测）


def _get_json(path: str, params: dict, timeout: float = TIMEOUT):
    """打一次 Bitget 接口，返回 `data` 段。失败抛 `RuntimeError`（由 Tool.run 转成哨兵）。"""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bitget 请求失败（{path}）：{exc}") from exc

    if str(payload.get("code")) != "00000":
        raise RuntimeError(
            f"Bitget 返回错误（{path}）：{payload.get('code')} {payload.get('msg')}")
    return payload.get("data")


def ticker(symbol: str, timeout: float = TIMEOUT) -> dict:
    """v3 tickers 的一条：lastPrice / markPrice / indexPrice / fundingRate / openInterest…

    注意 `fundingRate` 这里是**当期费率**（下一次结算将执行的那个），
    与 `funding_now()` 的值应当一致 —— 一致就说明数据被盯住了。
    """
    data = _get_json("/api/v3/market/tickers",
                     {"category": PERP, "symbol": bitget_symbol(symbol)}, timeout)
    rows = data if isinstance(data, list) else [data]
    rows = [r for r in rows if r]
    if not rows:
        raise RuntimeError(f"{symbol} 在 {PERP} 里没有这个品种")
    return rows[0]


def funding_now(symbol: str, timeout: float = TIMEOUT) -> dict:
    """当期资金费率的**结算节奏**：费率 / 结算周期(小时) / 下次结算时间(秒)。"""
    data = _get_json("/api/v2/mix/market/current-fund-rate",
                     {"symbol": bitget_symbol(symbol), "productType": PERP}, timeout)
    rows = data if isinstance(data, list) else [data]
    rows = [r for r in rows if r]
    if not rows:
        raise RuntimeError(f"{symbol} 没有当期资金费率")
    row = rows[0]
    out = {"rate": float(row["fundingRate"])}
    if row.get("fundingRateInterval"):
        out["interval_h"] = float(row["fundingRateInterval"])
    if row.get("nextUpdate"):
        out["next_ts"] = int(row["nextUpdate"]) // 1000
    return out


def funding_history(symbol: str, periods: int = 12, *, as_of: int | None = None,
                    timeout: float = TIMEOUT) -> list[dict]:
    """最近 `periods` 期资金费率，**升序**（最早 -> 最新）。

    `as_of` 给了就当成硬上界：结算时间在它之后的期数一律丢掉 ——
    与 K 线的防前视同一条纪律，不给上层留"看到未来"的口子。
    """
    page = max(1, min(int(periods), MAX_PERIODS))
    data = _get_json("/api/v2/mix/market/history-fund-rate",
                     {"symbol": bitget_symbol(symbol), "productType": PERP,
                      "pageSize": page}, timeout)
    rows = [{"ts": int(r["fundingTime"]) // 1000, "rate": float(r["fundingRate"])}
            for r in (data or [])]
    if as_of is not None:
        rows = [r for r in rows if r["ts"] <= as_of]
    rows.sort(key=lambda r: r["ts"])            # 接口给的是倒序，别信它的顺序
    return rows
