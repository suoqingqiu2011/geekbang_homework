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
        rendered = await ctx.prompt_renderer.render(
            body.template_id, body.template_vars, version=body.template_version
        )
        messages = [{"role": "system", "content": rendered}] + [
            m for m in messages if m["role"] != "system"
        ]

    # 6. 路由
    cfg = ctx.config_manager.config
    # ★ 缺陷C 修复：预算路由使用项目累计真实花费（而非硬编码 0.0），
    #   并按 max_tokens 预估候选最大成本核算，防止实际用量超支。
    spent_usd = await ctx.store.query_spent_usd(body.project) if body.budget_usd > 0 else 0.0
    candidates = await ctx.router.route(
        ctx.store,
        cfg.available_models,
        cfg.providers,
        requested_model=body.model,
        thinking_required=body.thinking,
        budget_usd=body.budget_usd,
        spent_usd=spent_usd,
        max_tokens=body.max_tokens,
    )

    # 7/8. 流式 or 非流式
    if body.stream:
        return await _handle_stream(ctx, request, body, trace, messages, candidates)
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
        capability = cfg.providers[candidate.provider].structured_output
        upstream_rf = _upstream_response_format(body, capability)
        upstream_msgs = _upstream_messages(messages, upstream_rf, schema)
        try:
            result = await adapter.chat_complete(
                upstream_msgs,
                candidate.upstream_model,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                response_format=upstream_rf,
                trace_id=trace.trace_id,
            )
        except UpstreamError as exc:
            if exc.http_status == 400 and upstream_rf is not None:
                # 上游不支持当前 response_format（如供应商能力变更/降级仍 400）：
                # 去掉 response_format 后重试一次（降级容错兜底）。
                # ★ 修复：原 messages 可能不含 JSON 相关词汇，需额外注入提示词确保
                #   无 response_format 的上游仍能输出合法 JSON（否则 validator 报
                #   "invalid_json" 导致整个结构化输出请求失败）。
                retry_msgs = _upstream_messages(messages, {"type": "json_object"}, schema)
                logger.warning(
                    "Upstream 400 with response_format, retrying without it + json hint (provider=%s)",
                    candidate.provider,
                )
                result = await adapter.chat_complete(
                    retry_msgs,
                    candidate.upstream_model,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                    response_format=None,
                    trace_id=trace.trace_id,
                )
            else:
                raise
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
    if result.usage.estimated:
        cost_usd = 0.0  # 估算行（无上游 usage/字符数兜底）不计入真实计费

    # ★ 缺陷C 修复：调用后按实际 token 用量核算——本次真实花费超出请求预算时
    #   记录 budget_exceeded 观测（费用已发生无法撤销，但可审计并让后续请求被拦截）。
    if body.budget_usd > 0 and cost_usd > body.budget_usd:
        trace.update_metric("budget_exceeded", True)
        logger.warning(
            "Request over budget: cost=$%.6f > budget=$%.6f (trace=%s provider=%s)",
            cost_usd, body.budget_usd, trace.trace_id, outcome.provider,
        )

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
        complete=result.usage.complete,
        estimated=result.usage.estimated,
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
            complete=result.usage.complete,
            estimated=result.usage.estimated,
        ),
    )


