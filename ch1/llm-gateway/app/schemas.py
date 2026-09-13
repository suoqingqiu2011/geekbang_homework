"""API 请求/响应 Pydantic 模型（SPEC §5）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str
    # 预留：OpenAI 协议中 role="tool" 消息用于回传工具执行结果，
    # 通过 tool_call_id 关联对应 assistants 消息里的 tool_calls。
    tool_call_id: Optional[str] = None


class JsonSchemaFormat(BaseModel):
    name: str
    schema: dict[str, Any]


class ResponseFormat(BaseModel):
    type: Literal["json_object", "json_schema"] = "json_object"
    json_schema: Optional[JsonSchemaFormat] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI 兼容请求体（含网关扩展字段）。"""

    model: Optional[str] = None          # None → 路由器自动选择
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    response_format: Optional[ResponseFormat] = None

    # ── 网关扩展字段 ────────────────────────────────────────
    user: str = "anonymous"
    project: str = "default"
    budget_usd: float = 0.0              # 0 = 不限制（SPEC §5.2）
    trace_id: Optional[str] = None
    template_id: Optional[str] = None    # 提示词模板（SPEC §4.5）
    template_version: Optional[int] = Field(default=None, ge=1)  # None = 使用最新版本
    template_vars: dict[str, Any] = Field(default_factory=dict)
    thinking: bool = False               # 要求高思考能力（路由权重）

    @model_validator(mode="after")
    def _check(self) -> "ChatCompletionRequest":
        if self.template_id is not None and not self.template_vars:
            raise ValueError("template_vars required when template_id is set")
        if self.template_version is not None and self.template_id is None:
            raise ValueError("template_id required when template_version is set")
        return self

    @field_validator("messages")
    @classmethod
    def _nonempty(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not v:
            raise ValueError("messages must not be empty")
        if len(v) > 64:
            raise ValueError("messages exceeds limit of 64")
        return v


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    complete: bool = True     # 是否完整(上游真实)用量
    estimated: bool = False   # token 为本地估算而非上游真实计数


class ChoiceOut(BaseModel):
    index: int = 0
    message: dict[str, Any]
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChoiceOut]
    usage: UsageOut


class ModelOut(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "llm-gateway"


class ModelsResponse(BaseModel):
    object: str = "list"
    data: list[ModelOut]
