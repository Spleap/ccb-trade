"""`python -m harness` —— 把 sub agent 跑起来（Paper Trading + 交易指令输出）。

    python -m harness                      # 一直跑到 Ctrl-C
    python -m harness --ticks 6            # 跑 6 个 tick 就停（预检用）
    python -m harness --agent trend-scout-01
    python -m harness --provider deepseek  # 换 LLM（默认就是 deepseek）
    python -m harness --no-llm             # 不接 LLM：只看止损扫描与账本是否正常
    python -m harness --trace last         # 不跑实盘：打印上一次唤醒的完整思考轨迹

**在 `ccb-sub-agents/` 目录下运行**（`harness` 是这里的顶层包）。
想让它读到 info-feeds 采集的新闻，两个进程要指向同一个库 —— 设同一个 `CCB_DB_PATH`。
"""
from __future__ import annotations

import argparse
import os
from dataclasses import replace

from harness import agents, live, trace
from harness.config import DEFAULT
from harness.llm import LLMError, NullClient, build_client
from harness.store import db, repo
from harness.tools.candles import BitgetCandleSource


def _pick_specs(args) -> list[agents.AgentSpec] | None:
    specs = agents.load_all()
    if not args.agent:
        return specs
    wanted = set(args.agent)
    specs = [s for s in specs if s.agent_id in wanted]
    missing = wanted - {s.agent_id for s in specs}
    if missing:
        live._stdout(f"！找不到 agent 配置：{', '.join(sorted(missing))}")
        return None
    return specs


def _build_llm(args):
    """返回 (llm, 描述)；失败返回 (None, None)。"""
    if args.no_llm:
        return NullClient(), "offline（--no-llm，所有唤醒将降级）"
    try:
        llm = build_client(args.provider, args.model)
    except LLMError as exc:
        live._stdout(f"！{exc}")
        live._stdout("  先设好 key（如 DEEPSEEK_API_KEY），或用 --no-llm 做无 key 预检。")
        return None, None
    name = args.provider or os.environ.get("CCB_LLM_PROVIDER") or "deepseek"
    return llm, f"{name} / {getattr(llm, 'model', '?')}"


def _resolve_decision(conn, target: str, agent_id: str | None) -> str | None:
    """把 `--trace` 的入参翻成一个确定的 decision_id（`last` / 完整 id / id 前缀）。"""
    if target == "last":
        dec = repo.latest_decision(conn, agent_id)
        if dec is None:
            live._stdout("！库里还没有任何决策 —— 先用 `python -m harness` 跑一轮。")
            return None
        return dec["decision_id"]

    hits = repo.find_decisions(conn, target)
    if not hits:
        live._stdout(f"！找不到 id 以 `{target}` 开头的决策（抄错了？还是上一次运行还没写进去？）。"
                     f"用 `--trace last` 看最近一次。")
        return None
    if len(hits) > 1 and hits[0]["decision_id"] != target:
        ids = "、".join(h["decision_id"][:12] for h in hits)
        live._stdout(f"！`{target}` 匹配到多条（{ids}），请多抄几位。")
        return None
    return hits[0]["decision_id"]


def _print_trace(conn, args) -> int:
    decision_id = _resolve_decision(conn, args.trace, (args.agent or [None])[0])
    if decision_id is None:
        return 2
    live._stdout(trace.render(conn, decision_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness",
                                 description="跑 sub agent 做 Paper Trading 并输出交易指令")
    ap.add_argument("--db", help="SQLite 路径（默认 harness.config.DB_PATH）")
    ap.add_argument("--agent", action="append", default=None,
                    help="只跑指定 agent_id（可重复）")
    ap.add_argument("--ticks", type=int, default=None,
                    help="跑 N 个 tick 后退出（预检用；默认一直跑）")
    ap.add_argument("--provider", default=None,
                    help="LLM provider：deepseek / openai / openrouter")
    ap.add_argument("--model", default=None, help="覆盖默认模型")
    ap.add_argument("--no-llm", action="store_true",
                    help="不接 LLM：所有唤醒走降级，用于无 key 预检")
    ap.add_argument("--no-stream", action="store_true",
                    help="关掉实时输出（默认会把思考和工具调用流式打在终端上）")
    ap.add_argument("--no-trace", action="store_true",
                    help="不把思考和工具调用落进 agent_trace（只影响观测层，不影响决策）")
    ap.add_argument("--trace", metavar="<id|last|前缀>", default=None,
                    help="不跑实盘：打印某次唤醒的完整轨迹（思考 + 工具调用）后退出。"
                         "配 `--agent` 可限定 `last` 取谁的")
    args = ap.parse_args(argv)

    cfg = DEFAULT if not args.db else replace(DEFAULT, db_path=args.db)
    if args.no_trace:
        cfg = replace(cfg, trace_enabled=False)

    if args.trace:
        conn = db.connect(cfg.db_path)
        try:
            return _print_trace(conn, args)
        finally:
            conn.close()

    specs = _pick_specs(args)
    if specs is None:
        return 2
    if not specs:
        live._stdout("！agents/ 里没有任何配置，先放一份 agents/*.json")
        return 2

    llm, llm_desc = _build_llm(args)
    if llm is None:
        return 2

    conn = db.connect(cfg.db_path)
    try:
        live.banner(specs, cfg, llm_desc)
        live.run(conn, specs=specs, llm=llm, candles=BitgetCandleSource(),
                 cfg=cfg, ticks=args.ticks, stream=not args.no_stream)
        live.summary(conn, specs)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