# ─────────────────────────────────────────────────────────────
# 流式管道
# ─────────────────────────────────────────────────────────────
async def _handle_stream(
    ctx: AppContext, request: Request, body: ChatCompletionRequest, trace: TraceFreezeRecord,
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
        stream_chars = 0  # 已向客户端输出的字符数（半程观测）
        partial_recorded = False  # 是否已固化"部分输出后失败"观测
        abort_candidate = False  # 是否应中止当前候选（失败=不再重试，转下一候选）

        # 方案B：注册客户端断连的主动轮询信号（兜底），与上游流任务共享取消信号，
        # 阻塞上游读取期间也能及时感知断连。正常终/取消时在 finally 注销，防 task 泄漏。
        signal = await ctx.cancellation.register_signal(trace.trace_id, request)
        try:
            for ci, candidate in enumerate(candidates):
                # 候选级 TTFT 锚定：每次切换候选重置，避免上一失败候选的首 token TTFT
                # 污染成功候选的归因（#4 修复）。
                candidate_first_token = True
                ttft = None
                decision = await ctx.circuit.can_pass_request(ctx.store, candidate.provider)
                if not decision.allowed:
                    continue
                adapter = get_adapter(cfg.providers[candidate.provider], ctx.http_pool)
                # 能力门控：按 provider 能力降级 response_format + 补充 json 提示词
                capability = cfg.providers[candidate.provider].structured_output
                upstream_rf = _upstream_response_format(body, capability)
                upstream_msgs = _upstream_messages(messages, upstream_rf, schema)
                rf_degraded = False  # 是否已"去掉 response_format"重试一次（降级容错兜底）

                for attempt in range(1, ctx.retry.policy.max_attempts + 1):
                    attempts_total += 1
                    # ★ 每次尝试（含同候选重试）重置首 token 锚点：
                    #   上一尝试输出首 token 后失败，重试时会继续保留旧 ttft，
                    #   导致 TTFT 归因到失败的尝试而非本次成功候选（同候选内跨尝试 bug）。
                    candidate_first_token = True
                    agen = adapter.stream_chat(
                        upstream_msgs,
                        candidate.upstream_model,
                        temperature=body.temperature,
                        max_tokens=body.max_tokens,
                        response_format=upstream_rf,
                        trace_id=trace.trace_id,
                    )
                    try:
                        while True:
                            # 主动轮询兜底：断连信号已触发则立即停止阻塞上游读取
                            if signal.cancelled:
                                await agen.aclose()
                                await _record_cancelled_trace(
                                    ctx, trace, candidate.provider, candidate.model_name,
                                    stream_chars, stream_started,
                                )
                                raise asyncio.CancelledError
                            ev, disconnected = await _anext_or_disconnect(agen, signal)
                            if disconnected:
                                await agen.aclose()
                                await _record_cancelled_trace(
                                    ctx, trace, candidate.provider, candidate.model_name,
                                    stream_chars, stream_started,
                                )
                                raise asyncio.CancelledError
                            if ev.usage is not None:
                                final_usage = ev.usage
                                success_provider, success_model = candidate.provider, candidate.model_name
                                if ttft is None:
                                    ttft = ev.ttft_ms
                                break
                            if ev.delta or ev.finish_reason:
                                if candidate_first_token and ev.ttft_ms is not None:
                                    ttft = ev.ttft_ms
                                candidate_first_token = False
                                stream_started = True
                                if ev.delta:
                                    stream_chars += len(str(ev.delta))
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
                    except StopAsyncIteration:
                        # 流正常结束但无 usage 事件：标记为不完整 + 估算（而非默认真实完整）
                        from ..adapters.base import Usage as _Usage
                        final_usage = _Usage(prompt_tokens=0, completion_tokens=0, complete=False, estimated=True)
                        await ctx.circuit.record_success(ctx.store, candidate.provider)
                        success_provider, success_model = candidate.provider, candidate.model_name
                    except asyncio.CancelledError:
                        await agen.aclose()
                        # 半程取消观测固化：客户端中途断开/取消仍留痕
                        await _record_cancelled_trace(
                            ctx, trace, candidate.provider, candidate.model_name,
                            stream_chars, stream_started,
                        )
                        raise
                    except GatewayError as exc:
                        await agen.aclose()
                        last_error = exc
                        if stream_started:
                            # 首 token 后断流：先固化"部分输出后失败"的半程观测，
                            # 再尝试后续候选/重试（区别于 cancel，语义为 partial_failed）。
                            # ★ 部分失败同样计入熔断：避免"能吐字但总中途失败"的
                            #   半死不活 provider 永远不被降级、持续制造 partial_failed。
                            await _record_partial_failure_trace(
                                ctx, trace, candidate.provider, candidate.model_name,
                                stream_chars, True, exc,
                            )
                            partial_recorded = True
                            await ctx.circuit.record_failure(ctx.store, candidate.provider)
                            abort_candidate = True
                            break
                        if ctx.retry.is_retryable(exc) and attempt < ctx.retry.policy.max_attempts:
                            # 与 RetryPolicy 保持一致（指数+抖动），而非线性退避
                            await asyncio.sleep(ctx.retry.policy.backoff_for(attempt))
                            continue
                        if (
                            isinstance(exc, UpstreamError)
                            and exc.http_status == 400
                            and upstream_rf is not None
                            and not rf_degraded
                        ):
                            # 上游不支持当前 response_format（能力降级仍 400）：
                            # 去掉 response_format 后重试一次（降级容错兜底）。
                            # ★ 与 _handle_completion 一致：重试时注入 JSON 提示词，
                            #   否则无 response_format 的上游可能输出纯文本，validator
                            #   解析失败导致整个结构化输出请求失败。
                            logger.warning(
                                "Upstream 400 with response_format, retrying without it + json hint (provider=%s)",
                                candidate.provider,
                            )
                            upstream_rf = None
                            upstream_msgs = _upstream_messages(messages, {"type": "json_object"}, schema)
                            rf_degraded = True
                            continue
                        await ctx.circuit.record_failure(ctx.store, candidate.provider)
                        abort_candidate = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        await agen.aclose()
                        # 兼容 Starlette/anyio 把取消包装为异常组（BaseExceptionGroup）
                        if isinstance(exc, BaseExceptionGroup) and _is_cancellation(exc):
                            await _record_cancelled_trace(
                                ctx, trace, candidate.provider, candidate.model_name,
                                stream_chars, stream_started,
                            )
                            raise exc  # noqa: TRY201 - 保留取消语义
                        last_error = UpstreamError(str(exc))
                        if stream_started:
                            # 已吐字后断流（如 httpx 连接中断/上游异常）：固化半程观测，
                            # 并计入熔断失败（半死不活的上游不应长期豁免降级）
                            await _record_partial_failure_trace(
                                ctx, trace, candidate.provider, candidate.model_name,
                                stream_chars, True, last_error,
                            )
                            partial_recorded = True
                            await ctx.circuit.record_failure(ctx.store, candidate.provider)
                            abort_candidate = True
                        break

                    if final_usage is not None or success_provider is not None or abort_candidate:
                        break

                # ★ 缺陷A 修复：流式成功（收到 usage / 流正常结束）后整体跳出候选循环，
                #   不再向后续 fallback 候选发起流式请求——否则客户端会收到多 provider
                #   拼接的输出，且 usage/TTFT 归因被后续候选覆盖。仅 abort_candidate
                #   （失败）时继续尝试下一候选。
                if success_provider is not None:
                    break

            # ── 汇总 ─────────────────────────────────────────────
            if success_provider is not None and final_usage is not None:
                await ctx.circuit.record_success(ctx.store, success_provider)
                provider_cfg = cfg.providers[success_provider]
                cost_usd = final_usage.total_tokens / 1_000_000.0 * provider_cfg.cost_per_million_tokens
                if final_usage.estimated:
                    cost_usd = 0.0  # 估算行（无上游 usage/字符数兜底）不计入真实计费

                # ★ 缺陷C 修复：流式同样按实际 token 用量核算，超预算记录观测
                if body.budget_usd > 0 and cost_usd > body.budget_usd:
                    trace.update_metric("budget_exceeded", True)
                    logger.warning(
                        "Stream request over budget: cost=$%.6f > budget=$%.6f (trace=%s provider=%s)",
                        cost_usd, body.budget_usd, trace.trace_id, success_provider,
                    )

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
                    complete=final_usage.complete,
                    estimated=final_usage.estimated,
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
            await _record_failure(ctx, trace, err, messages, already_partial=partial_recorded)
            yield _error_event(err)
        finally:
            # 注销断连监听任务，防止 background task 泄漏
            await ctx.cancellation.unregister(trace.trace_id, fire=False)

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


def _upstream_response_format(
    body: ChatCompletionRequest, capability: str
) -> Optional[dict[str, Any]]:
    """构造透传给上游的 response_format（供应商原生结构化输出约束）。

    按 provider 能力门控降级（本地 validator 为唯一校验权威）：
      - capability=json_schema（OpenAI）    → 原样透传 schema 约束
      - capability=json_object（DeepSeek/Kimi/Qwen/Ollama）→ 降级为 json_object，
        prompt 由 _upstream_messages 补充 "json" 字样（DeepSeek 硬性要求）
      - capability=none（Anthropic）        → 不转发原生约束，仅保留 schema 名
        供 Anthropic 适配器生成提示约束（anthropic.py _build_body）
    """
    if body.response_format is None:
        return None
    if body.response_format.type == "json_object":
        return {"type": "json_object"}
    if capability == "json_schema":
        schema = body.response_format.json_schema
        if schema is None:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.name,
                "schema": schema.schema,
                "strict": True,
            },
        }
    if capability == "none":
        name = body.response_format.json_schema.name if body.response_format.json_schema else "json"
        return {"type": "json_schema", "json_schema": {"name": name}}
    return {"type": "json_object"}


