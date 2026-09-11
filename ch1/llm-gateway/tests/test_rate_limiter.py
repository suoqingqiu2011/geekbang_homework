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
