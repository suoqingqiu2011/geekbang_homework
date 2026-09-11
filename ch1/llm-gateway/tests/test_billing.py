"""计费幂等（恰好一次）单元测试。"""

from __future__ import annotations

from app.storage.metrics_store import MetricsStore


async def test_record_usage_idempotent(tmp_path):
    store = MetricsStore(str(tmp_path / "billing.db"))
    await store.initialize()
    try:
        kw = dict(
            billing_id="b1", trace_id="t1", provider="ok", model="ok",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
            ttft_ms=50.0, cost_usd=0.001,
        )
        first = await store.record_usage(**kw)
        second = await store.record_usage(**kw)  # 同 billing_id 重复
        assert first is True
        assert second is False

        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM usage_metrics WHERE billing_id='b1'"
        )
        row = await cursor.fetchone()
        assert row["c"] == 1  # 恰好一次
    finally:
        await store.close()


async def test_daily_aggregate_bumped(tmp_path):
    store = MetricsStore(str(tmp_path / "agg.db"))
    await store.initialize()
    try:
        await store.record_usage(
            billing_id="a", trace_id="t", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
        )
        await store.record_usage(
            billing_id="b", trace_id="t2", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=200.0, cost_usd=0.02,
        )
        rows = await store.query_daily_aggregates(days=1)
        assert rows and rows[0]["requests"] == 2
        assert abs(rows[0]["cost_usd"] - 0.03) < 1e-9
    finally:
        await store.close()


async def test_query_summary(tmp_path):
    store = MetricsStore(str(tmp_path / "sum.db"))
    await store.initialize()
    try:
        await store.record_usage(
            billing_id="c", trace_id="t", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
        )
        await store.record_error("t", "p", "upstream_error", "boom")
        s = await store.query_summary()
        assert s["requests"] == 1
        assert s["errors"] == 1
        assert s["total_tokens"] == 15
    finally:
        await store.close()
