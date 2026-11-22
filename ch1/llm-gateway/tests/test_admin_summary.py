"""Step 1：/v1/admin/summary 的 group_by + days 窗口测试。"""

from __future__ import annotations

import time

from app.storage.metrics_store import MetricsStore

from .test_api import _headers


async def _seed(store: MetricsStore) -> None:
    await store.record_usage(
        billing_id="s1", trace_id="t1", provider="p1", model="m1",
        prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
        ttft_ms=50.0, cost_usd=0.01, stream=True, fallback_used=False,
    )
    await store.record_usage(
        billing_id="s2", trace_id="t2", provider="p1", model="m1",
        prompt_tokens=20, completion_tokens=10, latency_ms=300.0,
        ttft_ms=150.0, cost_usd=0.02, stream=False, fallback_used=True,
    )
    await store.record_usage(
        billing_id="s3", trace_id="t3", provider="p2", model="m2",
        prompt_tokens=5, completion_tokens=5, latency_ms=200.0,
        ttft_ms=100.0, cost_usd=0.0, stream=True, estimated=True,
    )
    await store.record_error("t1", "p1", "upstream_error", "boom", model="m1")
    await store.record_error("t2", "p1", "upstream_timeout", "slow", model="m1")
    await store.record_error("t3", "p2", "upstream_error", "boom", model="m2")
    # 只有失败、没有成功用量的组，也必须出现在 grouped items 里
    await store.record_error("t4", "p3", "upstream_error", "boom", model="m3")


async def test_query_summary_group_by_provider_model(tmp_path):
    store = MetricsStore(str(tmp_path / "summary.db"))
    await store.initialize()
    try:
        await _seed(store)

        # 默认结构保持不变
        flat = await store.query_summary()
        assert flat["requests"] == 3
        assert flat["providers"] == 2
        assert flat["errors"] == 4
        assert flat["total_requests"] == 7
        assert flat["error_rate"] == round(4 / 7, 6)
        assert flat["days"] == 0
        assert abs(flat["total_cost"] - 0.03) < 1e-9
        assert "items" not in flat

        grouped = await store.query_summary(group_by="provider_model")
        assert grouped["group_by"] == "provider_model"
        assert grouped["days"] == 0
        assert grouped["totals"]["requests"] == 3
        assert grouped["totals"]["total_requests"] == 7
        assert grouped["totals"]["error_rate"] == round(4 / 7, 6)
        assert len(grouped["items"]) == 3

        by_key = {(i["provider"], i["model"]): i for i in grouped["items"]}
        p1m1 = by_key[("p1", "m1")]
        assert p1m1["requests"] == 2
        assert p1m1["prompt_tokens"] == 30
        assert p1m1["completion_tokens"] == 15
        assert p1m1["total_tokens"] == 45
        assert abs(p1m1["cost_usd"] - 0.03) < 1e-9
        assert p1m1["avg_latency_ms"] == 200.0
        assert p1m1["avg_ttft_ms"] == 100.0
        assert p1m1["estimated_requests"] == 0
        assert p1m1["estimated_tokens"] == 0
        assert p1m1["stream_requests"] == 1
        assert p1m1["fallback_requests"] == 1
        assert p1m1["errors"] == 2
        assert p1m1["total_requests"] == 4
        assert p1m1["error_rate"] == 0.5

        p2m2 = by_key[("p2", "m2")]
        assert p2m2["requests"] == 1
        assert p2m2["total_tokens"] == 10
        assert p2m2["cost_usd"] == 0.0
        assert p2m2["estimated_requests"] == 1
        assert p2m2["estimated_tokens"] == 10
        assert p2m2["errors"] == 1
        assert p2m2["total_requests"] == 2
        assert p2m2["error_rate"] == 0.5

        # 只有失败、没有成功用量的组
        p3m3 = by_key[("p3", "m3")]
        assert p3m3["requests"] == 0
        assert p3m3["total_tokens"] == 0
        assert p3m3["cost_usd"] == 0.0
        assert p3m3["errors"] == 1
        assert p3m3["total_requests"] == 1
        assert p3m3["error_rate"] == 1.0

        # provider / model 单维度
        by_provider = await store.query_summary(group_by="provider")
        p1 = next(i for i in by_provider["items"] if i["provider"] == "p1")
        assert p1["requests"] == 2 and p1["errors"] == 2
        p3 = next(i for i in by_provider["items"] if i["provider"] == "p3")
        assert p3["requests"] == 0 and p3["errors"] == 1

        by_model = await store.query_summary(group_by="model")
        m1 = next(i for i in by_model["items"] if i["model"] == "m1")
        assert m1["requests"] == 2 and m1["errors"] == 2
        m3 = next(i for i in by_model["items"] if i["model"] == "m3")
        assert m3["requests"] == 0 and m3["errors"] == 1
    finally:
        await store.close()


