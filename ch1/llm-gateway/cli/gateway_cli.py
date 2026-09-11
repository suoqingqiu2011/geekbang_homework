"""
gateway-cli — SQLite 指标/追踪/模板查询工具（SPEC §13 / M5）。

用法示例：
    python -m cli.gateway_cli summary
    python -m cli.gateway_cli traces --limit 10 --status error
    python -m cli.gateway_cli daily --days 7 --provider deepseek
    python -m cli.gateway_cli circuit
    python -m cli.gateway_cli templates upsert --id demo --name Demo --content "你是助手，请回答：{{ question }}"
    python -m cli.gateway_cli templates list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional

# 允许从项目根目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage.metrics_store import MetricsStore  # noqa: E402


def _store(args) -> MetricsStore:
    db_path = args.db or os.environ.get("GATEWAY_DB_PATH", "data/metrics.db")
    return MetricsStore(db_path)


async def _summary(args) -> None:
    store = _store(args)
    await store.initialize()
    try:
        s = await store.query_summary()
        print(json.dumps(s, ensure_ascii=False, indent=2))
    finally:
        await store.close()


async def _traces(args) -> None:
    store = _store(args)
    await store.initialize()
    try:
        rows = await store.query_recent_traces(limit=args.limit, status=args.status)
        for r in rows:
            print(json.dumps(r, ensure_ascii=False, default=str))
    finally:
        await store.close()


async def _daily(args) -> None:
    store = _store(args)
    await store.initialize()
    try:
        rows = await store.query_daily_aggregates(days=args.days, provider=args.provider)
        if not rows:
            print("(no data)")
        for r in rows:
            print(
                f"{r['day']}  {r['provider']:<12} {r['model']:<20} "
                f"req={r['requests']:<6} err={r['errors']:<4} "
                f"tokens={r['total_tokens']:<10} cost=${r['cost_usd']:.4f} "
                f"avg_latency={r['total_latency_ms'] / max(r['requests'], 1):.1f}ms"
            )
    finally:
        await store.close()


async def _circuit(args) -> None:
    store = _store(args)
    await store.initialize()
    try:
        cursor = await store._require_db().execute("SELECT * FROM circuit_states ORDER BY provider")  # noqa: SLF001
        rows = await cursor.fetchall()
        if not rows:
            print("(no circuit states yet)")
        for r in rows:
            print(f"{r['provider']:<12} state={r['state']:<9} failures={r['consecutive_failures']}")
    finally:
        await store.close()


async def _templates(args) -> None:
    store = _store(args)
    await store.initialize()
    try:
        if args.tpl_action == "list":
            cursor = await store._require_db().execute(  # noqa: SLF001
                "SELECT template_id, name, version, updated_at FROM prompt_templates ORDER BY updated_at DESC"
            )
            rows = await cursor.fetchall()
            if not rows:
                print("(no templates)")
            for r in rows:
                print(f"{r['template_id']:<24} name={r['name']:<16} v{r['version']} updated={r['updated_at']:.0f}")
        elif args.tpl_action == "get":
            rec = await store.get_template(args.template_id)
            print(json.dumps(rec, ensure_ascii=False, indent=2) if rec else "(not found)")
        elif args.tpl_action == "upsert":
            await store.upsert_template(args.template_id, args.name, args.content)
            print(f"upserted: {args.template_id}")
        elif args.tpl_action == "delete":
            await store.delete_template(args.template_id)
            print(f"deleted: {args.template_id}")
    finally:
        await store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway-cli", description="LLM Gateway 运维查询 CLI")
    p.add_argument("--db", help="SQLite 数据库路径（默认 data/metrics.db）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("summary", help="全局用量汇总")
    sp.set_defaults(func=_summary)

    sp = sub.add_parser("traces", help="最近追踪记录")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--status", choices=["success", "error", "cancelled"])
    sp.set_defaults(func=_traces)

    sp = sub.add_parser("daily", help="按日聚合报表")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--provider")
    sp.set_defaults(func=_daily)

    sp = sub.add_parser("circuit", help="熔断器状态")
    sp.set_defaults(func=_circuit)

    sp = sub.add_parser("templates", help="提示词模板管理")
    tsub = sp.add_subparsers(dest="tpl_action", required=True)
    tsub.add_parser("list").set_defaults(func=_templates)
    tget = tsub.add_parser("get")
    tget.add_argument("template_id")
    tget.set_defaults(func=_templates)
    tup = tsub.add_parser("upsert")
    tup.add_argument("--id", dest="template_id", required=True)
    tup.add_argument("--name", required=True)
    tup.add_argument("--content", required=True)
    tup.set_defaults(func=_templates)
    tdel = tsub.add_parser("delete")
    tdel.add_argument("template_id")
    tdel.set_defaults(func=_templates)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
