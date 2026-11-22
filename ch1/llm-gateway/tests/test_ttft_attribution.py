"""TTFT 候选切换归因与首字节后终止测试。

覆盖：
  1) D1：首 token 后失败 → 终止，不 fallback 拼接后续候选；
  2) 候选完全失败（未吐字）→ fallback，TTFT 归属最终成功候选；
  3) 单候选正常流，TTFT 正常记录；
  4) 首个成功候选后立即停止，不串流后续候选。
"""

from __future__ import annotations

import json

from app.api.chat import _handle_stream
from app.core.router import Candidate
from app.core.trace_freeze import TraceFreezeRecord
from app.schemas import ChatCompletionRequest


class _FakeRequest:
    """驱动 _handle_stream 的 request stub（is_disconnected 用于断连轮询）。"""

    async def is_disconnected(self) -> bool:
        return False


def _body(trace_id: str) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "any",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "user": "u1",
            "project": "p1",
            "trace_id": trace_id,
        }
    )


def _candidates(first_upstream: str) -> list[Candidate]:
    """候选链：候选1 行为由 first_upstream 决定，候选2 固定为 slowstart（成功，TTFT≈300ms）。"""
    return [
        Candidate(
            model_name="first", provider="ok", upstream_model=first_upstream,
            cost_per_million=3.0, thinking_capability=0.7, routing_weight=1.0,
        ),
        Candidate(
            model_name="slowstart", provider="ok", upstream_model="slowstart",
            cost_per_million=3.0, thinking_capability=0.7, routing_weight=1.0,
        ),
    ]


async def _run(ctx, trace_id: str, first_upstream: str) -> list[str]:
    trace = TraceFreezeRecord(
        trace_id=trace_id, user_id="u1", project_id="p1", stream=True
    )
    resp = await _handle_stream(
        ctx, _FakeRequest(), _body(trace_id), trace,
        [{"role": "user", "content": "hi"}], _candidates(first_upstream),
    )
    return [c async for c in resp.body_iterator]


async def _usage_ttft(ctx, trace_id: str) -> tuple[str, str, float]:
    db = ctx.store._require_db()
    row = await (
        await db.execute(
            "SELECT provider, model, ttft_ms FROM usage_metrics WHERE trace_id = ?",
            (trace_id,),
        )
    ).fetchone()
    assert row is not None, f"usage_metrics {trace_id} not found"
    return row["provider"], row["model"], row["ttft_ms"]


async def _trace_runtime(ctx, trace_id: str) -> dict:
    row = await (
        await ctx.store._require_db().execute(
            "SELECT payload FROM traces WHERE trace_id = ?", (trace_id,)
        )
    ).fetchone()
    assert row is not None, f"trace {trace_id} not found"
    return json.loads(row["payload"])


def _streamed_models(chunks: list[str]) -> set[str]:
    models: set[str] = set()
    for chunk in chunks:
        if chunk.startswith("data: [DONE]"):
            continue
        try:
            payload = json.loads(chunk[len("data: "):].strip())
        except (ValueError, KeyError):
            continue
        choices = payload.get("choices") or [{}]
        if (choices[0].get("delta") or {}).get("content"):
            models.add(payload.get("model"))
    return models


