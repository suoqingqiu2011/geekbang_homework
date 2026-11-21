"""Embedding 与 LLM 构造、重试与主备切换。"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from typing import Any, Callable, TypeVar

from llama_index.core.base.llms.types import ChatMessage, LLMMetadata, MessageRole
from llama_index.llms.openai import OpenAI

from rag_core.config import LLMConfig, RagError, Settings, require_env

T = TypeVar("T")

# 只有这些瞬时故障值得重试；鉴权/参数错误重试多少次都不会成功。
_TRANSIENT_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
    "ServiceUnavailableError",
    "Timeout",
    "TimeoutError",
}


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def _retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.8, max_total: float = 30.0) -> T:
    started = time.monotonic()
    last: Exception | None = None
    for index in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if index == attempts - 1 or not _is_retryable(exc):
                break
            delay = base_delay * (2 ** index) * (0.75 + random.random() * 0.5)
            if time.monotonic() - started + delay > max_total:
                break
            print(f"  [retry] {type(exc).__name__}，{delay:.2f}s 后重试 ({index + 2}/{attempts})", file=sys.stderr)
            time.sleep(delay)
    raise last if last else RuntimeError("retry failed")


_KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]+)")


def _brief_error(exc: Exception, limit: int = 200) -> str:
    text = " ".join(str(exc).split())
    return _KEYLIKE.sub("[REDACTED]", text)[:limit]


def get_embed_model(settings: Settings) -> Any:
    if settings.embed_backend == "cloud":
        from llama_index.embeddings.openai import OpenAIEmbedding

        return OpenAIEmbedding(
            model=settings.embed_model,
            api_base=settings.embed_base_url,
            api_key=require_env(settings.embed_key_env),
            dimensions=settings.embed_dim,
            embed_batch_size=10,
            timeout=60.0,
            max_retries=2,
        )

    endpoint = os.getenv("HF_ENDPOINT", "").strip()
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    try:
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
    except ImportError as exc:
        raise RagError(
            "RAG-E01", "本地 Embedding 依赖未安装（torch / sentence-transformers）",
            "providers.get_embed_model", "pip install -r requirements-local-embed.txt",
        ) from exc

    return HuggingFaceEmbedding(
        model_name=settings.embed_model,
        max_length=512,
        embed_batch_size=16,
        device="cpu",
        normalize=True,
        query_instruction="为这个句子生成表示以用于检索相关文章：",
        text_instruction="",
    )


class OpenAICompatible(OpenAI):
    """OpenAI 兼容网关的模型名不在 llama-index 白名单内，覆写 metadata 跳过校验。"""

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=128000,
            num_output=self.max_tokens or -1,
            is_chat_model=True,
            is_function_calling_model=False,
            model_name=self.model,
        )


def get_llm(config: LLMConfig) -> Any:
    return OpenAICompatible(
        model=config.model,
        api_base=config.base_url,
        api_key=config.api_key(),
        temperature=0.1,
        max_tokens=768,
        timeout=60.0,
        max_retries=0,
    )


def complete_with_fallback(settings: Settings, prompt: str) -> tuple[str, str]:
    failures: list[str] = []
    for config in (settings.llm_primary, settings.llm_fallback):
        if not config.available():
            failures.append(f"{config.name}: 缺少 {config.api_key_env}")
            continue
        try:
            llm = get_llm(config)
            # 必须走 chat 端点：OpenAI 兼容网关的模型名（deepseek-chat /
            # qwen3-coder-plus）不在 CHAT_MODELS 名单内，complete() 会退化成
            # /completions 并失败。OpenAICompatible 覆写 metadata 让 chat()
            # 不再触发模型名白名单校验，否则会在发请求前抛 ValueError。
            reply = _retry(
                lambda: llm.chat([ChatMessage(role=MessageRole.USER, content=prompt)])
            )
            return (reply.message.content or "").strip(), config.model
        except Exception as exc:
            detail = _brief_error(exc)
            try:
                detail = detail.replace(config.api_key(), "[REDACTED]")
            except RagError:
                pass
            failures.append(f"{config.name}: {type(exc).__name__}: {detail}")
            print(f"  [llm] {config.name} 调用失败（{type(exc).__name__}: {detail}），尝试下一个模型", file=sys.stderr)

    raise RagError(
        "RAG-E06", "所有模型均调用失败 -> " + "; ".join(failures),
        "providers.complete_with_fallback", "检查网络与额度，或在 .env 中补齐主/备模型配置",
    )