async def test_query_summary_days_window(tmp_path):
    store = MetricsStore(str(tmp_path / "summary-window.db"))
    await store.initialize()
    try:
        await store.record_usage(
            billing_id="w1", trace_id="w1", provider="p1", model="m1",
            prompt_tokens=1, completion_tokens=1, latency_ms=10.0, cost_usd=0.01,
        )
        await store.record_usage(
            billing_id="w2", trace_id="w2", provider="p1", model="m1",
            prompt_tokens=1, completion_tokens=1, latency_ms=10.0, cost_usd=0.01,
        )
        await store.record_error("w1", "p1", "upstream_error", "boom", model="m1")

        old = time.time() - 10 * 86400.0
        db = store._require_db()
        await db.execute(
            "UPDATE usage_metrics SET created_at = ? WHERE billing_id = 'w1'", (old,)
        )
        await db.execute(
            "UPDATE error_log SET created_at = ? WHERE trace_id = 'w1'", (old,)
        )
        await db.commit()

        all_time = await store.query_summary(group_by="provider_model", days=0)
        assert all_time["totals"]["requests"] == 2
        assert all_time["totals"]["errors"] == 1
        assert all_time["items"][0]["requests"] == 2
        assert all_time["items"][0]["errors"] == 1

        recent = await store.query_summary(group_by="provider_model", days=1)
        assert recent["totals"]["requests"] == 1
        assert recent["totals"]["errors"] == 0
        assert recent["items"][0]["requests"] == 1
        assert recent["items"][0]["errors"] == 0
    finally:
        await store.close()


# ─────────────────────────────────────────────────────────────
# HTTP 接口
# ─────────────────────────────────────────────────────────────
async def test_admin_summary_group_by_endpoint(gateway):
    ctx = gateway["ctx"]
    await ctx.store.record_usage(
        billing_id="e1", trace_id="e1", provider="p1", model="m1",
        prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
    )
    await ctx.store.record_usage(
        billing_id="e2", trace_id="e2", provider="p2", model="m2",
        prompt_tokens=20, completion_tokens=10, latency_ms=200.0, cost_usd=0.02,
    )
    await ctx.store.record_error("e1", "p1", "upstream_error", "boom", model="m1")

    r = await gateway["client"].get(
        "/v1/admin/summary?group_by=provider_model&days=30", headers=_headers()
    )
    assert r.status_code == 200
    data = r.json()
    assert data["group_by"] == "provider_model"
    assert data["days"] == 30
    assert data["totals"]["requests"] == 2
    assert len(data["items"]) == 2
    p1 = next(i for i in data["items"] if i["provider"] == "p1")
    assert p1["model"] == "m1"
    assert p1["prompt_tokens"] == 10
    assert p1["errors"] == 1
    assert p1["total_requests"] == 2
    assert p1["error_rate"] == 0.5
    assert data["totals"]["total_requests"] == 3
    assert data["totals"]["error_rate"] == round(1 / 3, 6)

    # 默认结构保持扁平，并回显 days / total_requests / error_rate
    r = await gateway["client"].get("/v1/admin/summary", headers=_headers())
    assert r.status_code == 200
    flat = r.json()
    assert "items" not in flat
    assert flat["requests"] == 2
    assert flat["days"] == 0
    assert flat["errors"] == 1
    assert flat["total_requests"] == 3
    assert flat["error_rate"] == round(1 / 3, 6)

    # 非法维度 → 项目统一的 400 invalid_request
    r = await gateway["client"].get(
        "/v1/admin/summary?group_by=bogus", headers=_headers()
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
