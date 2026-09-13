"""
RetryFallbackManager — 重试 + fallback/failover（SPEC §4.7 / §9）。

规则：
  - 仅可重试错误重试：429、5xx、超时（退避 + 抖动）
  - 认证失败 / 非法请求：不重试，直接抛出
  - 候选链逐个尝试；单候选重试耗尽 → 切换下一候选（fallback）
  - 每次结果同步熔断器状态
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..core.circuit_breaker import CircuitBreakerManager
from ..errors import (
    GatewayError,
    InvalidAuthError,
    UpstreamError,
    UpstreamTimeoutError,
)
from ..storage.metrics_store import MetricsStore

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

        作为重试退避的**单一事实来源**（execute 与流式 _handle_stream 共用），
        保证两路退避策略一致（原流式用线性 0.5*attempt，与 RetryPolicy 指数+抖动不一致）。
        """
        if attempt < 1:
            return 0.0
        exp = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
        return exp * (1 + random.uniform(0, self.jitter))


@dataclass
class CallOutcome:
    """一次候选调用的结果。"""

    provider: str
    model: str
    attempts: int = 1
    success: bool = False
    result: object = None            # AdapterResult | None
    error: Optional[GatewayError] = None
    circuit_reason: str = ""


class RetryFallbackManager:
    """重试 + 降级链执行器。"""

    def __init__(self, circuit: CircuitBreakerManager, policy: Optional[RetryPolicy] = None):
        self._circuit = circuit
        self.policy = policy or RetryPolicy()

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

    async def execute(
        self,
        store: MetricsStore,
        candidates,
        call_one: Callable[..., Awaitable[object]],
    ) -> CallOutcome:
        """
        按候选链执行。

        candidates: Router 输出的有序候选（Candidate）
        call_one(candidate, is_probe) → AdapterResult（或抛 GatewayError）
        """
        attempts_total = 0
        last_error: Optional[GatewayError] = None

        for idx, candidate in enumerate(candidates):
            probe = idx > 0  # 后续候选视为 fallback
            # 调用前再确认熔断状态（半开探针等）
            decision = await self._circuit.can_pass_request(store, candidate.provider)
            if not decision.allowed:
                logger.info("Skip candidate %s (circuit %s)", candidate.provider, decision.reason)
                continue

            for attempt in range(1, self.policy.max_attempts + 1):
                attempts_total += 1
                try:
                    result = await call_one(candidate, probe=probe, attempt=attempt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 统一转换为错误
                    gw_exc = exc if isinstance(exc, GatewayError) else UpstreamError(str(exc))
                    last_error = gw_exc

                    if self.is_auth_error(gw_exc):
                        # 认证失败：不重试，映射为 502（不向客户端泄漏上游 401），跳到下一候选
                        await self._circuit.record_failure(store, candidate.provider)
                        last_error = UpstreamError(
                            f"Upstream authentication failed ({candidate.provider})",
                            http_status=502,
                        )
                        logger.error("Auth error from %s: %s", candidate.provider, gw_exc.message)
                        break

                    if self.is_retryable(gw_exc) and attempt < self.policy.max_attempts:
                        backoff = self.policy.backoff_for(attempt)
                        logger.warning(
                            "Retry provider=%s model=%s attempt=%d/%d backoff=%.2fs err=%s",
                            candidate.provider, candidate.model_name, attempt, self.policy.max_attempts, backoff, gw_exc,
                        )
                        await asyncio.sleep(backoff)
                        continue

                    # 重试耗尽 → 记熔断失败，尝试下一候选
                    await self._circuit.record_failure(store, candidate.provider)
                    logger.warning(
                        "Candidate failed provider=%s model=%s (attempts=%d) → fallback next",
                        candidate.provider, candidate.model_name, attempt,
                    )
                    break

                else:
                    # 成功：立即返回，不得落入 for...else 继续下一次 attempt
                    #（否则成功路径会重复调用上游，如 400 降级成功后仍发起第二次尝试）
                    await self._circuit.record_success(store, candidate.provider)
                    return CallOutcome(
                        provider=candidate.provider,
                        model=candidate.model_name,
                        attempts=attempts_total,
                        success=True,
                        result=result,
                        circuit_reason=decision.reason,
                    )

        # 全部候选失败
        err = last_error or UpstreamError("All candidates failed")
        raise err

    async def execute_stream(
        self,
        store: MetricsStore,
        candidates,
        call_one: Callable[..., Awaitable[object]],
    ) -> CallOutcome:
        """流式场景：与 execute 同逻辑（取消/断流异常在调用方处理）。"""
        return await self.execute(store, candidates, call_one)
