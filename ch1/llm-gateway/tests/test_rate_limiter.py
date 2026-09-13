"""RateLimiter / TokenBucket 单元测试。"""

from __future__ import annotations

from app.core.rate_limiter import RateLimiter, TokenBucket


async def test_token_bucket_capacity():
    b = TokenBucket(rate=1.0, capacity=2.0)
    ok1, _ = await b.consume()
    ok2, _ = await b.consume()
    ok3, retry = await b.consume()
    assert ok1 and ok2
    assert not ok3
    assert retry > 0.5  # 需等待约 1s 补充


async def test_token_bucket_refill():
    import asyncio

    b = TokenBucket(rate=10.0, capacity=1.0)
    ok1, _ = await b.consume()
    assert ok1
    await asyncio.sleep(0.2)  # 补充 ~2 token
    ok2, _ = await b.consume()
    assert ok2


async def test_ratelimiter_layers_most_restrictive_wins():
    rl = RateLimiter(
        rates={"model": 1.0, "endpoint": 100.0, "project": 100.0, "user": 100.0},
        bucket_capacity=1.0,
    )
    ok, _, layer = await rl.check(model="m1", endpoint_key="e", project_id="p", user_id="u")
    assert ok
    ok, retry, layer = await rl.check(model="m1", endpoint_key="e", project_id="p", user_id="u")
    assert not ok
    assert layer == "model"  # per-model 最严，最先触发


async def test_ratelimiter_user_level_only():
    rl = RateLimiter(rates={"user": 2.0}, bucket_capacity=2.0)
    assert (await rl.check(user_id="u1"))[0]
    assert (await rl.check(user_id="u1"))[0]
    assert not (await rl.check(user_id="u1"))[0]
    assert (await rl.check(user_id="u2"))[0]  # 不同 user 独立桶


async def test_ratelimiter_update_rates():
    rl = RateLimiter(rates={"model": 1.0}, bucket_capacity=1.0)
    await rl.check(model="m")
    await rl.update_rates({"model": 100.0})
    assert (await rl.check(model="m"))[0]  # 刷新后放行


# ─────────────────────────────────────────────────────────────
# 内存 DoS 防护：桶缓存超出 max_buckets 后按 LRU 淘汰，绝不无界增长
# ─────────────────────────────────────────────────────────────
async def test_ratelimiter_max_buckets_lru_eviction():
    rl = RateLimiter(rates={"model": 10.0}, bucket_capacity=10.0, max_buckets=2)
    await rl.check(model="m1")  # 桶: m:m1
    await rl.check(model="m2")  # 桶: m:m2
    assert len(rl._buckets) == 2

    # 再次用到 m1 → 提升其 LRU 新鲜度；随后 m3 加入 → 应淘汰 m2（最久未用）
    await rl.check(model="m1")
    await rl.check(model="m3")
    assert len(rl._buckets) == 2                       # 上限强制生效
    assert "m:m1" in rl._buckets and "m:m3" in rl._buckets
    assert "m:m2" not in rl._buckets                  # 最久未用的 m2 被淘汰


async def test_ratelimiter_max_buckets_continuous_bounded():
    """连续写入远多于 max_buckets 的随机 key，桶数始终有界（防内存 DoS 核心断言）。"""
    rl = RateLimiter(rates={"user": 10.0}, bucket_capacity=10.0, max_buckets=4)
    for i in range(100):
        await rl.check(user_id=f"u{i}")
        assert len(rl._buckets) <= 4
