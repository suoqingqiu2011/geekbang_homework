"""
mock_upstream — 模拟各供应商上游服务（M2：E2E 全 mock，不依赖真实第三方）。

行为由请求体 model 名前缀驱动：
  err500 / errauth / timeout → 对应错误
  broken                     → 流式首 chunk 后断连
  badjson                    → 非流式返回非法 JSON
  nousage                    → OpenAI 兼容返回不带 usage 的响应（验证估算/完整标记）
  claude-nousage             → Anthropic 返回不带 usage 的响应
  claude-*                   → Anthropic /v1/messages 格式
  其他                       → 正常响应（带 response_format 时返回 JSON）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

# 记录网关发往 /v1/chat/completions 的请求（含 X-Trace-Id 头），
# 供测试断言"非流式/流式上游是否收到 trace 头"（#5 双协议对称）。
_OPENAI_REQUESTS: list[dict[str, Any]] = []
# 记录网关发往 Anthropic /v1/messages 的请求（含 system 提示约束），
# 供测试断言 Anthropic 纯提示约束路径（structured_output: none）。
_ANTHROPIC_REQUESTS: list[dict[str, Any]] = []


def take_openai_requests() -> list[dict[str, Any]]:
    """取出并清空已捕获的 OpenAI 兼容上游请求记录。"""
    captured = _OPENAI_REQUESTS.copy()
    _OPENAI_REQUESTS.clear()
    return captured


def take_anthropic_requests() -> list[dict[str, Any]]:
    """取出并清空已捕获的 Anthropic 上游请求记录。"""
    captured = _ANTHROPIC_REQUESTS.copy()
    _ANTHROPIC_REQUESTS.clear()
    return captured


def _json_content() -> str:
    return json.dumps({"intent": "greeting", "items": ["a", "b"]}, ensure_ascii=False)


def _chat_payload(model: str) -> dict[str, Any]:
    # 默认返回普通文本；调用方在需要时覆盖为 JSON 内容
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
    }


def _sse_events(model: str, no_usage: bool = False) -> list[str]:
    tokens = ["Hello", " ", "world"]
    events = []
    for t in tokens:
        events.append(
            "data: "
            + json.dumps(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}],
                }
            )
            + "\n\n"
        )
    finish = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    if not no_usage:
        finish["usage"] = {"prompt_tokens": 5, "completion_tokens": 3}
    events.append("data: " + json.dumps(finish) + "\n\n")
    events.append("data: [DONE]\n\n")
    return events


async def _stream_broken(model: str) -> Any:
    first = _sse_events(model)[0]
    yield first
    await asyncio.sleep(0.01)
    raise RuntimeError("mock upstream: connection broken mid-stream")


async def _stream_slow(model: str) -> Any:
    """持续小步输出（不结束），供客户端在流中途取消/断连。

    首 chunk 后间隔发送内容 chunk：让网关持续尝试向客户端写数据，
    从而在客户端断连时能很快检测到写中断而触发取消。
    """
    content_chunk = _sse_events(model)[0]
    yield content_chunk
    for _ in range(400):  # 持续约 20s，远超测试窗口，确保不会自然结束
        await asyncio.sleep(0.05)
        yield content_chunk


async def _stream_slowstart(model: str) -> Any:
    """首 chunk 前刻意延迟，拉开 TTFT（供候选切换归因测试区分）。"""
    await asyncio.sleep(0.3)
    for chunk in _sse_events(model):
        yield chunk


def build_mock_app() -> FastAPI:
    app = FastAPI(title="mock-upstream")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        model = str(body.get("model", ""))
        # 记录本次上游收到的请求（含网关透传的 trace 头），供断言双协议对称
        _OPENAI_REQUESTS.append({
            "model": model,
            "stream": bool(body.get("stream", False)),
            "x_trace_id": request.headers.get("x-trace-id"),
            "response_format": body.get("response_format"),
            "messages": body.get("messages"),
        })

        if model == "err429":
            raise HTTPException(status_code=429, detail={"error": "rate limited"})
        if model == "err500":
            raise HTTPException(status_code=500, detail={"error": "boom"})
        if model == "errauth":
            raise HTTPException(status_code=401, detail={"error": "invalid key"})
        if model == "rf400":
            # 模拟不支持 response_format 的上游：带 response_format 即 400，
            # 去掉后正常返回（供降级容错兜底测试）。
            if body.get("response_format"):
                raise HTTPException(
                    status_code=400, detail={"error": "This response_format type is unavailable now"}
                )
            stream = bool(body.get("stream", False))
            if stream:
                return StreamingResponse(iter(_sse_events(model)), media_type="text/event-stream")
            payload = _chat_payload(model)
            payload["choices"][0]["message"]["content"] = _json_content()
            return payload
        if model == "timeout":
            await asyncio.sleep(60)
            return {}

        stream = bool(body.get("stream", False))
        if not stream:
            payload = _chat_payload(model)
            # 无 usage：验证估算/完整标记
            if model == "nousage":
                payload.pop("usage", None)
            # response_format 时返回 JSON 内容
            if body.get("response_format") and model != "badjson":
                payload["choices"][0]["message"]["content"] = _json_content()
            return payload

        # 流式
        if model == "broken":
            return StreamingResponse(_stream_broken(model), media_type="text/event-stream")
        if model == "slowstart":
            return StreamingResponse(_stream_slowstart(model), media_type="text/event-stream")
        if model == "slowstream":
            return StreamingResponse(_stream_slow(model), media_type="text/event-stream")
        if model == "nousage":
            return StreamingResponse(
                iter(_sse_events(model, no_usage=True)), media_type="text/event-stream"
            )
        return StreamingResponse(iter(_sse_events(model)), media_type="text/event-stream")

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request):
        body = await request.json()
        model = str(body.get("model", ""))
        _ANTHROPIC_REQUESTS.append({
            "model": model,
            "system": body.get("system", ""),
            "stream": bool(body.get("stream", False)),
        })
        if model == "err500":
            raise HTTPException(status_code=500, detail={"error": "boom"})
        stream = bool(body.get("stream", False))
        if stream:
            events = [
                "data: "
                + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}})
                + "\n\n",
            ]
            if model == "claude-nousage":
                # message_delta 不带 usage → 无上游 usage
                events.append(
                    "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}) + "\n\n"
                )
            else:
                events.append(
                    "data: "
                    + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}})
                    + "\n\n"
                )
            events.append("data: [DONE]\n\n")
            return StreamingResponse(iter(events), media_type="text/event-stream")
        if model == "claude-nousage":
            return {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            }
        if model == "claude-json":
            # 模拟在提示约束下返回合法 JSON（Anthropic 结构化输出纯提示约束测试）
            return {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": _json_content()}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    return app