"""
mock_upstream — 模拟各供应商上游服务（M2：E2E 全 mock，不依赖真实第三方）。

行为由请求体 model 名前缀驱动：
  err500 / errauth / timeout → 对应错误
  broken                     → 流式首 chunk 后断连
  badjson                    → 非流式返回非法 JSON
  claude-*                   → Anthropic /v1/messages 格式
  其他                       → 正常响应（带 response_format 时返回 JSON）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


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


def _sse_events(model: str) -> list[str]:
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
    events.append(
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }
        )
        + "\n\n"
    )
    events.append("data: [DONE]\n\n")
    return events


async def _stream_broken(model: str) -> Any:
    first = _sse_events(model)[0]
    yield first
    await asyncio.sleep(0.01)
    raise RuntimeError("mock upstream: connection broken mid-stream")


def build_mock_app() -> FastAPI:
    app = FastAPI(title="mock-upstream")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        model = str(body.get("model", ""))

        if model == "err429":
            raise HTTPException(status_code=429, detail={"error": "rate limited"})
        if model == "err500":
            raise HTTPException(status_code=500, detail={"error": "boom"})
        if model == "errauth":
            raise HTTPException(status_code=401, detail={"error": "invalid key"})
        if model == "timeout":
            await asyncio.sleep(60)
            return {}

        stream = bool(body.get("stream", False))
        if not stream:
            payload = _chat_payload(model)
            # response_format 时返回 JSON 内容
            if body.get("response_format") and model != "badjson":
                payload["choices"][0]["message"]["content"] = _json_content()
            return payload

        # 流式
        if model == "broken":
            return StreamingResponse(_stream_broken(model), media_type="text/event-stream")
        return StreamingResponse(iter(_sse_events(model)), media_type="text/event-stream")

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request):
        body = await request.json()
        model = str(body.get("model", ""))
        if model == "err500":
            raise HTTPException(status_code=500, detail={"error": "boom"})
        stream = bool(body.get("stream", False))
        if stream:
            events = [
                "data: " + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}) + "\n\n",
                "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}) + "\n\n",
                "data: [DONE]\n\n",
            ]
            return StreamingResponse(iter(events), media_type="text/event-stream")
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    return app
