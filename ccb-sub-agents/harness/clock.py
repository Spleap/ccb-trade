"""时间源。

**需要"现在几点"的地方一律走 Clock，不许直接调 `time.time()`。**
理由：测试与预检需要把时间钉死，否则"同一份输入跑两遍结果一致"就无从验证。

| 实现 | 用途 |
|---|---|
| `RealClock` | 实盘：跟随墙上时间，并自带 `sleep` |
| `FixedClock` | 测试 / 预检：时间由调用方手动推进，绝不自己走 |
"""
from __future__ import annotations

import time


class RealClock:
    """跟随墙上时间。"""

    deterministic = False

    def now(self) -> int:
        return int(time.time())

    def sleep(self, seconds: int) -> None:
        """循环节奏用。注意：它只负责"等多久"，不负责"现在几点"。"""
        time.sleep(seconds)


class FixedClock:
    """时间由外部推进，自己绝不走。测试与预检用（`deterministic=True` 时 id 也可复现）。"""

    deterministic = True

    def __init__(self, start: int):
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def advance_to(self, ts: int) -> None:
        if ts < self._now:
            raise ValueError(f"时钟不能倒退：{ts} < {self._now}")
        self._now = int(ts)

    def advance_by(self, seconds: int) -> None:
        self._now += int(seconds)

    def sleep(self, seconds: int) -> None:
        """固定时钟**绝不自己走时间**：时间只能由调用方 advance_to 推进。"""
        return None
