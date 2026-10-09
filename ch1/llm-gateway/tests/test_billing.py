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
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
            cost_usd=0.01, ttft_ms=50.0,
        )
        await store.record_error("t", "p", "upstream_error", "boom")
        s = await store.query_summary()
        assert s["requests"] == 1
        assert s["errors"] == 1
        assert s["prompt_tokens"] == 10
        assert s["completion_tokens"] == 5
        assert s["total_tokens"] == 15
        assert s["avg_ttft_ms"] == 50.0
    finally:
        await store.close()


async def test_record_trace_status_from_runtime(tmp_path):
    """状态列须与 payload 中的 runtime 一致（snapshot 把指标固化在 runtime 下）。"""
    store = MetricsStore(str(tmp_path / "trace.db"))
    await store.initialize()
    try:
        await store.record_trace({
            "trace_id": "trace-err-1",
            "user_id": "u1",
            "runtime": {
                "model": "m1",
                "provider": "p1",
                "status": "error",
                "error_code": "upstream_error",
                "latency_ms": 88.0,
            },
        })
        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT model, provider, status, error_code, latency_ms, payload "
            "FROM traces WHERE trace_id = 'trace-err-1'"
        )
        row = await cursor.fetchone()
        assert row["model"] == "m1"
        assert row["provider"] == "p1"
        assert row["status"] == "error"          # 不再是默认 success
        assert row["error_code"] == "upstream_error"
        assert abs(row["latency_ms"] - 88.0) < 1e-9
        payload = __import__("json").loads(row["payload"])
        assert payload["status"] == "error"      # 状态列与 payload 一致

        # 无 runtime 指标时仍可用顶层兜底值
        await store.record_trace({"trace_id": "trace-ok-1", "status": "success"})
        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT status FROM traces WHERE trace_id = 'trace-ok-1'"
        )
        row = await cursor.fetchone()
        assert row["status"] == "success"
    finally:
        await store.close()


async def test_completion_id_no_trailing_dash():
    """响应 id 应为 chatcmpl-<24 位 hex>，不得残留尾部裸连字符。"""
    from app.api.chat import _completion_id

    cid = _completion_id("72648c9c-c659-4d56-accb-1f2e3d4c5b6a")
    assert cid == "chatcmpl-72648c9cc6594d56accb1f2e"
    assert not cid.endswith("-")
    assert cid.count("-") == 1  # 仅前缀分隔符
