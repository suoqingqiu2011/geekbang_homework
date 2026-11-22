"""流式韧性 E2E：RF 降级、auth 统一 502、partial 观测落库。"""

from __future__ import annotations

import json

from .mock_upstream import take_openai_requests
from .test_api import _headers, _req

_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "items"],
}


async def _usage_row(ctx, trace_id: str):
    return await (
        await ctx.store._require_db().execute(
            "SELECT provider, model, attempts, estimated, complete "
            "FROM usage_metrics WHERE trace_id = ?",
            (trace_id,),
        )
    ).fetchone()


async def _trace_row(ctx, trace_id: str):
    return await (
        await ctx.store._require_db().execute(
            "SELECT provider, model, status, error_code FROM traces WHERE trace_id = ?",
            (trace_id,),
        )
    ).fetchone()


# ─────────────────────────────────────────────────────────────
# D4：400 response_format → 流式降级一次，attempt 仍为 1
# ─────────────────────────────────────────────────────────────
async def test_stream_rf400_degrades_without_extra_attempt(gateway):
    take_openai_requests()
    body = _req(
        model="rf400",
        stream=True,
        trace_id="rf400-stream",
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": _SCHEMA},
        },
    )
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert "Hello" in r.text and "data: [DONE]" in r.text
    assert '"type": "validation"' in r.text or '"status": "ok"' in r.text

    captured = [c for c in take_openai_requests() if c["stream"]]
    assert len(captured) == 2
    assert captured[0]["response_format"] is not None
    assert captured[1]["response_format"] is None

    row = await _usage_row(gateway["ctx"], "rf400-stream")
    assert row is not None
    assert row["attempts"] == 1      # 降级不消耗逻辑 attempt
    assert row["estimated"] == 0     # 降级后 mock 仍回传 usage
    assert row["complete"] == 1


# ─────────────────────────────────────────────────────────────
# D3：流式上游 401 → SSE error 事件统一 upstream_error/502
# ─────────────────────────────────────────────────────────────
async def test_stream_upstream_auth_maps_to_502(gateway):
    body = _req(model="failauth", stream=True, trace_id="auth-stream")
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200  # SSE 头已发出，错误在事件体中
    assert '"code": "upstream_error"' in r.text
    assert '"invalid_auth"' not in r.text

    trace = await _trace_row(gateway["ctx"], "auth-stream")
    assert trace is not None
    assert trace["status"] == "error"
    assert trace["error_code"] == "upstream_error"
    assert trace["provider"] == "fail"
    assert trace["model"] == "failauth"

    # error_log 同步补齐 provider 归因
    err = await (
        await gateway["ctx"].store._require_db().execute(
            "SELECT provider, error_code FROM error_log WHERE trace_id = ?",
            ("auth-stream",),
        )
    ).fetchone()
    assert err is not None
    assert err["provider"] == "fail"
    assert err["error_code"] == "upstream_error"


# ─────────────────────────────────────────────────────────────
# D1 + D2：首字节后断流 → partial_failed；不写 usage；观测独立表
# ─────────────────────────────────────────────────────────────
async def test_stream_partial_failure_is_observed_not_billed(gateway):
    body = _req(model="broken", stream=True, trace_id="partial-e2e")
    r = await gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert "Hello" in r.text
    assert '"error"' in r.text

    trace = await _trace_row(gateway["ctx"], "partial-e2e")
    assert trace is not None
    assert trace["status"] == "partial_failed"
    assert trace["provider"] == "ok"
    assert trace["model"] == "broken"

    usage = await _usage_row(gateway["ctx"], "partial-e2e")
    assert usage is None  # 已吐字后失败：不产生 usage/计费

    obs = await (
        await gateway["ctx"].store._require_db().execute(
            "SELECT provider, model, stream_chars, error_code "
            "FROM stream_attempt_failures WHERE trace_id = ?",
            ("partial-e2e",),
        )
    ).fetchone()
    assert obs is not None
    assert obs["provider"] == "ok"
    assert obs["model"] == "broken"
    assert obs["stream_chars"] > 0
    assert obs["error_code"]
