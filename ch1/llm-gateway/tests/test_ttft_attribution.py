"""TTFT 候选切换归因测试（对应"双协议、流观测、韧性主流程"的候选链观测修复）。

问题根因：_handle_stream 中 `stream_started`/`ttft` 跨越候选链共享。当候选1在
首个 token 后失败（或完全失败）再切换候选2，候选2的 TTFT 会被旧候选的
`stream_started=True` 屏蔽，导致 usage 记录的 ttft_ms 归属到失败的候选1，
而非真正成功输出内容的候选2 —— 归因错乱。

修复：引入候选级 `candidate_first_token` 并在每次进入候选时重置，TTFT 只记录
"最终成功候选"的首 token 延迟。

本文件采用与 test_stream_cancel/partial 一致的单元级驱动：构造多候选链并直接
迭代 `_handle_stream` 返回的 StreamingResponse.body_iterator，全程走真实
upstream 流（httpx pool）与落库，断言 usage_metrics.ttft_ms 归属成功候选。
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
    """候选链：候选1 行为由 first_upstream 决定（失败），候选2 固定为 slowstart（成功，TTFT≈300ms）。"""
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


# ─────────────────────────────────────────────────────────────
# 场景1：候选1 吐字后断流（partial_failed）→ 候选2 成功
# TTFT 应归属成功候选 slowstart（≈300ms），而非候选1 broken（≈0ms）
# ─────────────────────────────────────────────────────────────
async def test_ttft_attributed_to_successful_after_partial_failure(gateway):
    ctx = gateway["ctx"]
    trace_id = "ttft-partial"
    await _run(ctx, trace_id, first_upstream="broken")

    provider, model, ttft_ms = await _usage_ttft(ctx, trace_id)
    assert provider == "ok"
    assert model == "slowstart"          # 计费/归因给成功候选2
    assert ttft_ms >= 250                # ≈300ms 的首 token 延迟（归因成功候选2）


# ─────────────────────────────────────────────────────────────
# 场景2：候选1 完全失败（上游 5xx，未吐字）→ 候选2 成功
# 验证切换前无吐字时，TTFT 同样归属最终成功候选
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
# 防止修复引入回归（候选级重置不能丢正常流的 TTFT）
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
    assert "Hello" in text and "world" in text  # SSE 各 delta 分属不同事件，需拼接后断言
    assert "data: [DONE]" in text               # 成功流完整结束
    _, model, ttft_ms = await _usage_ttft(ctx, trace_id)
    # ★ 缺陷A 修复后：首个成功候选（model_name="first"）即停止，
    #   usage 归因不再被后续候选（slowstart）覆盖。
    assert model == "first"
    assert ttft_ms >= 250


# ─────────────────────────────────────────────────────────────
# 场景4（★ 缺陷A 回归）：多候选链中首个候选成功即停止，不得串流后续候选
# 修复前：成功候选后的候选循环未 break，会继续向候选2/3 发起流式请求，
# 客户端收到多 provider 拼接的输出，usage/TTFT 归因被后续候选覆盖。
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

    # 只下发首个成功候选（c1）的内容 chunk，不得出现 c2/c3 的流
    streamed_models = set()
    for c in chunks:
        if c.startswith("data: [DONE]"):
            continue
        payload = json.loads(c[len("data: "):].strip())
        choices = payload.get("choices") or [{}]
        if (choices[0].get("delta") or {}).get("content"):
            streamed_models.add(payload["model"])
    assert streamed_models == {"c1"}, f"串流输出到非首个成功候选: {streamed_models}"

    # usage/TTFT 归因于首个成功候选 c1（修复前会被 c3 覆盖）
    provider, model, _ = await _usage_ttft(ctx, trace_id)
    assert provider == "ok"
    assert model == "c1"