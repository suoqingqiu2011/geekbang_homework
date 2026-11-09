"""
/v1/chat/completions + /v1/models — 网关主入口（SPEC §5 / §6）。

请求管道：AuthCheck → RateLimiter → Pydantic → Trace 固化 → 注入检测
→ 模板渲染 → Router → Retry/Fallback → Adapter → Validator → 幂等计费 → 响应。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import uuid
from functools import lru_cache
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..adapters.registry import get_adapter
from ..context import AppContext
from ..core.config_manager import AppConfig
from ..core.retry_fallback import RetryPolicy
from ..core.router import estimate_prompt_tokens
from ..core.trace_freeze import TraceFreezeRecord
from ..errors import (
    GatewayError,
    InvalidRequestError,
    RateLimitExceededError,
    StructuredOutputError,
    UpstreamError,
    UpstreamTimeoutError,
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


@lru_cache(maxsize=16)
def _trusted_networks(specs: tuple[str, ...]) -> tuple[Any, ...]:
    """把 trusted_proxies 解析成网段（缓存，避免每请求重复解析）。

    支持单 IP 与 CIDR；非法条目跳过并告警，避免因一处配置错误影响全部请求。
    """
    nets: list[Any] = []
    for spec in specs:
        try:
            nets.append(ipaddress.ip_network(spec, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid trusted_proxies entry: %s", spec)
    return tuple(nets)


def _is_trusted(ip: str, nets: tuple[Any, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in nets)


def _client_ip(request: Request, cfg: AppConfig) -> str:
    """取客户端 IP，默认不信任 X-Forwarded-For，防止审计字段被伪造。

    仅当 ``trust_proxy_headers`` 开启、且直连对端命中 ``trusted_proxies`` 时才解析 XFF；
    解析时从右向左跳过可信代理，返回第一个不可信地址——右侧由代理追加，左侧可被客户端
    任意伪造，所以绝不能直接取最左值。
    """
    peer = request.client.host if request.client else ""
    if not cfg.trust_proxy_headers:
        return peer or "unknown"

    nets = _trusted_networks(tuple(cfg.trusted_proxies))
    if not nets or not _is_trusted(peer, nets):
        # 直连来源本身不可信（或未配置可信网段）→ 忽略 XFF
        return peer or "unknown"

    hops = [
        hop.strip()
        for hop in request.headers.get("x-forwarded-for", "").split(",")
        if hop.strip()
    ]
    for hop in reversed(hops):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            logger.warning("Ignoring invalid X-Forwarded-For entry: %s", hop)
            continue
        if not _is_trusted(hop, nets):
            return str(addr)
    # 所有 hop 都是可信代理（或全部非法）→ 没有可信的客户端地址，回落对端
    return peer or "unknown"


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
def _tightest_budget(bound: float, requested: float) -> float:
    """取"更严"的预算：服务端绑定配额是硬上限，客户端只能收紧。

    0 表示"不限制"（SPEC §5.2），因此 0 不参与取最小值；
    两者都为 0 时才真正不限制。
    """
    limits = [value for value in (bound, requested) if value > 0]
    return min(limits) if limits else 0.0


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """请求入口：统一记录请求量/耗时指标后转交管道实现。"""
    ctx: AppContext = request.app.state.ctx
    t0 = time.monotonic()
    status = "success"
    try:
        return await _chat_completions_impl(request, body, ctx)
    except GatewayError as exc:
        status = exc.code
        raise
    except Exception:
        status = "internal_error"
        raise
    finally:
        ctx.metrics.record_request(
            "chat/completions", status, time.monotonic() - t0
        )


async def _chat_completions_impl(
    request: Request, body: ChatCompletionRequest, ctx: AppContext
):
    # 1. 鉴权
    key = await ctx.auth.check(
        request.headers.get("authorization"), ctx.config_manager.get_api_keys
    )

    # ★ 服务端主体绑定（SPEC §4.1 / §8.1）：绑定 Key 的 user/project 以服务端为准，
    #   预算取"服务端硬配额与客户端请求值中更严者"，避免客户端换 project 重置
    #   预算/限流桶，或直接把 budget_usd 填 0 绕过成本管控。
    binding = ctx.config_manager.get_key_binding(key)
    if binding is not None:
        body.user = binding.user
        body.project = binding.project
        body.budget_usd = _tightest_budget(binding.budget_usd, body.budget_usd)

    # 2. 限流（per-model > per-endpoint > per-project > per-user）
    allowed, retry_after, layer = await ctx.rate_limiter.check(
        model=body.model,
        endpoint_key="POST:/v1/chat/completions",
        project_id=body.project,
        user_id=body.user,
    )
    if not allowed:
        ctx.metrics.inc_rate_limit_rejected()
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
        client_ip=_client_ip(request, ctx.config_manager.config),
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
        prompt_tokens_estimate=estimate_prompt_tokens(messages),
    )

    # ★ M1 修复：model 缺省（自动选路）时首检无法确定模型，model 层被跳过；
    #   路由确定首选模型后补检 per-model 层（只扣 model 桶，不重复扣其他层）。
    if not body.model:
        allowed, retry_after, layer = await ctx.rate_limiter.check_model(
            candidates[0].model_name
        )
        if not allowed:
            ctx.metrics.inc_rate_limit_rejected()
            raise RateLimitExceededError(
                f"Rate limit exceeded (layer={layer}). Retry after {retry_after:.1f}s.",
                retry_after=retry_after,
            )

    # 7/8. 流式 or 非流式
    if body.stream:
        # 可选缓冲模式：先完成校验/修复，再一次性以 SSE 下发；默认行为保持不变。
        if body.buffer_until_valid:
            return await _handle_buffered_stream(ctx, body, trace, messages, candidates)
        return await _handle_stream(ctx, request, body, trace, messages, candidates)
    return await _handle_completion(ctx, body, trace, messages, candidates)


# ─────────────────────────────────────────────────────────────
# 非流式管道
# ─────────────────────────────────────────────────────────────
async def _handle_completion(
    ctx: AppContext, body: ChatCompletionRequest, trace: TraceFreezeRecord,
    messages: list[dict[str, Any]], candidates,
    *,
    normalize_content: bool = False,
    stream_delivery: bool = False,
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
            ctx.metrics.inc_upstream_attempt()
            result = await adapter.chat_complete(
                upstream_msgs,
                candidate.upstream_model,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                top_p=body.top_p,
                stop=body.stop,
                presence_penalty=body.presence_penalty,
                frequency_penalty=body.frequency_penalty,
                seed=body.seed,
                logprobs=body.logprobs,
                top_logprobs=body.top_logprobs,
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
                ctx.metrics.inc_upstream_attempt()
                result = await adapter.chat_complete(
                    retry_msgs,
                    candidate.upstream_model,
                    temperature=body.temperature,
                    max_tokens=body.max_tokens,
                    top_p=body.top_p,
                    stop=body.stop,
                    presence_penalty=body.presence_penalty,
                    frequency_penalty=body.frequency_penalty,
                    seed=body.seed,
                    logprobs=body.logprobs,
                    top_logprobs=body.top_logprobs,
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
            # 缓冲模式：下发前统一序列化校验后的数据，确保客户端拿到的一定是合法 JSON
            # （含 validator 通过平衡块提取修复的情况）。
            if normalize_content:
                result.content = json.dumps(_data, ensure_ascii=False, separators=(",", ":"))
        return result

    async def on_attempt_failure(candidate, err):
        await _record_unbilled_cost(
            ctx, trace, err, messages, candidate.provider, candidate.model_name
        )

    try:
        outcome = await ctx.retry.execute(
            ctx.store, candidates, call_one, on_attempt_failure=on_attempt_failure
        )
    except GatewayError as exc:
        await _record_failure(ctx, trace, exc, messages)
        raise

    elapsed_ms = (time.monotonic() - t_start) * 1000.0
    result = outcome.result
    provider_cfg = cfg.providers[outcome.provider]
    cost_prompt, cost_completion = provider_cfg.resolve_effective_costs()
    cost_usd = (
        result.usage.prompt_tokens / 1_000_000.0 * cost_prompt
        + result.usage.completion_tokens / 1_000_000.0 * cost_completion
    )
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
        billing_id=trace.billing_id,
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
        stream=stream_delivery,
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
        id=_completion_id(trace.trace_id),
        created=int(trace.timestamp_us),
        model=outcome.model,
        choices=[
            ChoiceOut(
                message={"role": "assistant", "content": result.content},
                finish_reason=result.finish_reason,
                logprobs=result.logprobs,
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
# 缓冲式流式管道（stream + response_format + buffer_until_valid）
# ─────────────────────────────────────────────────────────────
async def _handle_buffered_stream(
    ctx: AppContext, body: ChatCompletionRequest, trace: TraceFreezeRecord,
    messages: list[dict[str, Any]], candidates,
) -> StreamingResponse:
    """先完成校验/修复/计费，再把已验证结果以 SSE 一次性下发。

    适用于“宁可不流式，也不能先收到无效内容”的结构化输出场景。
    """
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    try:
        result = await _handle_completion(
            ctx, body, trace, messages, candidates,
            normalize_content=True,
            stream_delivery=True,
        )
    except GatewayError as exc:
        # except 作用域结束后 exc 会被删除，先绑定到局部变量再供生成器闭包使用
        error = exc

        async def _error_events() -> AsyncIterator[str]:
            yield _error_event(error)

        return StreamingResponse(
            _error_events(), media_type="text/event-stream", headers=headers
        )

    content = str(result.choices[0].message.get("content") or "")
    finish_reason = result.choices[0].finish_reason
    base: dict[str, Any] = {
        "id": result.id,
        "object": "chat.completion.chunk",
        "created": result.created,
        "model": result.model,
    }

    async def _events() -> AsyncIterator[str]:
        yield _sse({
            **base,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }],
        })
        yield _sse({
            **base,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        })
        # 内容在发送前已通过校验，故 status 恒为 ok
        yield _sse({
            **base,
            "type": "validation",
            "status": "ok",
            "error": None,
            "choices": [],
        })
        yield _sse_done()

    return StreamingResponse(
        _events(), media_type="text/event-stream", headers=headers
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
        content_parts: list[str] = []  # 累积 delta 内容（仅结构化输出流式收尾校验用；schema=None 不累积）
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
                content_parts = []  # 候选级重置：失败候选残留内容不得污染成功候选的收尾校验
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
                    ctx.metrics.inc_upstream_attempt()
                    agen = adapter.stream_chat(
                        upstream_msgs,
                        candidate.upstream_model,
                        temperature=body.temperature,
                        max_tokens=body.max_tokens,
                        top_p=body.top_p,
                        stop=body.stop,
                        presence_penalty=body.presence_penalty,
                        frequency_penalty=body.frequency_penalty,
                        seed=body.seed,
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
                                    if schema is not None:
                                        content_parts.append(str(ev.delta))
                                chunk: dict[str, Any] = {
                                    "id": _completion_id(trace.trace_id),
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
                        if getattr(exc, "config_missing", False):
                            # 配置缺失（Key 未设置）：不重试、不计熔断，跳下一候选
                            #（与 retry_fallback.execute 语义一致，避免误开熔断）
                            last_error = UpstreamError(
                                f"Upstream authentication failed ({candidate.provider})",
                                http_status=502,
                            )
                            logger.warning(
                                "Skip candidate %s (missing api key)", candidate.provider
                            )
                            break
                        if stream_started:
                            # 首 token 后断流：先固化"部分输出后失败"的半程观测，
                            # 再尝试后续候选/重试（区别于 cancel，语义为 partial_failed）。
                            # ★ 部分失败同样计入熔断：避免"能吐字但总中途失败"的
                            #   半死不活 provider 永远不被降级、持续制造 partial_failed。
                            await _record_partial_failure_trace(
                                ctx, trace, candidate.provider, candidate.model_name,
                                stream_chars, True, exc,
                            )
                            # 观察模式：已吐字（上游已计费）后失败，记录可能产生的花费
                            await _record_unbilled_cost(
                                ctx, trace, exc, messages,
                                candidate.provider, candidate.model_name,
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
                            await _record_unbilled_cost(
                                ctx, trace, last_error, messages,
                                candidate.provider, candidate.model_name,
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
                cost_prompt, cost_completion = provider_cfg.resolve_effective_costs()
                cost_usd = (
                    final_usage.prompt_tokens / 1_000_000.0 * cost_prompt
                    + final_usage.completion_tokens / 1_000_000.0 * cost_completion
                )
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
                    billing_id=trace.billing_id,
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

                # ★ 流式结构化输出收尾校验：usage 汇总事件后对拼接内容做 validator 校验。
                #   非流式路径在返回前校验+自动修复；流式内容已逐块透传客户端、无法自动修复，
                #   故仅追加一个校验状态事件供客户端判断，并落 validation_failed 观测供审计。
                #   schema=None（普通对话流）：不累积、不发校验事件，保持 OpenAI 兼容事件序列。
                if schema is not None:
                    verr: Optional[str] = None
                    _data, verr = ctx.validator.validate_response(
                        "".join(content_parts), schema
                    )
                    if verr is not None:
                        trace.update_metric("validation_failed", True) \
                             .update_metric("validation_error", verr)
                        logger.warning(
                            "Stream structured output failed post-hoc validation "
                            "(trace=%s provider=%s error=%s)",
                            trace.trace_id, success_provider, verr,
                        )
                await ctx.store.record_trace(trace.snapshot())
                if schema is not None:
                    yield _sse({
                        "id": _completion_id(trace.trace_id),
                        "object": "chat.completion.chunk",
                        "created": int(trace.timestamp_us),
                        "model": success_model,
                        "type": "validation",
                        "status": "ok" if verr is None else "error",
                        "error": verr,
                        "choices": [],
                    })
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
def _completion_id(trace_id: str) -> str:
    """OpenAI 兼容响应 id：chatcmpl-<24 位 hex>。

    trace_id 为 UUID（含连字符），直接截断会留下尾部裸连字符
    （如 chatcmpl-72648c9c-c659-4d56-accb-）：先去连字符再截位。
    """
    return f"chatcmpl-{trace_id.replace('-', '')[:24]}"


def _assert_local_schema_refs(node: Any) -> None:
    """拒绝客户端 schema 中的外部 $ref（SSRF 前置于请求上游之前）。

    本地指针 `#/...` 是合法用法；任何非 `#` 开头的 `$ref` 都可能在
    jsonschema 校验时触发远程抓取，故在管道入口直接拒绝（400），
    避免先付费调用上游再发现 schema 不可用。
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            raise InvalidRequestError(
                "response_format.json_schema may only use local '#/...' $ref references"
            )
        for value in node.values():
            _assert_local_schema_refs(value)
    elif isinstance(node, list):
        for value in node:
            _assert_local_schema_refs(value)


