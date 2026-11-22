"""Step 3：/v1/admin/daily 的 model 过滤测试。"""

from __future__ import annotations

from app.storage.metrics_store import MetricsStore

from .test_api import _headers


async def _seed(store: MetricsStore) -> None:
    for billing_id, provider, model in (
        ("d1", "p1", "m1"),
        ("d2", "p1", "m2"),
        ("d3", "p2", "m1"),
    ):
        await store.record_usage(
            billing_id=billing_id, trace_id=billing_id, provider=provider, model=model,
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
        )


async def test_query_daily_aggregates_model_filter(tmp_path):
    store = MetricsStore(str(tmp_path / "daily.db"))
    await store.initialize()
    try:
        await _seed(store)

        assert len(await store.query_daily_aggregates(days=7)) == 3
        assert len(await store.query_daily_aggregates(days=7, provider="p1")) == 2
        assert len(
            await store.query_daily_aggregates(days=7, provider="p1", model="m1")
        ) == 1
        assert len(await store.query_daily_aggregates(days=7, model="m1")) == 2
    finally:
        await store.close()


async def test_admin_daily_model_filter_endpoint(gateway):
    ctx = gateway["ctx"]
    await ctx.store.record_usage(
        billing_id="api-d1", trace_id="api-d1", provider="p1", model="m1",
        prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
    )
    await ctx.store.record_usage(
        billing_id="api-d2", trace_id="api-d2", provider="p1", model="m2",
        prompt_tokens=20, completion_tokens=10, latency_ms=200.0, cost_usd=0.02,
    )

    r = await gateway["client"].get(
        "/v1/admin/daily?days=7&provider=p1&model=m1", headers=_headers()
    )
    assert r.status_code == 200
    data = r.json()
    assert data["days"] == 7
    assert data["provider"] == "p1"
    assert data["model"] == "m1"
    assert len(data["items"]) == 1
    assert data["items"][0]["model"] == "m1"
    assert data["items"][0]["requests"] == 1
