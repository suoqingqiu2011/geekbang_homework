"""
/v1/templates — 提示词模板管理 HTTP 端点（SPEC §4.5）。

将原先仅 CLI 可用的模板管理能力暴露为 REST 端点：列出 / 查询 / 新增版本 /
删除 / 预览渲染。鉴权与业务逻辑复用 AppContext 中的 store 与 prompt_renderer。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from ..context import AppContext
from ..errors import NotFoundError
from ..schemas import TemplateOut, TemplateRenderRequest, TemplateUpsertRequest

logger = logging.getLogger("llm-gateway.api.templates")

router = APIRouter(prefix="/v1/templates", tags=["templates"])


async def _authorize(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    await ctx.auth.check(request.headers.get("authorization"), ctx.config_manager.get_api_keys)
    return ctx


def _to_out(record: dict) -> TemplateOut:
    return TemplateOut(
        template_id=record["template_id"],
        name=record["name"],
        content=record["content"],
        version=record["version"],
        updated_at=record["updated_at"],
    )


@router.get("", response_model=list[TemplateOut])
async def list_templates(request: Request) -> list[TemplateOut]:
    """列出所有模板（每个 template_id 的最新版本）。"""
    ctx = await _authorize(request)
    return [_to_out(r) for r in await ctx.store.list_templates()]


@router.get("/{template_id}", response_model=TemplateOut)
async def get_template(
    request: Request,
    template_id: str,
    version: int | None = Query(default=None, ge=1),
) -> TemplateOut:
    """查询模板。version 缺省返回最新版本。"""
    ctx = await _authorize(request)
    record = await ctx.store.get_template(template_id, version=version)
    if record is None:
        raise NotFoundError(f"Prompt template not found: {template_id}"
                            + (f" (version {version})" if version is not None else ""))
    return _to_out(record)


@router.get("/{template_id}/versions", response_model=list[TemplateOut])
async def list_template_versions(request: Request, template_id: str) -> list[TemplateOut]:
    """列出指定模板的全部历史版本（按版本号升序）。"""
    ctx = await _authorize(request)
    records = await ctx.store.list_template_versions(template_id)
    if not records:
        raise NotFoundError(f"Prompt template not found: {template_id}")
    return [_to_out(r) for r in records]


@router.post("", response_model=TemplateOut)
async def upsert_template(request: Request, body: TemplateUpsertRequest) -> TemplateOut:
    """新增一个模板版本（自增 version、保留历史），返回新版本。"""
    ctx = await _authorize(request)
    new_version = await ctx.prompt_renderer.upsert(body.template_id, body.name, body.content)
    record = await ctx.store.get_template(body.template_id, version=new_version)
    assert record is not None  # 刚写入必存在
    return _to_out(record)


@router.delete("/{template_id}")
async def delete_template(
    request: Request,
    template_id: str,
    version: int | None = Query(default=None, ge=1),
) -> dict:
    """删除模板。version 缺省删除全部版本；否则仅删除指定版本。"""
    ctx = await _authorize(request)
    if version is not None:
        if await ctx.store.get_template(template_id, version=version) is None:
            raise NotFoundError(f"Prompt template not found: {template_id} (version {version})")
        await ctx.store.delete_template(template_id, version=version)
        ctx.prompt_renderer.invalidate(template_id)
        return {"template_id": template_id, "deleted": 1}
    versions = await ctx.store.list_template_versions(template_id)
    if not versions:
        raise NotFoundError(f"Prompt template not found: {template_id}")
    await ctx.store.delete_template(template_id)
    ctx.prompt_renderer.invalidate(template_id)
    return {"template_id": template_id, "deleted": len(versions)}


@router.post("/{template_id}/render")
async def render_template(
    request: Request,
    template_id: str,
    body: TemplateRenderRequest,
) -> dict:
    """用给定变量预览渲染模板（不调用上游）。version 缺省渲染最新版本。"""
    ctx = await _authorize(request)
    rendered = await ctx.prompt_renderer.render(
        template_id, body.variables, version=body.version
    )
    return {"template_id": template_id, "rendered": rendered}