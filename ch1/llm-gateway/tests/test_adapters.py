"""不同 Model Adapter（OpenAI 兼容 + Anthropic）协议与错误映射测试（对上游全 mock）。

覆盖：
  - OpenAI 兼容族（openai / deepseek / kimi / qwen / ollama）：Bearer 鉴权 + {base}/chat/completions
  - Anthropic：x-api-key 鉴权 + {base}/v1/messages
  - 各 provider 的鉴权失败(401)/上游 5xx/超时 错误映射
  - 成功路径的内容与 usage，以及流式 SSE

复用 conftest.multi_gateway（6 个 provider 全部指向 mock 上游）。
"""

from __future__ import annotations

import json
from typing import Any

from .mock_upstream import take_openai_requests


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer sk-test"}


def _req(model: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────
# OpenAI 兼容族：走同一 OpenAICompatAdapter，各自路由到对应 provider
# ─────────────────────────────────────────────────────────────
# 成功路径：openai(gpt-4o) / deepseek(deepseek-chat) → mock "ok" → 200 "Hello world"
# 错误映射：kimi(401) / qwen(500) / ollama(timeout) → 网关统一错误码
async def test_openai_compat_success(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("gpt-4o"), headers=_headers()
    )
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello world"
    assert data["usage"]["total_tokens"] == 20


async def test_deepseek_compat_success(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("deepseek-chat"), headers=_headers()
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "Hello world"


# 鉴权失败：mock 返回 401 → OpenAICompatAdapter 映射为 InvalidAuthError → 网关 502
async def test_kimi_auth_failure_mapping(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("kimi-k2"), headers=_headers()
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


# 上游 5xx：mock 返回 500 → 重试后仍失败，无可用 fallback → 网关透传 500
async def test_qwen_5xx_mapping(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("qwen-max"), headers=_headers()
    )
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "upstream_error"


# 超时：mock sleep 触发 http 层 read_timeout → 网关 504 upstream_timeout
async def test_ollama_timeout_mapping(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("ollama-llama3"), headers=_headers()
    )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"


# OpenAI 兼容族 · 参数透传（temperature / max_tokens）
async def test_openai_compat_param_passthrough(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions",
        json=_req("deepseek-chat", temperature=0.3, max_tokens=64),
        headers=_headers(),
    )
    assert r.status_code == 200


# OpenAI 兼容族 · 流式 SSE
async def test_openai_compat_stream(multi_gateway):
    body = _req("gpt-4o", stream=True)
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    text = r.text
    assert "Hello" in text and "world" in text
    assert "data: [DONE]" in text


# #5（双协议对称）：OpenAI 兼容族非流式请求同样向上游透传 X-Trace-Id，
# 与流式路径及 Anthropic 适配器一致，保证全链路追踪对称。
async def test_openai_compat_trace_header_non_streaming(multi_gateway):
    # 先发一次流式请求（确认捕获机制工作），再发非流式请求
    r_stream = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("gpt-4o", stream=True), headers=_headers()
    )
    assert r_stream.status_code == 200
    take_openai_requests()  # 清空流式记录，聚焦非流式

    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("gpt-4o"), headers=_headers()
    )
    assert r.status_code == 200

    captured = take_openai_requests()
    non_stream = [c for c in captured if not c["stream"]]
    # 非流式请求必须被捕获，且每一条都携带非空 X-Trace-Id（此前遗漏，现已修复）。
    # 注：网关非流式路径可能对同一次请求产生重试，因此同一 trace_id 出现多次属正常，
    # 我们仅断言"所有非流式请求都透传了 trace 头"。
    assert len(non_stream) >= 1
    assert all(c["x_trace_id"] for c in non_stream)


# 流式路径同样透传 X-Trace-Id（基线，与 #5 修复后对称）
async def test_openai_compat_trace_header_streaming(multi_gateway):
    take_openai_requests()
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("gpt-4o", stream=True), headers=_headers()
    )
    assert r.status_code == 200
    captured = take_openai_requests()
    stream = [c for c in captured if c["stream"]]
    assert len(stream) == 1
    assert stream[0]["x_trace_id"]


# ─────────────────────────────────────────────────────────────
# Anthropic Adapter：x-api-key + /v1/messages，Content 来自 content[].text，
# usage 使用 input_tokens / output_tokens，stop_reason=end_turn → finish=stop
# ─────────────────────────────────────────────────────────────
async def test_anthropic_adapter_success(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("claude-3-5-sonnet"), headers=_headers()
    )
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hi"
    assert data["choices"][0]["finish_reason"] == "stop"


async def test_anthropic_adapter_usage(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("claude-3-5-sonnet"), headers=_headers()
    )
    data = r.json()
    # mock 返回 input_tokens=3 / output_tokens=2（适配器转换为 prompt/completion）
    assert data["usage"]["prompt_tokens"] == 3
    assert data["usage"]["completion_tokens"] == 2


# Anthropic 流式：content_block_delta(text_delta) → 聚合出 "hello"，message_delta → stop
async def test_anthropic_adapter_stream(multi_gateway):
    body = _req(model="claude-3-5-sonnet", stream=True)
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    assert r.status_code == 200
    assert "hello" in r.text
    assert "data: [DONE]" in r.text


# Anthropic 流式中提取文本 delta
async def test_anthropic_adapter_stream_content_delta(multi_gateway):
    body = _req(model="claude-3-5-sonnet", stream=True)
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=body, headers=_headers()
    )
    # 逐事件解析，确认 text_delta 文本被整理进 SSE 的 data
    deltas: list[str] = []
    for line in r.text.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            ch = (evt.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            if delta.get("content"):
                deltas.append(delta["content"])
    assert "hello" in "".join(deltas)


# ─────────────────────────────────────────────────────────────
# v1/models 展示全部 provider 模型（供查看网关暴露了哪些模型）
# ─────────────────────────────────────────────────────────────
async def test_models_exposes_all_providers(multi_gateway):
    r = await multi_gateway["client"].get("/v1/models", headers=_headers())
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    for expected in ("gpt-4o", "deepseek-chat", "kimi-k2", "qwen-max", "ollama-llama3", "claude-3-5-sonnet"):
        assert expected in ids