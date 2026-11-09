"""API 请求/响应 Pydantic 模型（SPEC §5）。"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ── 边界上限（SPEC §8.3）：入参在网关边界拦截，避免超长文本/超深结构
#    在令牌估算、注入检测、Schema 校验等热路径上放大 CPU 与内存开销。
MAX_MESSAGE_CHARS = 100_000          # 单条消息内容
MAX_TOTAL_PROMPT_CHARS = 512_000     # 全部消息内容合计
MAX_MESSAGES = 64                    # 消息条数（与既有校验保持一致）
MAX_SCHEMA_NODES = 1_000             # JSON Schema 节点数
MAX_SCHEMA_DEPTH = 20                # JSON Schema 嵌套深度
MAX_VARS_BYTES = 64_000              # template_vars 序列化后体积
MAX_VARS_NODES = 500                 # template_vars 节点数
MAX_VARS_DEPTH = 10                  # template_vars 嵌套深度


def _count_nodes_and_depth(
    value: Any,
    max_nodes: int = MAX_SCHEMA_NODES,
    max_depth: int = MAX_SCHEMA_DEPTH,
) -> tuple[int, int]:
    """迭代统计 JSON 结构的节点数与最大嵌套深度（避免递归触发 RecursionError）。"""
    nodes = 0
    depth_max = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > depth_max:
            depth_max = depth
        if nodes > max_nodes or depth_max > max_depth:
            # 已确定超限，提前返回不必继续遍历
            return nodes, depth_max
        if isinstance(current, dict):
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, depth + 1) for v in current)
    return nodes, depth_max


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str = Field(max_length=MAX_MESSAGE_CHARS)
    # 预留：OpenAI 协议中 role="tool" 消息用于回传工具执行结果，
    # 通过 tool_call_id 关联对应 assistants 消息里的 tool_calls。
    tool_call_id: Optional[str] = Field(default=None, max_length=128)


class JsonSchemaFormat(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 字段内部命名 schema_，通过 alias="schema" 保持 wire 层键名不变，
    # 避免遮蔽 BaseModel 废弃类方法 schema() 触发 Pydantic 遮蔽警告。
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")

    @model_validator(mode="after")
    def _check_complexity(self) -> "JsonSchemaFormat":
        nodes, depth = _count_nodes_and_depth(self.schema_)
        if nodes > MAX_SCHEMA_NODES:
            raise ValueError(f"json_schema too complex: {nodes} nodes (max {MAX_SCHEMA_NODES})")
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError(f"json_schema nested too deeply: {depth} (max {MAX_SCHEMA_DEPTH})")
        return self


class ResponseFormat(BaseModel):
    type: Literal["json_object", "json_schema"] = "json_object"
    json_schema: Optional[JsonSchemaFormat] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI 兼容请求体（含网关扩展字段）。"""

    model: Optional[str] = Field(default=None, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: bool = False
    # 仅对 stream + response_format 生效：先缓冲完整输出并完成校验/修复，
    # 校验通过后再一次性下发，避免客户端先收到不可用内容。
    buffer_until_valid: bool = False
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    response_format: Optional[ResponseFormat] = None

    # ── 采样参数（OpenAI 兼容子集，均可不传；不传时上游行为不变）───────
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[str | list[str]] = None
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    seed: Optional[int] = None
    # logprobs 仅非流式生效；top_logprobs 上限对齐 OpenAI(0~20)，防止响应体积被放大
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = Field(default=None, ge=0, le=20)

    # ── 网关扩展字段 ────────────────────────────────────────
    user: str = Field(default="anonymous", max_length=128)
    project: str = Field(default="default", max_length=128)
    budget_usd: float = 0.0              # 0 = 不限制（SPEC §5.2）
    trace_id: Optional[str] = Field(default=None, max_length=128)
    template_id: Optional[str] = Field(default=None, max_length=128)  # 提示词模板（SPEC §4.5）
    template_version: Optional[int] = Field(default=None, ge=1)  # None = 使用最新版本
    template_vars: dict[str, Any] = Field(default_factory=dict)
    thinking: bool = False               # 要求高思考能力（路由权重）

    @model_validator(mode="after")
    def _check(self) -> "ChatCompletionRequest":
        if self.template_id is not None and not self.template_vars:
            raise ValueError("template_vars required when template_id is set")
        if self.template_version is not None and self.template_id is None:
            raise ValueError("template_id required when template_version is set")
        if self.top_logprobs is not None and not self.logprobs:
            raise ValueError("logprobs must be true when top_logprobs is set")
        if self.buffer_until_valid and not self.stream:
            raise ValueError("buffer_until_valid requires stream=true")
        if self.buffer_until_valid and self.response_format is None:
            raise ValueError("buffer_until_valid requires response_format")
        # 总量上限：单条与条数各自受限仍可能叠加过大，这里再做一次合计约束
        total_chars = sum(len(m.content) for m in self.messages)
        if total_chars > MAX_TOTAL_PROMPT_CHARS:
            raise ValueError(
                f"total prompt content too large: {total_chars} chars "
                f"(max {MAX_TOTAL_PROMPT_CHARS})"
            )
        return self

    @field_validator("stop")
    @classmethod
    def _stop_sequences(cls, v: Optional[str | list[str]]) -> Optional[str | list[str]]:
        """stop 支持字符串或字符串数组，数组上限 4（与 OpenAI/Anthropic 一致）。"""
        if v is None:
            return v
        seqs = [v] if isinstance(v, str) else list(v)
        if len(seqs) > 4:
            raise ValueError("stop supports at most 4 sequences")
        if any(not seq for seq in seqs):
            raise ValueError("stop sequences must be non-empty strings")
        if any(len(seq) > 256 for seq in seqs):
            raise ValueError("each stop sequence must be <= 256 characters")
        return v

    @field_validator("template_vars")
    @classmethod
    def _limit_template_vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        """模板变量体积/复杂度上限，避免超大 JSON 进入渲染与追踪链路。"""
        size = len(json.dumps(v, ensure_ascii=False))
        if size > MAX_VARS_BYTES:
            raise ValueError(
                f"template_vars too large: {size} bytes (max {MAX_VARS_BYTES})"
            )
        nodes, depth = _count_nodes_and_depth(v, MAX_VARS_NODES, MAX_VARS_DEPTH)
        if nodes > MAX_VARS_NODES or depth > MAX_VARS_DEPTH:
            raise ValueError("template_vars structure too large or too deeply nested")
        return v

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
    # 未请求 logprobs 时为 None（与 OpenAI 原生行为一致：该字段始终存在）
    logprobs: Optional[dict[str, Any]] = None


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


class TemplateOut(BaseModel):
    template_id: str
    name: str
    content: str
    version: int
    updated_at: float


class TemplateUpsertRequest(BaseModel):
    template_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    # 模板内容同样进入渲染热路径，限制体积避免超大模板拖慢/占用内存
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class TemplateRenderRequest(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    version: Optional[int] = Field(default=None, ge=1)

    @field_validator("variables")
    @classmethod
    def _limit_variables(cls, v: dict[str, Any]) -> dict[str, Any]:
        """渲染入口同样受体积/复杂度上限约束，避免绕过 chat 侧校验。"""
        size = len(json.dumps(v, ensure_ascii=False))
        if size > MAX_VARS_BYTES:
            raise ValueError(
                f"variables too large: {size} bytes (max {MAX_VARS_BYTES})"
            )
        nodes, depth = _count_nodes_and_depth(v, MAX_VARS_NODES, MAX_VARS_DEPTH)
        if nodes > MAX_VARS_NODES or depth > MAX_VARS_DEPTH:
            raise ValueError("variables structure too large or too deeply nested")
        return v