# ─────────────────────────────────────────────────────────────
# 场景1（D1）：候选1 吐字后断流 → 终止，不得 fallback 拼接候选2
# ─────────────────────────────────────────────────────────────
async def test_no_fallback_after_partial_and_trace_partial_failed(gateway):
    ctx = gateway["ctx"]
    trace_id = "partial-abort"
    chunks = await _run(ctx, trace_id, first_upstream="broken")

    live = [c for c in chunks if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    text = "".join(chunks)
    assert "Hello" in text                 # 候选1 已吐出的内容
    assert "world" not in text             # 候选2 未被调用，不得拼接
    assert _streamed_models(chunks) == {"first"}
    assert any('"error"' in c for c in live)

    runtime = await _trace_runtime(ctx, trace_id)
    assert runtime["status"] == "partial_failed"
    assert runtime["partial"] is True
    assert runtime["provider"] == "ok"
    assert runtime["model"] == "first"
    assert runtime["stream_chars"] > 0
    assert runtime["error_code"]

    # 已下发部分内容后终止：不产生 usage / 计费行
    db = ctx.store._require_db()
    usage = await (
        await db.execute(
            "SELECT 1 FROM usage_metrics WHERE trace_id = ?", (trace_id,)
        )
    ).fetchone()
    assert usage is None

    # partial 观测写独立表
    obs = await (
        await db.execute(
            "SELECT provider, model, error_code FROM stream_attempt_failures "
            "WHERE trace_id = ?",
            (trace_id,),
        )
    ).fetchone()
    assert obs is not None
    assert obs["provider"] == "ok"
    assert obs["model"] == "first"
    assert obs["error_code"]


# ─────────────────────────────────────────────────────────────
# 场景2：候选1 完全失败（上游 5xx，未吐字）→ 候选2 成功
# TTFT 应归属成功候选 slowstart
# ─────────────────────────────────────────────────────────────
async def test_ttft_attributed_to_successful_after_full_failure(gateway):
    ctx = gateway["ctx"]
    trace_id = "ttft-fullfail"
    await _run(ctx, trace_id, first_upstream="err500")

    provider, model, ttft_ms = await _usage_ttft(ctx, trace_id)
    assert model == "slowstart"
    assert provider == "ok"
    assert ttft_ms >= 250


# ─────────────────────────────────────────────────────────────
# 场景3（基线）：无候选切换，单成功候选 TTFT 仍正常记录
# ─────────────────────────────────────────────────────────────
async def test_ttft_normal_flow_successful_candidate(gateway):
    ctx = gateway["ctx"]
    trace_id = "ttft-single"
    trace = TraceFreezeRecord(trace_id=trace_id, user_id="u1", project_id="p1", stream=True)
    resp = await _handle_stream(
        ctx, _FakeRequest(), _body(trace_id), trace,
        [{"role": "user", "content": "hi"}],
        _candidates("slowstart"),  # 候选1 即成功
    )
    chunks = [c async for c in resp.body_iterator]
    text = "".join(chunks)
    assert "Hello" in text and "world" in text
    assert "data: [DONE]" in text
    _, model, ttft_ms = await _usage_ttft(ctx, trace_id)
    # 首个成功候选（model_name="first"）即停止，usage 归因不再被后续候选覆盖
    assert model == "first"
    assert ttft_ms >= 250


# ─────────────────────────────────────────────────────────────
# 场景4：多候选链中首个候选成功即停止，不得串流后续候选
# ─────────────────────────────────────────────────────────────
async def test_stream_stops_after_first_successful_candidate(gateway):
    ctx = gateway["ctx"]
    trace_id = "defectA-stop"
    cands = [
        Candidate(
            model_name="c1", provider="ok", upstream_model="ok",
            cost_per_million=1.0, thinking_capability=0.5, routing_weight=1.0,
        ),
        Candidate(
            model_name="c2", provider="ok", upstream_model="ok",
            cost_per_million=1.0, thinking_capability=0.5, routing_weight=1.0,
        ),
        Candidate(
            model_name="c3", provider="ok", upstream_model="ok",
            cost_per_million=1.0, thinking_capability=0.5, routing_weight=1.0,
        ),
    ]
    trace = TraceFreezeRecord(trace_id=trace_id, user_id="u1", project_id="p1", stream=True)
    resp = await _handle_stream(
        ctx, _FakeRequest(), _body(trace_id), trace,
        [{"role": "user", "content": "hi"}], cands,
    )
    chunks = [c async for c in resp.body_iterator]

    assert _streamed_models(chunks) == {"c1"}, "串流输出到非首个成功候选"

    provider, model, _ = await _usage_ttft(ctx, trace_id)
    assert provider == "ok"
    assert model == "c1"
