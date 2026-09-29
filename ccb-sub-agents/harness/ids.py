"""id 生成。

实盘用随机串；测试 / 预检用**由输入算出来**的串，这样"同一份输入跑两遍结果一致"
才是可验证的（连两个库 diff 都做得了）。

    new_id(clock, "dec", agent_id, ts, n)   →  实盘 "9f3c…" / 固定时钟 "dec-lin-01-1700000000-0"

`parts` 必须**唯一确定那一件事**。同一秒里可能发生多件事（一次唤醒里既平仓又反向开仓），
所以决策的 `parts` 里带了一个"这一秒的第几条"—— 见 `repo.count_decisions_at`。
成交的 id 同理（`paper/ledger.py` 里自己拼了序号）。

凡是"天然归属于某个已知 id"的东西（快照属于决策、情节属于决策）不必走这里，
直接拼前缀即可。
"""
from __future__ import annotations

import uuid


def new_id(clock, prefix: str, *parts: object) -> str:
    """`clock.deterministic` 时由 `parts` 决定，否则随机。

    时钟是唯一知道"这次跑是否可以复现"的地方，所以由它来定这件事最不容易出错。
    """
    if getattr(clock, "deterministic", False):
        return f"{prefix}-" + "-".join(str(p) for p in parts)
    return uuid.uuid4().hex