def _upstream_messages(
    messages: list[dict[str, Any]],
    upstream_rf: Optional[dict[str, Any]],
    schema: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """为降级为 json_object 的上游补充 JSON 提示词。

    DeepSeek 的 json_object 模式硬性要求 prompt 中出现 "json" 字样；
    对支持 json_schema 的供应商同样无害。纯提示约束（Anthropic）时
    上游也带 json 提示，与适配器注入的 schema 提示互补。

    ★ schema 非空时注入完整 schema 定义（字段名/必填项）：json_object
      模式只保证输出合法 JSON，不保证字段名匹配调用方要求（如缺 intent
      字段导致 validator 校验失败）；显式告知模型目标结构可显著提升命中率。
    """
    if upstream_rf is None:
        return messages
    if schema is not None:
        hint = (
            "You must output a single valid JSON object that strictly matches this "
            f"JSON Schema (no extra text, all required fields present): "
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
    elif "json" in json.dumps(messages, ensure_ascii=False).lower():
        return messages
    else:
        hint = "You must output a single valid JSON object without any extra text."
    return [*messages, {"role": "system", "content": hint}]


def _is_cancellation(exc: BaseException) -> bool:
    """识别"请求被客户端取消/断连"。兼容纯 CancelledError 与 Starlette/anyio
    结构化并发下被包装为 BaseExceptionGroup 的取消异常。"""
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_cancellation(e) for e in exc.exceptions)
    return False


async def _anext_or_disconnect(agen, signal) -> tuple[Optional[Any], bool]:
    """取下一个上游事件，与断连信号竞争（主动轮询兜底，方案B）。

    - 上游先返回事件 → `(ev, False)`
    - 断连信号先触发 → `(None, True)`（此时上游读取任务已被取消）
    - 上游正常流结束 → 抛 `StopAsyncIteration`

    即便阻塞在 `agen.__anext__()` 上，也能被断连信号打断，从而及时断开上游连接。
    """
    next_task = asyncio.ensure_future(agen.__anext__())
    sig_task = asyncio.ensure_future(signal.wait())
    try:
        done, pending = await asyncio.wait(
            {next_task, sig_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        # 无论正常竞争还是外部取消，都确保两个子任务无泄漏
        for t in (next_task, sig_task):
            if not t.done():
                t.cancel()
    if sig_task in done:
        # 断连已触发：返回；未完成的上游读取已被 finally 取消
        return (None, True)
    # 上游出了新事件或已结束（StopAsyncIteration 原样上抛）
    return (await next_task, False)


async def _record_cancelled_trace(
    ctx: AppContext,
    trace: TraceFreezeRecord,
    provider: str,
    model: str,
    stream_chars: int,
    stream_started: bool,
) -> None:
    """流中途取消的半程观测固化：落一条 status=cancelled 的 trace。

    取消不代表失败，故不计入 error，但不该完全不留痕；同时取消不产生
    usage 事件 → 天然不计费，无需在此处理计费。
    """
    trace.update_metric("status", "cancelled") \
         .update_metric("provider", provider) \
         .update_metric("model", model) \
         .update_metric("partial", True) \
         .update_metric("stream_chars", stream_chars) \
         .update_metric("stream_started", stream_started)
    await ctx.store.record_trace(trace.snapshot())


async def _record_partial_failure_trace(
    ctx: AppContext,
    trace: TraceFreezeRecord,
    provider: str,
    model: str,
    stream_chars: int,
    stream_started: bool,
    err: GatewayError,
) -> None:
    """部分输出后失败（先吐字再断流）的半程观测固化：落 status=partial_failed。

    与取消（cancelled，无 error_code）不同：该场景确实失败、携带 error_code，
    但已向客户端吐出部分内容，需保留半程指标（partial / stream_chars）。
    此路径无 usage 汇总事件 → 不计费，无需额外计费处理。
    """
    trace.update_metric("status", "partial_failed") \
         .update_metric("partial", True) \
         .update_metric("provider", provider) \
         .update_metric("model", model) \
         .update_metric("stream_chars", stream_chars) \
         .update_metric("stream_started", stream_started) \
         .update_metric("error_code", err.code)
    await ctx.store.record_trace(trace.snapshot())


async def _record_failure(
    ctx: AppContext, trace: TraceFreezeRecord, err: GatewayError, messages: list[dict[str, Any]],
    already_partial: bool = False,
) -> None:
    if already_partial:
        # 半程已固化 partial_failed 观测：不再用 error 覆盖（避免丢失"先吐后失败"信息），
        # 仅补充错误明细
        await ctx.store.record_error(trace.trace_id, "", err.code, err.message)
        return
    trace.update_metric("status", "error").update_metric("error_code", err.code)
    await ctx.store.record_trace(trace.snapshot())
    await ctx.store.record_error(trace.trace_id, "", err.code, err.message)
