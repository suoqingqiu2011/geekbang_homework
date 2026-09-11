"""
/v1/chat/completions + /v1/models — 网关主入口（SPEC §5 / §6）。

请求管道：AuthCheck → RateLimiter → Pydantic → Trace 固化 → 注入检测
→ 模板渲染 → Router → Retry/Fallback → Adapter → Validator → 幂等计费 → 响应。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..adapters.registry import get_adapter
from ..context import AppContext
from ..core.retry_fallback import RetryPolicy
from ..core.trace_freeze import TraceFreezeRecord
from ..errors import (
    GatewayError,
    RateLimitExceededError,
    StructuredOutputError,
    UpstreamError,
)
from ..schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceOut,
    ModelOut,
    ModelsResponse,
    UsageOut,
)
from ..storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway.api.chat")

router = APIRouter(prefix="/v1", tags=["gateway"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_done() -> str:
    return "data: [DONE]\n\n"


def _error_event(err: GatewayError) -> str:
    return _sse(err.to_response())


# ─────────────────────────────────────────────────────────────
# GET /v1/models
# ─────────────────────────────────────────────────────────────
@router.get("/models")
async def list_models(request: Request) -> ModelsResponse:
    ctx: AppContext = request.app.state.ctx
    await ctx.auth.check(request.headers.get("authorization"), ctx.config_manager.get_api_keys)
    cfg = ctx.config_manager.config
    data = [
        ModelOut(id=m.name, owned_by=m.provider)
        for m in cfg.available_models
    ]
    return ModelsResponse(data=data)


# ─────────────────────────────────────────────────────────────
# POST /v1/chat/completions
# ─────────────────────────────────────────────────────────────
@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    ctx: AppContext = request.app.state.ctx

    # 1. 鉴权
    await ctx.auth.check(request.headers.get("authorization"), ctx.config_manager.get_api_keys)

    # 2. 限流（per-model > per-endpoint > per-project > per-user）
    allowed, retry_after, layer = await ctx.rate_limiter.check(
        model=body.model,
        endpoint_key="POST:/v1/chat/completions",
        project_id=body.project,
        user_id=body.user,
    )
    if not allowed:
        raise RateLimitExceededError(
            f"Rate limit exceeded (layer={layer}). Retry after {retry_after:.1f}s.",
            retry_after=retry_after,
        )

    # 3. Trace 固化
    trace_id = body.trace_id or str(uuid.uuid4())
    trace = TraceFreezeRecord(
        trace_id=trace_id,
        endpoint="chat/completions",
        user_id=body.user,
        project_id=body.project,
        client_ip=_client_ip(request),
        budget_snapshot=body.budget_usd,
        has_schema=body.response_format is not None,
        schema_name=body.response_format.json_schema.name if body.response_format and body.response_format.json_schema else "",
        stream=body.stream,
    )

    # 4. Prompt 注入防护
    ctx.validator.check_prompt_injection([m.model_dump() for m in body.messages])

    # 5. 模板渲染（可选）
    messages: list[dict[str, Any]] = [m.model_dump() for m in body.messages]
    if body.template_id:
        rendered = await ctx.prompt_renderer.render(body.template_id, body.template_vars)
        messages = [{"role": "system", "content": rendered}] + [
            m for m in messages if m["role"] != "system"
        ]

    # 6. 路由
    cfg = ctx.config_manager.config
    candidates = await ctx.router.route(
        ctx.store,
        cfg.available_models,
        cfg.providers,
        requested_model=body.model,
        thinking_required=body.thinking,
        budget_usd=body.budget_usd,
        spent_usd=0.0,
    )

    # 7/8. 流式 or 非流式
    if body.stream:
        return await _handle_stream(ctx, body, trace, messages, candidates)
    return await _handle_completion(ctx, body, trace, messages, candidates)


# ─────────────────────────────────────────────────────────────
# 非流式管道
# ─────────────────────────────────────────────────────────────
async def _handle_completion(
    ctx: AppContext, body: ChatCompletionRequest, trace: TraceFreezeRecord,
    messages: list[dict[str, Any]], candidates,
):
    schema = _resolve_schema(body)
    cfg = ctx.config_manager.config
    t_start = time.monotonic()

    def _adapter_for(candidate):
        return get_adapter(cfg.providers[candidate.provider], ctx.http_pool)

    async def call_one(candidate, probe=False, attempt=1):
        adapter = _adapter_for(candidate)
        result = await adapter.chat_complete(
            messages,
            candidate.upstream_model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            response_format=_upstream_response_format(body),
            trace_id=trace.trace_id,
        )
        # 结构化输出校验 + 自动修复（无效则触发重试/fallback）
        if schema is not None:
            _data, err = ctx.validator.validate_response(result.content, schema)
            if err is not None:
                raise StructuredOutputError(
                    f"Output failed validation for {candidate.provider}: {err}"
                )
        return result

    try:
        outcome = await ctx.retry.execute(ctx.store, candidates, call_one)
    except GatewayError as exc:
        await _record_failure(ctx, trace, exc, messages)
        raise

    elapsed_ms = (time.monotonic() - t_start) * 1000.0
    result = outcome.result
    provider_cfg = cfg.providers[outcome.provider]
    cost_usd = result.usage.total_tokens / 1_000_000.0 * provider_cfg.cost_per_million_tokens

    await ctx.store.record_usage(
        billing_id=trace.trace_id,
        trace_id=trace.trace_id,
        provider=outcome.provider,
        model=outcome.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        latency_ms=elapsed_ms,
        ttft_ms=result.ttft_ms,
        cost_usd=cost_usd,
        attempts=outcome.attempts,
        fallback_used=outcome.provider != candidates[0].provider,
        stream=False,
        user_id=body.user,
        project_id=body.project,
    )
    trace.update_metric("latency_ms", elapsed_ms) \
         .update_metric("provider", outcome.provider) \
         .update_metric("model", outcome.model) \
         .update_metric("cost_usd", cost_usd) \
         .update_metric("status", "success") \
         .update_metric("attempts", outcome.attempts)
    await ctx.store.record_trace(trace.snapshot())

    return ChatCompletionResponse(
        id=f"chatcmpl-{trace.trace_id[:24]}",
        created=int(trace.timestamp_us),
        model=outcome.model,
        choices=[
            ChoiceOut(
                message={"role": "assistant", "content": result.content},
                finish_reason=result.finish_reason,
            )
        ],
        usage=UsageOut(
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.total_tokens,
        ),
    )


# ─────────────────────────────────────────────────────────────
# 流式管道
# ─────────────────────────────────────────────────────────────
async def _handle_stream(
    ctx: AppContext, body: ChatCompletionRequest, trace: TraceFreezeRecord,
    messages: list[dict[str, Any]], candidates,
) -> StreamingResponse:
    schema = _resolve_schema(body)
    cfg = ctx.config_manager.config
    t_start = time.monotonic()

    async def event_stream() -> AsyncIterator[str]:
        stream_started = False
        last_error: Optional[GatewayError] = None
        attempts_total = 0
        success_provider: Optional[str] = None
        success_model: Optional[str] = None
        final_usage = None
        ttft: Optional[float] = None

        for ci, candidate in enumerate(candidates):
            decision = await ctx.circuit.can_pass_request(ctx.store, candidate.provider)
            if not decision.allowed:
                continue
            adapter = get_adapter(cfg.providers[candidate.provider], ctx.http_pool)

            for attempt in range(1, ctx.retry.policy.max_attempts + 1):
                attempts_total += 1
                agen = adapter.stream_chat(
                    messages,
                    candidate.upstream_model,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                    response_format=_upstream_response_format(body),
                    trace_id=trace.trace_id,
                )
                try:
                    async for ev in agen:
                        if ev.usage is not None:
                            final_usage = ev.usage
                            success_provider, success_model = candidate.provider, candidate.model_name
                            if ttft is None:
                                ttft = ev.ttft_ms
                            break
                        if ev.delta or ev.finish_reason:
                            if not stream_started and ev.ttft_ms is not None:
                                ttft = ev.ttft_ms
                            stream_started = True
                            chunk: dict[str, Any] = {
                                "id": f"chatcmpl-{trace.trace_id[:24]}",
                                "object": "chat.completion.chunk",
                                "created": int(trace.timestamp_us),
                                "model": candidate.model_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": ev.delta} if ev.delta else {},
                                    "finish_reason": ev.finish_reason,
                                }],
                            }
                            yield _sse(chunk)
                        # 不提前 break：继续消费直到 usage 汇总事件（保证计费真实）
                    if final_usage is not None:
                        break
                    # 流正常结束但无 usage 事件：兜底记录零用量
                    from ..adapters.base import Usage as _Usage
                    final_usage = _Usage(prompt_tokens=0, completion_tokens=0)
                    await ctx.circuit.record_success(ctx.store, candidate.provider)
                    success_provider, success_model = candidate.provider, candidate.model_name
                    break
                except asyncio.CancelledError:
                    await agen.aclose()
                    raise
                except GatewayError as exc:
                    await agen.aclose()
                    last_error = exc
                    if stream_started:
                        # 首 token 后断流：尝试后续候选/重试
                        break
                    if ctx.retry.is_retryable(exc) and attempt < ctx.retry.policy.max_attempts:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    await ctx.circuit.record_failure(ctx.store, candidate.provider)
                    break
                except Exception as exc:  # noqa: BLE001
                    await agen.aclose()
                    last_error = UpstreamError(str(exc))
                    break

            if final_usage is not None or success_provider is not None:
                break

        # ── 汇总 ─────────────────────────────────────────────
        if success_provider is not None and final_usage is not None:
            await ctx.circuit.record_success(ctx.store, success_provider)
            provider_cfg = cfg.providers[success_provider]
            cost_usd = final_usage.total_tokens / 1_000_000.0 * provider_cfg.cost_per_million_tokens
            elapsed_ms = (time.monotonic() - t_start) * 1000.0
            await ctx.store.record_usage(
                billing_id=trace.trace_id,
                trace_id=trace.trace_id,
                provider=success_provider,
                model=success_model or "",
                prompt_tokens=final_usage.prompt_tokens,
                completion_tokens=final_usage.completion_tokens,
                latency_ms=elapsed_ms,
                ttft_ms=ttft or 0.0,
                cost_usd=cost_usd,
                attempts=attempts_total,
                fallback_used=success_provider != candidates[0].provider,
                stream=True,
                user_id=body.user,
                project_id=body.project,
            )
            trace.update_metric("latency_ms", elapsed_ms) \
                 .update_metric("provider", success_provider) \
                 .update_metric("model", success_model) \
                 .update_metric("cost_usd", cost_usd) \
                 .update_metric("status", "success") \
                 .update_metric("attempts", attempts_total)
            await ctx.store.record_trace(trace.snapshot())
            yield _sse_done()
            return

        # ── 失败 ─────────────────────────────────────────────
        err = last_error or UpstreamError("All upstream candidates failed")
        await _record_failure(ctx, trace, err, messages)
        yield _error_event(err)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


# ─────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────
def _resolve_schema(body: ChatCompletionRequest) -> Optional[dict[str, Any]]:
    if body.response_format is None:
        return None
    if body.response_format.json_schema is not None:
        return body.response_format.json_schema.schema
    return {"type": "object"}  # json_object 模式：至少要求合法 JSON 对象


def _upstream_response_format(body: ChatCompletionRequest) -> Optional[dict[str, Any]]:
    """构造透传给上游的 response_format（供应商原生结构化输出约束）。"""
    if body.response_format is None:
        return None
    if body.response_format.type == "json_schema" and body.response_format.json_schema is not None:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": body.response_format.json_schema.name,
                "schema": body.response_format.json_schema.schema,
                "strict": True,
            },
        }
    return {"type": "json_object"}


async def _record_failure(
    ctx: AppContext, trace: TraceFreezeRecord, err: GatewayError, messages: list[dict[str, Any]]
) -> None:
    trace.update_metric("status", "error").update_metric("error_code", err.code)
    await ctx.store.record_trace(trace.snapshot())
    await ctx.store.record_error(trace.trace_id, "", err.code, err.message)
