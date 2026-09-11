"""
Adapter 基类 — 统一上游交互契约（SPEC §4.8）。

所有供应商差异（鉴权头、请求字段、SSE 格式、停止原因）收敛在 Adapter 内部。
新增供应商仅需实现 BaseAdapter 并注册。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from ..core.config_manager import ProviderConfig
from ..core.http_client_pool import SharedHttpClientPool


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class AdapterResult:
    """非流式调用的标准化结果。"""

    content: str
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    ttft_ms: float = 0.0
    raw: Any = None


@dataclass
class StreamEvent:
    """流式事件的标准化表示。"""

    delta: str = ""                  # 增量文本
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None    # 仅在末尾事件携带
    ttft_ms: Optional[float] = None  # 首 token 到达时刻填充


class BaseAdapter(ABC):
    """供应商适配器抽象基类。"""

    def __init__(self, provider: ProviderConfig, http_pool: SharedHttpClientPool):
        self.provider = provider
        self.http_pool = http_pool

    # ── 由子类实现 ───────────────────────────────────────────
    @abstractmethod
    async def chat_complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        trace_id: str = "",
    ) -> AdapterResult:
        """非流式补全。异常统一抛出 app.errors.GatewayError。"""

    @abstractmethod
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        trace_id: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """流式补全（异步生成器）。调用方负责在取消时 aclose。"""

    # ── 公共工具 ─────────────────────────────────────────────
    @staticmethod
    def _trace_headers(trace_id: str) -> dict[str, str]:
        return {"X-Trace-Id": trace_id} if trace_id else {}
