"""Adapter 工厂 — 按 provider 类型实例化适配器（SPEC §4.8 / §11-3）。"""

from __future__ import annotations

from typing import Any

from ..core.config_manager import ProviderConfig
from ..core.http_client_pool import SharedHttpClientPool
from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .openai_compat import OpenAICompatAdapter

# provider → 适配器类型（可扩展）
ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "openai": OpenAICompatAdapter,
    "deepseek": OpenAICompatAdapter,
    "kimi": OpenAICompatAdapter,
    "qwen": OpenAICompatAdapter,
    "ollama": OpenAICompatAdapter,
    "anthropic": AnthropicAdapter,
}


def get_adapter(
    provider: ProviderConfig,
    http_pool: SharedHttpClientPool,
) -> BaseAdapter:
    """按 provider 名称实例化适配器。未注册类型回退到 OpenAI 兼容。"""
    adapter_cls = ADAPTER_REGISTRY.get(provider.name, OpenAICompatAdapter)
    return adapter_cls(provider, http_pool)
