"""错误归因与 daily.errors 回归测试。

覆盖：
  - 非流式失败：traces / error_log / daily_aggregates 均带 provider+model；
  - 流式失败：daily_aggregates.errors 递增；
  - record_daily_error 与 record_usage 共用 (day, provider, model) 行且互不覆盖。
"""

from __future__ import annotations

from app.storage.metrics_store import MetricsStore

from .test_api import _headers, _req


async def _db_row(ctx, sql: str, params: tuple = ()):
    return await (
        await ctx.store._require_db().execute(sql, params)
    ).fetchone()


# ─────────────────────────────────────────────────────────────
# 非流式失败：错误归因写入 traces / error_log / daily
# ─────────────────────────────────────────────────────────────
async def test_nonstream_failure_attribution(gateway):
    body = _req(model="fail500", trace_id="err-attr-nonstream")
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 500  # 上游 5xx 原样透传（既有语义）
    assert r.json()["error"]["code"] == "upstream_error"

    ctx = gateway["ctx"]
    trace = await _db_row(
        ctx,
        "SELECT provider, model, status, error_code FROM traces WHERE trace_id = ?",
        ("err-attr-nonstream",),
    )
    assert trace is not None
    assert trace["provider"] == "fail"
    assert trace["model"] == "fail500"
    assert trace["status"] == "error"
    assert trace["error_code"] == "upstream_error"

    err = await _db_row(
        ctx,
        "SELECT provider, model, error_code FROM error_log WHERE trace_id = ?",
        ("err-attr-nonstream",),
    )
    assert err is not None
    assert err["provider"] == "fail"
    assert err["model"] == "fail500"
    assert err["error_code"] == "upstream_error"

    daily = await _db_row(
        ctx,
        "SELECT requests, errors FROM daily_aggregates "
        "WHERE provider = ? AND model = ?",
        ("fail", "fail500"),
    )
    assert daily is not None
    assert daily["requests"] == 0     # 失败请求不计入成功用量
    assert daily["errors"] == 1       # 终态失败只计一次（不按 attempt 重复累加）


# ─────────────────────────────────────────────────────────────
# 流式失败：daily.errors 递增
# ─────────────────────────────────────────────────────────────
async def test_stream_failure_increments_daily_errors(gateway):
    body = _req(model="broken", stream=True, trace_id="err-attr-stream")
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert '"error"' in r.text

    ctx = gateway["ctx"]
    err = await _db_row(
        ctx,
        "SELECT provider, model, error_code FROM error_log WHERE trace_id = ?",
        ("err-attr-stream",),
    )
    assert err is not None
    assert err["provider"] == "ok"
    assert err["model"] == "broken"

    daily = await _db_row(
        ctx,
        "SELECT requests, errors FROM daily_aggregates "
        "WHERE provider = ? AND model = ?",
        ("ok", "broken"),
    )
    assert daily is not None
    assert daily["requests"] == 0
    assert daily["errors"] == 1


# ─────────────────────────────────────────────────────────────
# daily 行：errors 与 requests 共用同一 (day, provider, model)
# ─────────────────────────────────────────────────────────────
async def test_daily_error_and_usage_share_row(tmp_path):
    store = MetricsStore(str(tmp_path / "daily-err.db"))
    await store.initialize()
    try:
        await store.record_daily_error("p", "m")
        await store.record_daily_error("p", "m")

        row = await (
            await store._require_db().execute(
                "SELECT requests, errors FROM daily_aggregates "
                "WHERE provider = 'p' AND model = 'm'"
            )
        ).fetchone()
        assert row["requests"] == 0
        assert row["errors"] == 2

        await store.record_usage(
            billing_id="b1", trace_id="t1", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
            cost_usd=0.01,
        )
        row = await (
            await store._require_db().execute(
                "SELECT requests, errors FROM daily_aggregates "
                "WHERE provider = 'p' AND model = 'm'"
            )
        ).fetchone()
        assert row["requests"] == 1
        assert row["errors"] == 2  # 成功用量累加不得覆盖 errors
    finally:
        await store.close()
