"""部分输出后失败（先吐字再断流）的半程观测固化测试。

对应"流观测闭环"中与取消对称的缺口：流式请求已向客户端吐出部分内容后才失败
（如上游断连/5xx），应固化 status=partial_failed、partial=True、携带 error_code 及
半程字符数的 trace，而不是被静默归并进 success 或 error。

采用与 test_stream_cancel.py 一致的单元级验证：直接调用
`_record_partial_failure_trace` 与 `_record_failure(already_partial=True)`，
另加一条 E2E 用例（model=broken 首 chunk 后断流）验证真实链路落库。
"""

from __future__ import annotations

import json

from app.api.chat import _record_failure, _record_partial_failure_trace
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
# 单元级：_record_partial_failure_trace 固化
# ─────────────────────────────────────────────────────────────
async def test_record_partial_failure_trace_persists_flagged(gateway):
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-1", user_id="u1")
    err = UpstreamError("upstream broke mid-stream", http_status=502)

    await _record_partial_failure_trace(
        ctx, trace, provider="ok", model="broken",
        stream_chars=5, stream_started=True, err=err,
    )

    runtime = await _payload(ctx.store._require_db(), "pf-1")
    assert runtime["status"] == "partial_failed"
    assert runtime["partial"] is True
    assert runtime["stream_started"] is True
    assert runtime["stream_chars"] == 5
    assert runtime["provider"] == "ok"
    assert runtime["model"] == "broken"
    # 与取消不同：先吐后失败确实失败，携带 error_code
    assert runtime["error_code"]


async def test_record_partial_failure_trace_zero_chars(gateway):
    """吐字数为 0 也应固化（边界：首 token 前断流但标记为 partial_failed）。"""
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-1b")
    err = UpstreamError("failed", http_status=503)

    await _record_partial_failure_trace(
        ctx, trace, "ok", "m", 0, False, err,
    )

    runtime = await _payload(ctx.store._require_db(), "pf-1b")
    assert runtime["status"] == "partial_failed"
    assert runtime["stream_chars"] == 0
    assert runtime["stream_started"] is False


# ─────────────────────────────────────────────────────────────
# 单元级：最终失败不覆盖 partial 观测，仅补错误明细
# ─────────────────────────────────────────────────────────────
async def test_record_failure_preserves_partial_trace(gateway):
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-2")
    err = UpstreamError("boom", http_status=500)

    await _record_partial_failure_trace(
        ctx, trace, "ok", "broken", 3, True, err,
    )
    await _record_failure(ctx, trace, err, [], already_partial=True)

    runtime = await _payload(ctx.store._require_db(), "pf-2")
    assert runtime["status"] == "partial_failed"  # 未被覆盖为 error
    # 错误明细已落到 error_log
    row = await (
        await ctx.store._require_db().execute(
            "SELECT error_code FROM error_log WHERE trace_id = 'pf-2'"
        )
    ).fetchone()
    assert row is not None
    assert row["error_code"] == err.code


async def test_record_failure_full_error_when_no_partial(gateway):
    """未固化 partial 时，_record_failure 仍写 status=error（保持原行为）。"""
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="pf-3")
    err = UpstreamError("boom", http_status=500)

    await _record_failure(ctx, trace, err, [])

    runtime = await _payload(ctx.store._require_db(), "pf-3")
    assert runtime["status"] == "error"
    assert runtime["error_code"] == err.code


# ─────────────────────────────────────────────────────────────
# E2E：model=broken 首 chunk 后断流 → 真实链路落 partial_failed
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


# ─────────────────────────────────────────────────────────────
# 修复：部分输出后失败（partial_failed）必须计入熔断失败，
# 避免"能吐字但总中途失败"的上游长期豁免、不被降级。
# ─────────────────────────────────────────────────────────────
async def test_partial_failure_is_counted_toward_circuit(gateway):
    ctx = gateway["ctx"]
    body = _req(model="broken", stream=True)
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert '"error"' in r.text          # 先吐字后断流 → 失败事件

    db = ctx.store._require_db()
    row = await (
        await db.execute(
            "SELECT consecutive_failures FROM circuit_states WHERE provider='ok'"
        )
    ).fetchone()
    assert row is not None, "partial_failed 应被计入 upstream 熔断失败"
    assert row["consecutive_failures"] >= 1