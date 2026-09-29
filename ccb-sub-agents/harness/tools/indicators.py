"""技术指标：**服务端算，不让 LLM 算**（ARCHITECTURE §4.3）。

不让 LLM 算的理由不是它算不对，而是**它每次都可能算出不同的数** ——
同一个 RSI，两次调用给两个值，回测就不可复现了。

纯函数，不引入 numpy / pandas：指标本身只有几十行算术，
多一个依赖不如多十行代码（§10.1 "不引入重框架"）。

平滑方式统一用 Wilder（与主流平台一致），EMA 以首值为种子。
"""
from __future__ import annotations

SUPPORTED = ("sma20", "sma50", "ema12", "ema26", "rsi14", "atr14", "macd", "boll20")


def sma(values: list[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list[float], n: int) -> list[float]:
    if not values or n <= 0:
        return []
    k = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def ema(values: list[float], n: int) -> float | None:
    series = ema_series(values, n)
    return series[-1] if series else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> float | None:
    if len(highs) != len(lows) or len(lows) != len(closes) or len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    value = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        value = (value * (n - 1) + trs[i]) / n
    return value


def macd(closes: list[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    fast_line = ema_series(closes, fast)
    slow_line = ema_series(closes, slow)
    line = [f - s for f, s in zip(fast_line, slow_line)]
    sig = ema_series(line, signal)
    return {"macd": line[-1], "signal": sig[-1], "hist": line[-1] - sig[-1]}


def boll(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    sd = (sum((v - mid) ** 2 for v in window) / n) ** 0.5
    return {"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd}


def compute(names: list[str], bars: list[dict]) -> dict:
    """按名字算一批指标，只返回算得出来的（算不出的进 `skipped`）。

    bars: 升序的 [{ts, open, high, low, close, volume}]
    """
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]

    table = {
        "sma20": lambda: sma(closes, 20),
        "sma50": lambda: sma(closes, 50),
        "ema12": lambda: ema(closes, 12),
        "ema26": lambda: ema(closes, 26),
        "rsi14": lambda: rsi(closes, 14),
        "atr14": lambda: atr(highs, lows, closes, 14),
        "macd": lambda: macd(closes),
        "boll20": lambda: boll(closes),
    }

    out: dict = {}
    skipped: list[str] = []
    for name in names:
        fn = table.get(name)
        if fn is None:
            skipped.append(f"{name}(未知)")
            continue
        value = fn()
        if value is None:
            skipped.append(f"{name}(样本不足)")
            continue
        out[name] = _round(value)
    if skipped:
        out["skipped"] = skipped
    return out


def _round(value):
    if isinstance(value, dict):
        return {k: round(float(v), 6) for k, v in value.items()}
    return round(float(value), 6)
