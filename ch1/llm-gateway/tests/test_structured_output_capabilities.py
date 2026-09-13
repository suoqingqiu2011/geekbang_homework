"""结构化输出能力门控：按 provider 能力降级 response_format + prompt 注入 + 400 降级重试。

覆盖：
  - 单测：_upstream_response_format 能力映射（json_schema 透传 / json_object 降级 / none 仅留名）
  - 单测：_upstream_messages 的 "json" 提示词注入
  - 集成：DeepSeek（json_object）请求 json_schema → 上游收到 json_object + json 提示
  - 集成：OpenAI（json_schema）请求 json_schema → 上游原样收到 json_schema
  - 集成：Anthropic（none）→ 提示约束注入 system，不依赖原生 response_format
  - 集成：上游对 response_format 返回 400 → 去掉后自动重试一次成功（非流式 + 流式）
"""

from __future__ import annotations

import json
from typing import Any

from app.api.chat import _upstream_messages, _upstream_response_format
from app.schemas import ChatCompletionRequest

from .mock_upstream import take_anthropic_requests, take_openai_requests

SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
    "required": ["intent"],
}

RF = {
    "type": "json_schema",
    "json_schema": {"name": "intent", "schema": SCHEMA},
}


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer sk-test"}


def _req(model: str | None = "ok", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────
# 单测：能力映射
# ─────────────────────────────────────────────────────────────
def _body_with_rf(rf: dict[str, Any]) -> ChatCompletionRequest:
    return ChatCompletionRequest(model="ok", messages=[{"role": "user", "content": "hello"}], response_format=rf)


def test_rf_json_schema_capability_passthrough():
    rf = _upstream_response_format(_body_with_rf(RF), "json_schema")
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "intent"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == SCHEMA


def test_rf_json_object_capability_downgrades_schema():
    rf = _upstream_response_format(_body_with_rf(RF), "json_object")
    assert rf == {"type": "json_object"}


def test_rf_none_capability_keeps_name_for_prompt_hint():
    rf = _upstream_response_format(_body_with_rf(RF), "none")
    assert rf == {"type": "json_schema", "json_schema": {"name": "intent"}}


def test_rf_json_object_request_forwarded_for_all_capabilities():
    body = _body_with_rf({"type": "json_object"})
    assert _upstream_response_format(body, "json_object") == {"type": "json_object"}
    assert _upstream_response_format(body, "json_schema") == {"type": "json_object"}
    assert _upstream_response_format(body, "none") == {"type": "json_object"}


def test_rf_none_when_no_response_format():
    body = ChatCompletionRequest(model="ok", messages=[{"role": "user", "content": "hello"}])
    assert _upstream_response_format(body, "json_schema") is None
    assert _upstream_response_format(body, "json_object") is None
    assert _upstream_response_format(body, "none") is None


# ─────────────────────────────────────────────────────────────
# 单测：json 提示词注入
# ─────────────────────────────────────────────────────────────
def test_upstream_messages_injects_json_hint():
    msgs = [{"role": "user", "content": "解析意图"}]
    out = _upstream_messages(msgs, {"type": "json_object"})
    assert out[-1]["role"] == "system"
    assert "json" in out[-1]["content"].lower()


def test_upstream_messages_injects_full_schema_when_schema_given():
    """★ #6 修复：注入完整 schema 定义（字段名/必填项），而非仅泛化 "json" 字样。"""
    msgs = [{"role": "user", "content": "解析意图"}]
    out = _upstream_messages(msgs, {"type": "json_object"}, schema=SCHEMA)
    assert out[-1]["role"] == "system"
    content = out[-1]["content"]
    assert "JSON Schema" in content
    # schema 定义被完整序列化进提示词（含字段名 intent 与 required）
    assert '"intent"' in content
    assert '"required"' in content


def test_upstream_messages_schema_hint_injected_even_if_msgs_contain_json():
    """schema 存在时不再走"消息含 json 就跳过"短路，必须始终注入结构约束。"""
    msgs = [{"role": "user", "content": "请以 json 格式输出"}]
    out = _upstream_messages(msgs, {"type": "json_object"}, schema=SCHEMA)
    assert out[-1]["role"] == "system"
    assert '"intent"' in out[-1]["content"]


def test_upstream_messages_no_duplicate_hint():
    msgs = [{"role": "user", "content": "请以 json 格式输出"}]
    assert _upstream_messages(msgs, {"type": "json_object"}) == msgs


def test_upstream_messages_untouched_without_rf():
    msgs = [{"role": "user", "content": "解析意图"}]
    assert _upstream_messages(msgs, None) == msgs


# ─────────────────────────────────────────────────────────────
# 集成：DeepSeek json_object 能力降级
# ─────────────────────────────────────────────────────────────
async def test_deepseek_json_schema_downgrades_to_json_object(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("deepseek-chat", response_format=RF), headers=_headers()
    )
    assert r.status_code == 200
    content = json.loads(r.json()["choices"][0]["message"]["content"])
    assert content["intent"] == "greeting"

    reqs = take_openai_requests()
    assert reqs, "upstream should have received the request"
    last = reqs[-1]
    # 能力门控：json_schema 请求被降级为 json_object
    assert last["response_format"] == {"type": "json_object"}
    # prompt 已注入 "json" 字样（DeepSeek json_object 硬性要求）
    assert any("json" in str(m.get("content", "")).lower() for m in last["messages"])


# ─────────────────────────────────────────────────────────────
# 集成：OpenAI json_schema 能力原样透传
# ─────────────────────────────────────────────────────────────
async def test_openai_json_schema_passthrough(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("gpt-4o", response_format=RF), headers=_headers()
    )
    assert r.status_code == 200
    content = json.loads(r.json()["choices"][0]["message"]["content"])
    assert content["intent"] == "greeting"

    reqs = take_openai_requests()
    last = reqs[-1]
    assert last["response_format"]["type"] == "json_schema"
    assert last["response_format"]["json_schema"]["name"] == "intent"
    assert last["response_format"]["json_schema"]["strict"] is True


# ─────────────────────────────────────────────────────────────
# 集成：Anthropic none 能力走提示约束
# ─────────────────────────────────────────────────────────────
async def test_anthropic_uses_prompt_constraint(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("claude-json", response_format=RF), headers=_headers()
    )
    assert r.status_code == 200
    content = json.loads(r.json()["choices"][0]["message"]["content"])
    assert content["intent"] == "greeting"

    reqs = take_anthropic_requests()
    assert reqs and reqs[-1]["model"] == "claude-json"
    # 纯提示约束：system 中注入 JSON 输出要求（无原生 response_format）
    assert "JSON object matching the schema" in reqs[-1]["system"]


# ─────────────────────────────────────────────────────────────
# 集成：上游 400 → 去掉 response_format 自动重试（非流式）
# ─────────────────────────────────────────────────────────────
async def test_upstream_400_degrades_retry_nonstream(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("rf400", response_format=RF), headers=_headers()
    )
    assert r.status_code == 200
    content = json.loads(r.json()["choices"][0]["message"]["content"])
    assert content["intent"] == "greeting"

    reqs = take_openai_requests()
    assert len(reqs) == 2
    assert reqs[0]["response_format"] is not None  # 第一次带 response_format → 上游 400
    assert reqs[1]["response_format"] is None      # 降级重试：去掉 response_format → 成功
    # ★ 修复回归断言：降级重试时必须注入完整 schema（含 intent 字段），
    #   仅泛化 "json" 字样无法约束字段名（#6 深层修复）。
    retry_msgs = reqs[1]["messages"]
    assert any("json" in str(m.get("content", "")).lower() for m in retry_msgs), (
        "degraded retry must carry a json hint in messages"
    )
    assert any('"intent"' in str(m.get("content", "")) for m in retry_msgs), (
        "degraded retry must embed the full schema (field names) in messages"
    )


# ─────────────────────────────────────────────────────────────
# 集成：上游 400 → 去掉 response_format 自动重试（流式）
# ─────────────────────────────────────────────────────────────
async def test_upstream_400_degrades_retry_stream(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions",
        json=_req("rf400", stream=True, response_format=RF),
        headers=_headers(),
    )
    assert r.status_code == 200
    assert "data: [DONE]" in r.text

    reqs = take_openai_requests()
    assert len(reqs) == 2
    assert reqs[0]["response_format"] is not None
    assert reqs[1]["response_format"] is None
    # ★ 修复回归断言（流式路径同源修复）：降级重试请求同样注入完整 schema
    assert any("json" in str(m.get("content", "")).lower() for m in reqs[1]["messages"]), (
        "degraded retry must carry a json hint in messages"
    )
    assert any('"intent"' in str(m.get("content", "")) for m in reqs[1]["messages"]), (
        "degraded retry must embed the full schema (field names) in messages"
    )
