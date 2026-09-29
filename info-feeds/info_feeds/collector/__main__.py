"""``python -m info_feeds.collector`` —— 启动信息层常驻采集进程。

在 ``info-feeds/`` 目录下运行（或把该目录加进 ``PYTHONPATH``）：

    python -m info_feeds.collector              # 常驻，按各源节奏循环
    python -m info_feeds.collector --once       # 只跑一轮就退出（冒烟用）
    python -m info_feeds.collector --db X.db    # 覆盖 CCB_DB_PATH

写库位置默认是 ``ccb-sub-agents/ccb_subagents.db``，可用 ``CCB_DB_PATH`` 覆盖；
盯哪些标的 / 主题见 ``collector/config.py``。
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config, store
from .runner import run_forever, run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m info_feeds.collector")
    parser.add_argument("--once", action="store_true", help="跑一轮就退出（冒烟测试用）")
    parser.add_argument("--db", default=None, help="覆盖库路径（默认 CCB_DB_PATH）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打开 DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.once:
        conn = store.connect(args.db)
        try:
            written = run_once(conn)
        finally:
            conn.close()
        for name, count in written.items():
            print(f"{name}: {count if count is not None else 'FAILED'}")
        return 0

    run_forever(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
