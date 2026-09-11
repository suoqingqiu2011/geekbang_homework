"""
CircuitBreakerManager — 跨 worker 一致的熔断状态机（G3 修复，SPEC §4.7）。

状态机：CLOSED → OPEN → HALF_OPEN → (CLOSED | OPEN)
  - 状态持久化于 MetricsStore.circuit_states（单一事实来源）
  - TTLCache 热路径优化；决策最终以 DB 为准
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from cachetools import TTLCache

from ..storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway.circuit-breaker")


@dataclass
class CircuitDecision:
    allowed: bool
    reason: str  # "" | "circuit_open" | "half_open_probe"


class CircuitBreakerManager:
    """按 provider 维护熔断状态。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        cache_ttl: float = 1.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._cache: TTLCache[str, CircuitDecision] = TTLCache(maxsize=1024, ttl=cache_ttl)

    def _cache_key(self, provider: str) -> str:
        return provider

    async def can_pass_request(self, store: MetricsStore, provider: str) -> CircuitDecision:
        """是否放行请求（热路径走缓存，冷路径回源 DB）。"""
        cached = self._cache.get(provider)
        if cached is not None:
            return cached
        blocked, reason = await store.should_block_provider(
            provider,
            threshold=self.failure_threshold,
            recovery_timeout=self.recovery_timeout,
            half_open_max_calls=self.half_open_max_calls,
        )
        decision = CircuitDecision(allowed=not blocked, reason=reason)
        # 只缓存确定性的 CLOSED 放行，open/half_open 决策必须实时
        if decision.allowed and reason == "":
            self._cache[provider] = decision
        return decision

    async def record_success(self, store: MetricsStore, provider: str) -> None:
        await store.record_provider_success(provider)
        self._cache[provider] = CircuitDecision(allowed=True, reason="")

    async def record_failure(self, store: MetricsStore, provider: str) -> None:
        await store.record_provider_failure(provider, self.failure_threshold)
        self._cache.pop(provider, None)

    def update_params(
        self, failure_threshold: int, recovery_timeout: float, half_open_max_calls: int
    ) -> None:
        """配置热更新时刷新熔断参数（M4）。"""
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._cache.clear()
