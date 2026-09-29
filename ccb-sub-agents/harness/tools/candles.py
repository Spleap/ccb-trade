"""K 线来源：**pull 层**（ARCHITECTURE §2.1）。

K 线体量太大，不落库、唤醒时按需拉 —— 所以这里是一个**接口**，由调用方注入实现：

| 场景 | 实现 |
|---|---|
| 实盘 | `BitgetCandleSource`：直接打 Bitget 现货 v2 candles |
| 测试 / 离线 | `ListCandleSource`：从给定的 bars 里按 `as_of` 切片 |

**防前视统一在这一层做（§7.2 / §2.6）**：一个 bar 只有在
`bar.ts + tf秒数 <= as_of` 时才算"已完结、可当收盘价用"。
这一条不写死，任何基于它的判断都是假的 —— 所以它由接口层保证，不给上层留绕过口子。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


class CandleSource(Protocol):
    def fetch(self, symbol: str, tf: str, limit: int, as_of: int) -> list[dict]:
        """返回**升序**、**已完结**、`ts + tf <= as_of` 的 OHLCV，最多 limit 根。"""
        ...


class ListCandleSource:
    """离线 / 测试用。bars 必须升序，字段 ts/open/high/low/close/volume。"""

    def __init__(self, bars_by_symbol: dict[str, list[dict]], tf: str = "1h"):
        if tf not in TF_SECONDS:
            raise ValueError(f"不支持的周期 {tf!r}")
        self.tf = tf
        self._data = bars_by_symbol

    def fetch(self, symbol: str, tf: str, limit: int, as_of: int) -> list[dict]:
        if tf != self.tf:
            raise ValueError(f"ListCandleSource 只提供 {self.tf}，不支持 {tf}")
        step = TF_SECONDS[tf]
        closed = [b for b in self._data.get(symbol, []) if b["ts"] + step <= as_of]
        return closed[-limit:] if limit > 0 else closed


# ============================================================
# 实盘：Bitget 现货 v2
# ============================================================

_BITGET_URL = "https://api.bitget.com/api/v2/spot/market/candles"

# 本地周期名 -> Bitget granularity。Bitget 用小写，且 1m/1d 拼作 1min/1day。
_BITGET_GRANULARITY = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "1d": "1day",
}

# Bitget 单次请求的**两个**上限，必须取更严的那个：
#   ① 根数上限 1000
#   ② **时间跨度上限 30 天** —— 实测：1h 请求 720 根（29.96 天）正常，
#      721 根（30.0 天）时第二页直接返回空。这是 Bitget 侧的限制，
#      不是分页写错了。所以按周期把 30 天换算成根数，与 1000 取小值。
# 只有 1h 及更粗的周期会被这条卡住：15m × 1000 只有 10.4 天，离上限还很远。
_PAGE_BARS = 1000
_MAX_SPAN_SECONDS = 30 * 86400


def _max_bars_per_request(tf: str) -> int:
    """单次请求该周期最多能要多少根。"""
    return max(1, min(_PAGE_BARS, _MAX_SPAN_SECONDS // TF_SECONDS[tf]))


def _bitget_symbol(symbol: str) -> str:
    """把 agent 口径的符号翻成 Bitget 口径：`BTC/USDT` / `BTC-USD` / `BTC` -> `BTCUSDT`。"""
    s = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()
    if s.endswith("USDT") or s.endswith("USDC"):
        return s
    if s.endswith("USD"):
        return s + "T"          # BTCUSD -> BTCUSDT
    return s + "USDT"           # BTC -> BTCUSDT


class BitgetCandleSource:
    """实盘 K 线源：直接打 Bitget 现货 v2 candles。

    三件事在 `fetch` 里一次做对：

    * **符号/周期映射** —— agent 说 `BTC/USDT` + `1h`，Bitget 要 `BTCUSDT` + `1h`
    * **防前视**（§2.6）—— 只吐 `ts + tf秒数 <= as_of` 的**已完结** bar，
      正在走的那一根永远被挡在外面
    * **缓存** —— 同一 tick 内 watchdog / scheduler 会重复问
      同一个 (symbol, tf)，用短 TTL 缓存挡掉；**只缓存成功**，失败绝不缓存
      （否则一次网络抖动会被缓存成一个 tick 的空数据）
    """

    def __init__(self, ttl: float = 10.0, timeout: float = 10.0):
        self.ttl = ttl
        self.timeout = timeout
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    def fetch(self, symbol: str, tf: str, limit: int, as_of: int) -> list[dict]:
        if tf not in TF_SECONDS:
            raise ValueError(f"不支持的周期 {tf!r}")
        bars = self._raw(symbol, tf, limit)
        step = TF_SECONDS[tf]
        closed = [b for b in bars if b["ts"] + step <= as_of]
        return closed[-limit:] if limit > 0 else closed

    # ── 内部 ────────────────────────────────────────

    def _raw(self, symbol: str, tf: str, limit: int) -> list[dict]:
        key = (symbol, tf)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl and len(hit[1]) >= limit:
            return hit[1]
        bars = self._download(symbol, tf, limit)
        self._cache[key] = (now, bars)
        return bars

    def _download(self, symbol: str, tf: str, limit: int) -> list[dict]:
        # 多取 2 根：as_of 过滤可能吃掉正在走的一根，甚至刚收的一根
        return _bitget_candles(symbol, tf, min(int(limit) + 2, _max_bars_per_request(tf)),
                               self.timeout)


# ============================================================
# Bitget 现货 K 线：底层请求
# ============================================================


def _bitget_request(url: str, symbol: str, tf: str, limit: int, timeout: float,
                    end_time_ms: int | None) -> list[dict]:
    """打一次 Bitget 现货 K 线接口，返回**升序** OHLCV（不做任何截断/过滤）。

    每行 `[ts(ms), open, high, low, close, baseVol, quoteVol, ...]`。
    Bitget 原生就是**升序**（实测：limit=5 时第 0 行是最旧的一根，
    最后一根是正在走的那根），**不要 reverse** —— 反转会让 `closed[-limit:]`
    取到最旧的 N 根，整个窗口系统性地滞后 1~2 根。

    `end_time_ms` 是给翻页用的：只要截止在这一刻（含）之前的 K 线。
    """
    if tf not in _BITGET_GRANULARITY:
        raise ValueError(f"不支持的周期 {tf!r}")
    params = {
        "symbol": _bitget_symbol(symbol),
        "granularity": _BITGET_GRANULARITY[tf],
        "limit": max(1, int(limit)),
    }
    if end_time_ms is not None:
        params["endTime"] = str(int(end_time_ms))
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bitget K 线请求失败（{symbol} {tf}）：{exc}") from exc

    if str(payload.get("code")) != "00000":
        raise RuntimeError(
            f"Bitget K 线返回错误（{symbol} {tf}）："
            f"{payload.get('code')} {payload.get('msg')}")

    return [
        {"ts": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
         "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
        for row in (payload.get("data") or [])
        if len(row) >= 6
    ]


def _bitget_candles(symbol: str, tf: str, limit: int, timeout: float = 10.0,
                    end_time_ms: int | None = None) -> list[dict]:
    """`/candles`：只服务**最近**一段历史。limit 按 `_max_bars_per_request` 收口。"""
    return _bitget_request(_BITGET_URL, symbol, tf,
                           min(int(limit), _max_bars_per_request(tf)),
                           timeout, end_time_ms)


def last_close(source: CandleSource, symbol: str, tf: str, as_of: int) -> float | None:
    """最后一根**已完结** bar 的收盘价。拿不到返回 None。

    这就是"成交价永远取决策之后的价格"里的那个价格（§7.2）——
    因为它带着 `as_of` 过滤，结构上不可能读到未来。
    """
    bars = source.fetch(symbol, tf, 1, as_of)
    return float(bars[-1]["close"]) if bars else None
