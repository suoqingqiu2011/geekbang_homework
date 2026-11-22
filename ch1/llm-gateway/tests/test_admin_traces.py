"""Step 2：/v1/admin/traces 的 provider/model/days 过滤测试。"""

from __future__ import annotations

import time

from app.storage.metrics_store import MetricsStore

from .test_api import _headers


async def _seed(store: MetricsStore) -> None:
    now = time.time()
    old = now - 10 * 86400.0
    rows = [
        ("tr1", now, "p1", "m1", "success"),
        ("tr2", now, "p1", "m1", "error"),
        ("tr3", now, "p2", "m2", "success"),
        ("tr4", old, "p1", "m1", "error"),
    ]
    for trace_id, ts, provider, model, status in rows:
        await store.record_trace({
            "trace_id": trace_id,
            "timestamp_us": ts,
            "runtime": {"provider": provider, "model": model, "status": status},
        })


async def test_query_recent_traces_filters(tmp_path):
    store = MetricsStore(str(tmp_path / "traces.db"))
    await store.initialize()
    try:
        await _seed(store)

        assert len(await store.query_recent_traces()) == 4
        assert len(await store.query_recent_traces(provider="p1")) == 3
        assert len(await store.query_recent_traces(provider="p1", days=1)) == 2
        assert len(await store.query_recent_traces(model="m2")) == 1

        errors = await store.query_recent_traces(
            provider="p1", model="m1", status="error"
        )
        assert {r["trace_id"] for r in errors} == {"tr2", "tr4"}

        recent_errors = await store.query_recent_traces(
            provider="p1", model="m1", status="error", days=1
        )
        assert [r["trace_id"] for r in recent_errors] == ["tr2"]
    finally:
        await store.close()


async def test_admin_traces_filters_endpoint(gateway):
    ctx = gateway["ctx"]
    now = time.time()
    await ctx.store.record_trace({
        "trace_id": "api-tr1", "timestamp_us": now,
        "runtime": {"provider": "p1", "model": "m1", "status": "error"},
    })
    await ctx.store.record_trace({
        "trace_id": "api-tr2", "timestamp_us": now,
        "runtime": {"provider": "p2", "model": "m2", "status": "success"},
    })

    r = await gateway["client"].get(
        "/v1/admin/traces?provider=p1&model=m1&status=error&days=30&limit=10",
        headers=_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "p1"
    assert data["model"] == "m1"
    assert data["status"] == "error"
    assert data["days"] == 30
    assert data["limit"] == 10
    assert [i["trace_id"] for i in data["items"]] == ["api-tr1"]

    # 非法 days → 项目统一的 400 invalid_request
    r = await gateway["client"].get(
        "/v1/admin/traces?days=-1", headers=_headers()
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
