"""部分输出后失败（先吐字再断流）的观测固化测试。

D2 决策：
  - traces 只在请求终态写一次（success / error / partial_failed / cancelled）；
  - partial 失败写独立 append-only 表 stream_attempt_failures，
    不写入 error_log（避免虚增 summary.errors），也不中途占用 traces。

对应场景：流式请求已向客户端吐出部分内容后才失败（如上游断连/5xx）。
"""

from __future__ import annotations

import json

from app.api.chat import _record_failure
from app.core.trace_freeze import TraceFreezeRecord
from app.errors import UpstreamError

from .test_api import _headers, _req  # 复用 api 测试的请求构造


async def _payload(db, trace_id: str) -> dict:
    row = await (
        await db.execute("SELECT payload FROM traces WHERE trace_id = ?", (trace_id,))
    ).fetchone()
    assert row is not None, f"trace {trace_id} not found"
    return json.loads(row["payload"])


# ─────────────────────────────────────────────────────────────
# 单元级：partial 观测写独立表，不写 traces
# ─────────────────────────────────────────────────────────────
async def test_record_stream_attempt_failure_persists_and_is_idempotent(gateway):
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-1", user_id="u1")

    inserted = await ctx.store.record_stream_attempt_failure(
        billing_id=trace.billing_id,
        trace_id=trace.trace_id,
        provider="ok",
        model="broken",
        attempt=1,
        stream_chars=5,
        error_code="upstream_error",
    )
    assert inserted is True

    # 同一请求/候选/尝试重复上报 → 幂等忽略
    inserted_again = await ctx.store.record_stream_attempt_failure(
        billing_id=trace.billing_id,
        trace_id=trace.trace_id,
        provider="ok",
        model="broken",
        attempt=1,
        stream_chars=5,
        error_code="upstream_error",
    )
    assert inserted_again is False

    db = ctx.store._require_db()
    row = await (
        await db.execute(
            "SELECT * FROM stream_attempt_failures WHERE trace_id = 'pf-1'"
        )
    ).fetchone()
    assert row is not None
    assert row["provider"] == "ok"
    assert row["model"] == "broken"
    assert row["attempt"] == 1
    assert row["stream_chars"] == 5
    assert row["error_code"] == "upstream_error"

    # 中途观测不得写入 traces（终态才写）
    trace_row = await (
        await db.execute("SELECT 1 FROM traces WHERE trace_id = 'pf-1'")
    ).fetchone()
    assert trace_row is None


async def test_record_stream_attempt_failure_zero_chars(gateway):
    """吐字数为 0 也应固化（边界：首 token 前断流但已判定为 partial）。"""
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-1b")
    ok = await ctx.store.record_stream_attempt_failure(
        billing_id=trace.billing_id,
        trace_id=trace.trace_id,
        provider="ok",
        model="m",
        attempt=1,
        stream_chars=0,
        error_code="upstream_error",
    )
    assert ok


# ─────────────────────────────────────────────────────────────
# 单元级：终态 partial_failed 只写一次，携带 provider/model/error
# ─────────────────────────────────────────────────────────────
async def test_record_failure_writes_partial_failed_once(gateway):
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-2")
    err = UpstreamError("upstream broke mid-stream", http_status=502)
    trace.update_metric("partial", True) \
         .update_metric("stream_chars", 3) \
         .update_metric("stream_started", True)

    await _record_failure(
        ctx, trace, err, [],
        already_partial=True, provider="ok", model="broken",
    )

    runtime = await _payload(ctx.store._require_db(), "pf-2")
    assert runtime["status"] == "partial_failed"
    assert runtime["partial"] is True
    assert runtime["stream_started"] is True
    assert runtime["stream_chars"] == 3
    assert runtime["provider"] == "ok"
    assert runtime["model"] == "broken"
    assert runtime["error_code"] == err.code

    # error_log 同步补齐 provider 归因
    row = await (
        await ctx.store._require_db().execute(
            "SELECT provider, error_code FROM error_log WHERE trace_id = 'pf-2'"
        )
    ).fetchone()
    assert row is not None
    assert row["provider"] == "ok"
    assert row["error_code"] == err.code


async def test_record_failure_full_error_when_no_partial(gateway):
    """未固化 partial 时，_record_failure 仍写 status=error（保持原行为）。"""
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-3")
    err = UpstreamError("boom", http_status=500)

    await _record_failure(ctx, trace, err, [], provider="ok", model="m")

    runtime = await _payload(ctx.store._require_db(), "pf-3")
    assert runtime["status"] == "error"
    assert runtime["error_code"] == err.code
    assert runtime["provider"] == "ok"


# ─────────────────────────────────────────────────────────────
# E2E：model=broken 首 chunk 后断流 → 终态 partial_failed
# ─────────────────────────────────────────────────────────────
async def test_stream_broken_midway_solidifies_partial_failed(gateway):
    ctx = gateway["ctx"]
    body = _req(model="broken", stream=True)
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert "Hello" in r.text          # 已发出首 chunk
    assert '"error"' in r.text        # 随后错误事件

    db = ctx.store._require_db()
    row = await (
        await db.execute(
            "SELECT payload FROM traces ORDER BY rowid DESC LIMIT 1"
        )
    ).fetchone()
    assert row is not None
    runtime = json.loads(row["payload"])
    assert runtime["status"] == "partial_failed"
    assert runtime["partial"] is True
    assert runtime["stream_chars"] > 0
    assert runtime["error_code"]

    # 独立观测表也应有记录
    obs = await (
        await db.execute(
            "SELECT provider, model, stream_chars, error_code "
            "FROM stream_attempt_failures ORDER BY rowid DESC LIMIT 1"
        )
    ).fetchone()
    assert obs is not None
    assert obs["provider"] == "ok"
    assert obs["model"] == "broken"
    assert obs["stream_chars"] > 0
    assert obs["error_code"]


# ─────────────────────────────────────────────────────────────
# 部分输出后失败（partial_failed）必须计入熔断失败
# ─────────────────────────────────────────────────────────────
async def test_partial_failure_is_counted_toward_circuit(gateway):
    ctx = gateway["ctx"]
    body = _req(model="broken", stream=True)
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert '"error"' in r.text

    db = ctx.store._require_db()
    row = await (
        await db.execute(
            "SELECT consecutive_failures FROM circuit_states WHERE provider='ok'"
        )
    ).fetchone()
    assert row is not None, "partial_failed 应被计入 upstream 熔断失败"
    assert row["consecutive_failures"] >= 1
