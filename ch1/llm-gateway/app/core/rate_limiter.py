"""
RateLimiter — Token Bucket 分层限流（SPEC §4.2 / §11-4）。

粒度优先级（最严格者生效）：per-model > per-endpoint > per-project > per-user。
暂不含 token 配额与突发量（Out of Scope）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger("llm-gateway.rate-limiter")


class TokenBucket:
    """单桶令牌桶。refill_rate=每秒补充令牌数；capacity=桶容量。"""

    __slots__ = ("rate", "capacity", "tokens", "updated_at", "lock")

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated_at = time.monotonic()
        self.lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        """
        尝试消耗令牌。
        返回 (允许, retry_after_seconds)。
        """
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated_at
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated_at = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True, 0.0
            deficit = tokens - self.tokens
            return False, deficit / self.rate


class RateLimiter:
    """
    分层令牌桶限流器。

    分层键：
      model   → f"m:{model}"
      endpoint→ f"e:{method}:{path}"
      project → f"p:{project_id}"
      user    → f"u:{user_id}"
    任一桶不足即拒绝（最严格者生效）。
    """

    def __init__(
        self,
        rates: dict[str, float],
        bucket_capacity: float | None = None,
        max_buckets: int = 8192,
    ) -> None:
        """
        rates: {"model": r1, "endpoint": r2, "project": r3, "user": r4}
        bucket_capacity: 各桶统一容量（None 时取对应 rate 的 5 倍）
        max_buckets: 桶缓存上限，超出按 LRU 淘汰最久未使用桶，防随机
                     user_id/model 打请求导致内存无界增长（内存 DoS）。
        """
        self._rates = rates
        self._default_capacity = bucket_capacity
        self._max_buckets = max_buckets
        # OrderedDict 实现 LRU：命中的键 move_to_end，超限时淘汰队首（最久未用）
        self._buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def _bucket(self, key: str, rate: float) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is not None and abs(bucket.rate - rate) < 1e-9:
            self._buckets.move_to_end(key)
            return bucket
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None and abs(bucket.rate - rate) < 1e-9:
                self._buckets.move_to_end(key)
                return bucket
            capacity = self._default_capacity if self._default_capacity else max(rate * 5, 1.0)
            bucket = TokenBucket(rate, capacity)
            self._buckets[key] = bucket
            self._buckets.move_to_end(key)
            if len(self._buckets) > self._max_buckets:
                # 淘汰最久未使用的桶，防止恶意随机 key 撑爆内存
                self._buckets.pop(next(iter(self._buckets)), None)
            return bucket

    async def check(
        self,
        *,
        model: str | None = None,
        endpoint_key: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[bool, float, str | None]:
        """
        逐层检查。返回 (允许, retry_after, 命中最严层名)。
        层级优先级：model > endpoint > project > user。

        两阶段（预检 → 原子扣减）：先在固定顺序下持有全部相关桶锁完成
        只读预检（刷新令牌、判定是否足够），任一不足即拒绝且不扣任何令牌；
        全部足够才统一扣减。避免"高层拒绝仍消耗低层令牌"压低低层配额，
        同时保持并发下不超扣、令牌不为负的严格语义。
        """
        layers = [
            ("model", model, self._rates.get("model")),
            ("endpoint", endpoint_key, self._rates.get("endpoint")),
            ("project", project_id, self._rates.get("project")),
            ("user", user_id, self._rates.get("user")),
        ]
        items: list[tuple[str, str, TokenBucket]] = []
        for name, value, rate in layers:
            if not value or rate is None:
                continue
            items.append((name, value, await self._bucket(f"{name[0]}:{value}", float(rate))))

        # 固定顺序（model→endpoint→project→user）加锁，多桶持锁不产生死锁
        locked: list[TokenBucket] = []
        try:
            for _, _, bucket in items:
                await bucket.lock.acquire()
                locked.append(bucket)

            # 阶段一：预检（只刷新与判定，不扣令牌）
            worst: float = 0.0
            worst_layer: str | None = None
            for name, value, bucket in items:
                now = time.monotonic()
                elapsed = now - bucket.updated_at
                bucket.tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.rate)
                bucket.updated_at = now
                if bucket.tokens < 1.0:
                    retry = (1.0 - bucket.tokens) / bucket.rate
                    logger.warning(
                        "Rate limit hit: layer=%s key=%s retry_after=%.2fs", name, value, retry
                    )
                    if retry > worst:
                        worst, worst_layer = retry, name
            if worst > 0:
                return False, worst, worst_layer  # 被拒请求不消耗任何令牌

            # 阶段二：全部通过后统一原子扣减
            for _, _, bucket in items:
                bucket.tokens -= 1.0
            return True, 0.0, None
        finally:
            for bucket in locked:
                bucket.lock.release()

    async def update_rates(self, rates: dict[str, float]) -> None:
        """配置热更新时刷新限流速率（M4）。已有桶按新速率重建。"""
        self._rates = dict(rates)
        async with self._lock:
            self._buckets.clear()