def _resolve_schema(body: ChatCompletionRequest) -> Optional[dict[str, Any]]:
    if body.response_format is None:
        return None
    if body.response_format.json_schema is not None:
        schema = body.response_format.json_schema.schema_
        _assert_local_schema_refs(schema)
        return schema
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
                "schema": schema.schema_,
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


# 上游明确拒绝的状态码：通常不产生费用，记入观察表会高估漏算规模。
# 429/400/401/403/404/422 都属于"请求未被计费处理"。
_NOT_BILLED_STATUS = {400, 401, 403, 404, 422, 429}


def _should_record_unbilled(err: GatewayError) -> bool:
    """判断失败尝试是否可能已被上游计费（决定是否写入观察表）。"""
    if getattr(err, "config_missing", False):
        return False  # 未发出请求（缺 Key）
    if isinstance(err, StructuredOutputError):
        return True  # 上游已 200 返回并计费，只是本地校验失败
    if isinstance(err, UpstreamTimeoutError):
        return True  # 超时：上游可能已完成并计费
    if isinstance(err, UpstreamError):
        return getattr(err, "http_status", 502) not in _NOT_BILLED_STATUS
    return False


async def _record_unbilled_cost(
    ctx: AppContext, trace: TraceFreezeRecord, err: GatewayError,
    messages: list[dict[str, Any]], provider: str, model: str,
) -> None:
    """观察模式：记录失败请求可能已产生的上游花费（不改变账目与预算判定）。

    失败尝试拿不到上游 usage，只能按 prompt 本地估算、completion 记 0，
    因此该数据仅用于评估漏算规模；是否纳入预算需另行确认。
    """
    if not _should_record_unbilled(err):
        return
    try:
        prompt_tokens = estimate_prompt_tokens(messages)
        provider_cfg = ctx.config_manager.config.providers.get(provider)
        cost_prompt = provider_cfg.resolve_effective_costs()[0] if provider_cfg else 0.0
        await ctx.store.record_unbilled_attempt_cost(
            billing_id=trace.billing_id,
            trace_id=trace.trace_id,
            project_id=trace.project_id,
            provider=provider,
            model=model,
            reason=err.code,
            prompt_tokens=prompt_tokens,
            cost_usd=prompt_tokens / 1_000_000.0 * cost_prompt,
        )
    except Exception as exc:  # noqa: BLE001 - 观测失败不得影响错误响应
        logger.warning("Failed to record unbilled attempt cost: %s", exc)
