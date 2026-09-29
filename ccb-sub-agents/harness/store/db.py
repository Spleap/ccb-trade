"""SQLite 连接与建表。

用 stdlib sqlite3 + 薄仓储层，不引入 ORM（ARCHITECTURE §10.1）。

事务约定：仓储层函数不自己 commit。调用方用 `with conn:` 包住一整段业务操作，
保证"一次决策"的所有写入是原子的。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: str, init: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: str | Path | None = None) -> None:
    if schema_path is None:
        from harness.config import SCHEMA_PATH

        schema_path = SCHEMA_PATH
    conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
    conn.commit()
