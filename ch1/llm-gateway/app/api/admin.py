"""
/v1/admin — 指标只读查询端点。

把 CLI 已有的查询能力（summary / traces / daily）暴露为 HTTP，便于外部脚本与
看板读取。只读、需 Bearer 鉴权、查询规模有硬上限，不提供任何写操作。
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query, Request

from ..context import AppContext

logger = logging.getLogger("llm-gateway.api.admin")

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# 单次查询条数硬上限，防止一次请求把 SQLite 读满
MAX_LIMIT = 200


async def _authorize(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    await ctx.auth.check(request.headers.get("authorization"), ctx.config_manager.get_api_keys)
    return ctx


@router.get("/summary")
async def summary(
    request: Request,
    group_by: Literal["provider", "model", "provider_model"] | None = Query(
        None, description="按 provider/model 维度分组；不传则返回全局聚合"
    ),
    days: int = Query(0, ge=0, le=365, description="回溯天数，0 表示全量"),
) -> dict:
    """累计用量概览（请求数 / token / 成本 / 平均时延 / 错误数）。

    传 group_by 时返回 {group_by, days, totals, items}，items 为分组明细；
    不传时保持原扁平结构，向后兼容 CLI/看板。
    """
    ctx = await _authorize(request)
    return await ctx.store.query_summary(group_by=group_by, days=days)


@router.get("/traces")
async def traces(
    request: Request,
    limit: int = Query(20, ge=1, le=MAX_LIMIT, description="返回条数上限"),
    status: str | None = Query(
        None, description="按状态过滤：success/error/partial_failed/cancelled"
    ),
    provider: str | None = Query(None, description="按供应商过滤"),
    model: str | None = Query(None, description="按模型过滤"),
    days: int = Query(0, ge=0, le=365, description="回溯天数，0 表示全量"),
) -> dict:
    """最近请求追踪记录（不含 prompt/response 原文）。"""
    ctx = await _authorize(request)
    items = await ctx.store.query_recent_traces(
        limit=limit, status=status, provider=provider, model=model, days=days
    )
    return {
        "limit": limit,
        "status": status,
        "provider": provider,
        "model": model,
        "days": days,
        "items": items,
    }


@router.get("/daily")
async def daily(
    request: Request,
    days: int = Query(7, ge=1, le=90, description="回溯天数"),
    provider: str | None = Query(None, description="按供应商过滤"),
    model: str | None = Query(None, description="按模型过滤"),
) -> dict:
    """按天聚合的用量与成本。"""
    ctx = await _authorize(request)
    items = await ctx.store.query_daily_aggregates(
        days=days, provider=provider, model=model
    )
    return {
        "days": days,
        "provider": provider,
        "model": model,
        "items": items,
    }
