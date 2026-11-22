"""
RetryFallbackManager — 重试 + fallback/failover（SPEC §4.7 / §9）。

规则（非流式与流式共用同一份决策逻辑）：
  - 仅可重试错误重试：429、5xx、超时（退避 + 抖动）
  - 认证失败 / 非法请求：不重试，直接切换候选
  - 上游不支持 response_format（400）：不消耗 attempt，降级后重试同一 attempt
  - 候选链逐个尝试；单候选重试耗尽 → 切换下一候选（fallback）
  - 每次候选终态同步熔断器状态
  - 流式一旦已向客户端下发字节，失败即终止（不回退拼接）

流式驱动以 async generator 暴露生命周期事件：manager 持有候选链/重试/熔断
状态机，路由层只负责把 delta 转 SSE、计费与 trace 终态落库。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..adapters.base import Usage
from ..errors import (
    GatewayError,
    InvalidAuthError,
    UpstreamError,
    UpstreamTimeoutError,
)
from ..storage.metrics_store import MetricsStore
from .cancellation import ClientDisconnected, is_cancellation
from .circuit_breaker import CircuitBreakerManager

logger = logging.getLogger("llm-gateway.retry-fallback")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_backoff: float = 0.5
    max_backoff: float = 8.0
    jitter: float = 0.2

    def backoff_for(self, attempt: int) -> float:
        """第 attempt 次的退避秒数：指数 + 抖动，且受 max_backoff 截断。

        作为重试退避的**单一事实来源**（execute 与 stream 共用），
        保证两路退避策略一致。
        """
        if attempt < 1:
            return 0.0
        exp = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
        return exp * (1 + random.uniform(0, self.jitter))


class FailureAction(str, Enum):
    """一次失败后的统一动作。"""

    RETRY = "retry"                    # 同候选退避后重试（消耗 attempt）
    DEGRADE_RF = "degrade_rf"          # 去掉 response_format 重试同一 attempt（不消耗 attempt）
    NEXT_CANDIDATE = "next_candidate"  # 切换下一候选
    ABORT_STREAM = "abort_stream"      # 已向客户端下发字节，终止请求（不 fallback）


@dataclass
class FailureDecision:
    """失败决策结果；由 execute/stream 两个驱动器解释执行。"""

    action: FailureAction
    error: GatewayError
    backoff: float = 0.0
    record_circuit_failure: bool = False


@dataclass
class CallOutcome:
    """一次候选链执行的最终结果。"""

    provider: str
    model: str
    attempts: int = 1
    success: bool = False
    result: object = None            # 非流式：AdapterResult
    error: Optional[GatewayError] = None
    circuit_reason: str = ""
    ttft_ms: float = 0.0
    usage: Optional[Usage] = None    # 流式：终态 usage
    finish_reason: str = "stop"      # 流式：终态 finish_reason
    partial: bool = False            # 流式：是否部分下发后终止


@dataclass
class StreamItem:
    """流式状态机产出的生命周期事件。"""

    kind: str                        # attempt_started | delta | partial_failed | finished
    candidate: Any = None
    event: Any = None
    outcome: Optional[CallOutcome] = None
    error: Optional[GatewayError] = None
    attempt: int = 0
    attempts_total: int = 0
    stream_chars: int = 0


class RetryFallbackManager:
    """重试 + 降级链执行器（候选链/重试/熔断记账的唯一状态机）。"""

    def __init__(self, circuit: CircuitBreakerManager, policy: Optional[RetryPolicy] = None):
        self._circuit = circuit
        self.policy = policy or RetryPolicy()

    # ── 错误分类 ─────────────────────────────────────────────
    def is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, UpstreamTimeoutError):
            return True
        if isinstance(exc, UpstreamError):
            return getattr(exc, "http_status", 502) in RETRYABLE_STATUS
        # 上游 httpx 网络类错误统一视为可重试
        if exc.__class__.__name__ in ("ConnectError", "ReadTimeout", "WriteTimeout", "PoolTimeout", "RemoteProtocolError"):
            return True
        return False

    def is_auth_error(self, exc: BaseException) -> bool:
        if isinstance(exc, InvalidAuthError):
            return True
        # 上游 401/403 包装在 UpstreamError 中
        if isinstance(exc, UpstreamError):
            return getattr(exc, "http_status", None) in (401, 403)
        return False

    def _normalize_error(self, candidate: Any, exc: BaseException) -> tuple[GatewayError, bool]:
        """把任意异常归一为 GatewayError，并返回 config_missing 标记。"""
        config_missing = bool(getattr(exc, "config_missing", False))
        if self.is_auth_error(exc):
            # 上游鉴权失败不向客户端泄漏 401/403 细节，统一映射 502
            return (
                UpstreamError(
                    f"Upstream authentication failed ({candidate.provider})",
                    http_status=502,
                ),
                config_missing,
            )
        if isinstance(exc, GatewayError):
            return exc, config_missing
        return UpstreamError(str(exc)), False

    def _hook_error(self, exc: BaseException) -> GatewayError:
        """观察钩子使用的错误：保留原始错误码（如 invalid_auth 不记漏算花费）。"""
        if isinstance(exc, GatewayError):
            return exc
        return UpstreamError(str(exc))

    def decide_failure(
        self,
        candidate: Any,
        exc: BaseException,
        attempt: int,
        *,
        stream_started: bool,
        rf_available: bool,
        rf_degraded: bool,
    ) -> FailureDecision:
        """失败决策的**唯一入口**。

        stream_started: 已向客户端下发过 delta/finish_reason（一旦为真禁止 fallback）
        rf_available:   本次请求是否携带 response_format
        rf_degraded:    当前候选是否已做过 RF 降级
        """
        err, config_missing = self._normalize_error(candidate, exc)

        # 已经向客户端吐字：不能再切换候选拼接输出，终止请求
        if stream_started:
            return FailureDecision(
                FailureAction.ABORT_STREAM,
                err,
                record_circuit_failure=not config_missing,
            )

        # 认证失败：不重试，直接切换候选；配置缺失不计熔断失败
        if self.is_auth_error(exc):
            return FailureDecision(
                FailureAction.NEXT_CANDIDATE,
                err,
                record_circuit_failure=not config_missing,
            )

        # 上游不支持 response_format：降级重试同一 attempt，不消耗 attempt
        if (
            isinstance(err, UpstreamError)
            and err.http_status == 400
            and rf_available
            and not rf_degraded
        ):
            return FailureDecision(FailureAction.DEGRADE_RF, err)

        # 可重试且还有 attempt 配额 → 退避重试
        if self.is_retryable(err) and attempt < self.policy.max_attempts:
            return FailureDecision(
                FailureAction.RETRY,
                err,
                backoff=self.policy.backoff_for(attempt),
            )

        # 重试耗尽 / 不可重试 → 记熔断失败并切换下一候选
        return FailureDecision(
            FailureAction.NEXT_CANDIDATE,
            err,
            record_circuit_failure=not config_missing,
        )

    async def _safe_hook(self, hook, *args) -> None:
        """执行观察钩子；钩子异常必须吞掉（观测失败不能打断主流程）。"""
        if hook is None:
            return
        try:
            await hook(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retry/fallback observation hook failed: %s", exc)

    async def _close_stream(self, agen: Any) -> None:
        aclose = getattr(agen, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不覆盖原始错误
            logger.debug("Failed to close upstream stream", exc_info=True)

    # ── 非流式 ───────────────────────────────────────────────
    async def execute(
        self,
        store: MetricsStore,
        candidates,
        call_one: Callable[..., Awaitable[object]],
        on_attempt_failure: Optional[Callable[..., Awaitable[None]]] = None,
        *,
        rf_available: bool = False,
    ) -> CallOutcome:
        """按候选链执行非流式调用。

        call_one(candidate, attempt, rf_degraded) → AdapterResult（或抛 GatewayError）
        """
        attempts_total = 0
        last_error: Optional[GatewayError] = None
        last_candidate: Any = None

        for candidate in candidates:
            # 调用前再确认熔断状态（半开探针等）
            decision = await self._circuit.can_pass_request(store, candidate.provider)
            if not decision.allowed:
                logger.info("Skip candidate %s (circuit %s)", candidate.provider, decision.reason)
                continue

            last_candidate = candidate
            attempt = 1
            counted = False  # 当前逻辑 attempt 是否已计入 attempts_total
            rf_degraded = False

            while attempt <= self.policy.max_attempts:
                if not counted:
                    attempts_total += 1
                    counted = True
                try:
                    result = await call_one(
                        candidate, attempt=attempt, rf_degraded=rf_degraded
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 统一转换为错误
                    failure = self.decide_failure(
                        candidate, exc, attempt,
                        stream_started=False,
                        rf_available=rf_available,
                        rf_degraded=rf_degraded,
                    )
                    last_error = failure.error
                    await self._safe_hook(
                        on_attempt_failure, candidate, self._hook_error(exc)
                    )

                    if failure.record_circuit_failure:
                        await self._circuit.record_failure(store, candidate.provider)

                    if failure.action is FailureAction.DEGRADE_RF:
                        logger.warning(
                            "Upstream rejected response_format (provider=%s); "
                            "retrying same attempt without it",
                            candidate.provider,
                        )
                        rf_degraded = True
                        continue

                    if failure.action is FailureAction.RETRY:
                        logger.warning(
                            "Retry provider=%s model=%s attempt=%d/%d backoff=%.2fs err=%s",
                            candidate.provider, candidate.model_name, attempt,
                            self.policy.max_attempts, failure.backoff, failure.error,
                        )
                        await asyncio.sleep(failure.backoff)
                        attempt += 1
                        counted = False
                        continue

                    # NEXT_CANDIDATE（非流式不会出现 ABORT_STREAM）
                    logger.warning(
                        "Candidate failed provider=%s model=%s (attempts=%d) → fallback next",
                        candidate.provider, candidate.model_name, attempt,
                    )
                    break
                else:
                    await self._circuit.record_success(store, candidate.provider)
                    return CallOutcome(
                        provider=candidate.provider,
                        model=candidate.model_name,
                        attempts=attempts_total,
                        success=True,
                        result=result,
                        circuit_reason=decision.reason,
                    )

        err = last_error or UpstreamError("All candidates failed")
        if last_candidate is not None:
            # 失败归因：供 api 层写入 traces / error_log / daily_aggregates
            setattr(err, "provider", last_candidate.provider)
            setattr(err, "model", last_candidate.model_name)
        raise err

    # ── 流式 ─────────────────────────────────────────────────
    async def stream(
        self,
        store: MetricsStore,
        candidates,
        open_stream: Callable[..., Awaitable[Any]],
        *,
        rf_available: bool = False,
        read_event: Optional[Callable[[Any], Awaitable[Any]]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        on_attempt_failure: Optional[Callable[..., Awaitable[None]]] = None,
        on_partial_failure: Optional[Callable[..., Awaitable[None]]] = None,
        on_cancel: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> AsyncIterator[StreamItem]:
        """按候选链执行流式调用，产出生命周期事件。

        open_stream(candidate, attempt, rf_degraded) → AsyncIterator[StreamEvent]
        read_event(agen) 由路由层注入（可在此竞争客户端断连信号）
        on_partial_failure(candidate, error, attempt, stream_chars)
        on_cancel(candidate, stream_chars, stream_started)
        """
        attempts_total = 0
        last_error: Optional[GatewayError] = None
        last_candidate: Any = None
        request_committed = False  # 整个请求是否已向客户端下发过内容

        for candidate in candidates:
            decision = await self._circuit.can_pass_request(store, candidate.provider)
            if not decision.allowed:
                logger.info("Skip candidate %s (circuit %s)", candidate.provider, decision.reason)
                continue

            last_candidate = candidate
            attempt = 1
            counted = False
            rf_degraded = False

            while attempt <= self.policy.max_attempts:
                if not counted:
                    attempts_total += 1
                    counted = True

                attempt_started = False
                attempt_chars = 0
                ttft: Optional[float] = None
                finish_reason = "stop"
                agen: Any = None

                yield StreamItem(
                    kind="attempt_started",
                    candidate=candidate,
                    attempt=attempt,
                    attempts_total=attempts_total,
                )

                try:
                    agen = await open_stream(
                        candidate, attempt=attempt, rf_degraded=rf_degraded
                    )
                    while True:
                        if is_cancelled is not None and is_cancelled():
                            raise ClientDisconnected()
                        ev = (
                            await read_event(agen)
                            if read_event is not None
                            else await agen.__anext__()
                        )

                        if ev.usage is not None:
                            usage = ev.usage
                            if ttft is None and ev.ttft_ms is not None:
                                ttft = ev.ttft_ms
                            if ev.finish_reason:
                                finish_reason = ev.finish_reason
                            await self._circuit.record_success(store, candidate.provider)
                            yield StreamItem(
                                kind="finished",
                                candidate=candidate,
                                attempt=attempt,
                                attempts_total=attempts_total,
                                outcome=CallOutcome(
                                    provider=candidate.provider,
                                    model=candidate.model_name,
                                    attempts=attempts_total,
                                    success=True,
                                    usage=usage,
                                    finish_reason=finish_reason,
                                    ttft_ms=ttft or 0.0,
                                    circuit_reason=decision.reason,
                                ),
                            )
                            return

                        if ev.delta or ev.finish_reason:
                            if ttft is None and ev.ttft_ms is not None:
                                ttft = ev.ttft_ms
                            attempt_started = True
                            request_committed = True
                            if ev.delta:
                                attempt_chars += len(ev.delta)
                            if ev.finish_reason:
                                finish_reason = ev.finish_reason
                            yield StreamItem(
                                kind="delta",
                                candidate=candidate,
                                event=ev,
                                attempt=attempt,
                                attempts_total=attempts_total,
                                stream_chars=attempt_chars,
                            )

                except StopAsyncIteration:
                    # 流正常结束但无 usage 事件：标记为不完整 + 估算
                    await self._circuit.record_success(store, candidate.provider)
                    yield StreamItem(
                        kind="finished",
                        candidate=candidate,
                        attempt=attempt,
                        attempts_total=attempts_total,
                        outcome=CallOutcome(
                            provider=candidate.provider,
                            model=candidate.model_name,
                            attempts=attempts_total,
                            success=True,
                            usage=Usage(complete=False, estimated=True),
                            finish_reason=finish_reason,
                            ttft_ms=ttft or 0.0,
                            circuit_reason=decision.reason,
                        ),
                    )
                    return

                except ClientDisconnected:
                    await self._safe_hook(
                        on_cancel, candidate, attempt_chars, attempt_started
                    )
                    raise asyncio.CancelledError

                except asyncio.CancelledError:
                    await self._safe_hook(
                        on_cancel, candidate, attempt_chars, attempt_started
                    )
                    raise

                except GeneratorExit:
                    await self._safe_hook(
                        on_cancel, candidate, attempt_chars, attempt_started
                    )
                    raise

                except Exception as exc:  # noqa: BLE001 - 统一转换为错误
                    failure = self.decide_failure(
                        candidate, exc, attempt,
                        stream_started=request_committed or attempt_started,
                        rf_available=rf_available,
                        rf_degraded=rf_degraded,
                    )
                    last_error = failure.error

                    # 半程失败观测先于熔断记账（与旧流式顺序一致）
                    hook_err = self._hook_error(exc)
                    if attempt_started:
                        await self._safe_hook(
                            on_partial_failure,
                            candidate, hook_err, attempt, attempt_chars,
                        )
                    await self._safe_hook(
                        on_attempt_failure, candidate, hook_err
                    )

                    if failure.record_circuit_failure:
                        await self._circuit.record_failure(store, candidate.provider)

                    if failure.action is FailureAction.ABORT_STREAM:
                        logger.warning(
                            "Stream committed then failed provider=%s model=%s "
                            "(attempt=%d chars=%d) → abort without fallback",
                            candidate.provider, candidate.model_name,
                            attempt, attempt_chars,
                        )
                        yield StreamItem(
                            kind="partial_failed",
                            candidate=candidate,
                            error=failure.error,
                            attempt=attempt,
                            attempts_total=attempts_total,
                            stream_chars=attempt_chars,
                        )
                        yield StreamItem(
                            kind="finished",
                            candidate=candidate,
                            attempt=attempt,
                            attempts_total=attempts_total,
                            outcome=CallOutcome(
                                provider=candidate.provider,
                                model=candidate.model_name,
                                attempts=attempts_total,
                                success=False,
                                error=failure.error,
                                circuit_reason=decision.reason,
                                partial=True,
                            ),
                        )
                        return

                    if failure.action is FailureAction.DEGRADE_RF:
                        logger.warning(
                            "Upstream rejected response_format (provider=%s); "
                            "retrying same attempt without it",
                            candidate.provider,
                        )
                        rf_degraded = True
                        continue

                    if failure.action is FailureAction.RETRY:
                        logger.warning(
                            "Retry stream provider=%s model=%s attempt=%d/%d backoff=%.2fs err=%s",
                            candidate.provider, candidate.model_name, attempt,
                            self.policy.max_attempts, failure.backoff, failure.error,
                        )
                        await asyncio.sleep(failure.backoff)
                        attempt += 1
                        counted = False
                        continue

                    # NEXT_CANDIDATE
                    logger.warning(
                        "Stream candidate failed provider=%s model=%s (attempts=%d) → fallback next",
                        candidate.provider, candidate.model_name, attempt,
                    )
                    break

                except BaseException as exc:
                    if is_cancellation(exc):
                        await self._safe_hook(
                            on_cancel, candidate, attempt_chars, attempt_started
                        )
                        raise
                    raise

                finally:
                    if agen is not None:
                        await self._close_stream(agen)

        # 全部候选失败/被熔断跳过
        err = last_error or UpstreamError("All upstream candidates failed")
        yield StreamItem(
            kind="finished",
            candidate=last_candidate,
            outcome=CallOutcome(
                provider=last_candidate.provider if last_candidate else "",
                model=last_candidate.model_name if last_candidate else "",
                attempts=attempts_total,
                success=False,
                error=err,
            ),
        )
