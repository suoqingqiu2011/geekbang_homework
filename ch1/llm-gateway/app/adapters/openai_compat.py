"""
OpenAICompatAdapter — 兼容 OpenAI Chat Completions 协议的供应商适配器。

覆盖：OpenAI、DeepSeek、Kimi (Moonshot)、Qwen (DashScope compatible)、Ollama。
差异仅在于 base_url 与 API Key（均从 ProviderConfig 读取）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import httpx

from ..core.config_manager import ProviderConfig
from ..core.http_client_pool import SharedHttpClientPool
from ..errors import InvalidAuthError, UpstreamError, UpstreamTimeoutError
from .base import AdapterResult, BaseAdapter, StreamEvent, Usage

logger = logging.getLogger("llm-gateway.adapter.openai-compat")


class OpenAICompatAdapter(BaseAdapter):
    """OpenAI 兼容协议适配器。"""

    def __init__(self, provider: ProviderConfig, http_pool: SharedHttpClientPool):
        super().__init__(provider, http_pool)
        self._endpoint = provider.api_base_url.rstrip("/") + "/chat/completions"

    # ── 上游错误映射 ─────────────────────────────────────────
    async def _raise_for_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if 200 <= status < 300:
            return
        body = ""
        try:
            # 流式响应须先 aread 才能访问 .json()/.text（否则抛 ResponseNotRead，
            # 错误体被吞掉且 4xx 状态码无法正确映射；httpx<0.28 的 read() 为同步方法）
            await resp.aread()
        except Exception:  # noqa: BLE001
            pass
        try:
            data = resp.json()
            body = str(data.get("error", data))
        except Exception:  # noqa: BLE001
            body = resp.text[:300]
        if status in (401, 403):
            raise InvalidAuthError(f"Upstream auth failed ({self.provider.name}): {body}")
        if status == 429:
            raise UpstreamError(
                f"Upstream rate limited ({self.provider.name}): {body}",
                http_status=429, retry_after=float(resp.headers.get("retry-after", 1) or 1),
            )
        if status in (408, 425, 500, 502, 503, 504):
            raise UpstreamError(
                f"Upstream error {status} ({self.provider.name}): {body}",
                http_status=status,
            )
        raise UpstreamError(f"Upstream error {status} ({self.provider.name}): {body}", http_status=status)

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[dict[str, Any]],
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            # OpenAI 兼容 response_format（structured output 约束）
            body["response_format"] = response_format
        return body

    async def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.provider.resolve_api_key()}",
        }
        return headers

    # ── 非流式 ───────────────────────────────────────────────
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
        client = await self.http_pool.get_client(self.provider.api_base_url)
        body = self._build_body(
            messages, model, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, stream=False,
        )
        t0 = time.monotonic()
        try:
            # 与流式/Anthropic 一致：非流式同样在请求头透传 trace_id，保证全链路追踪对称
            headers = await self._headers()
            headers.update(self._trace_headers(trace_id))
            resp = await client.post(
                self._endpoint, json=body, headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"Upstream timeout ({self.provider.name}): {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream network error ({self.provider.name}): {exc}") from exc

        await self._raise_for_status(resp)
        data = resp.json()
        latency_ms = (time.monotonic() - t0) * 1000.0

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):  # 多模态内容数组
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            complete=bool(usage_raw),
            estimated=not usage_raw,
        )
        return AdapterResult(
            content=content,
            finish_reason=choice.get("finish_reason") or "stop",
            usage=usage,
            ttft_ms=latency_ms,  # 非流式视整体延迟为 TTFT 近似
            raw=data,
        )

    # ── 流式 ─────────────────────────────────────────────────
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict[str, Any]] = None,
        trace_id: str = "",
    ) -> AsyncIterator[StreamEvent]:
        client = await self.http_pool.get_client(self.provider.api_base_url)
        body = self._build_body(
            messages, model, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, stream=True,
        )
        headers = await self._headers()
        headers.update(self._trace_headers(trace_id))

        t0 = time.monotonic()
        ttft: Optional[float] = None
        prompt_tokens = 0
        completion_tokens = 0
        usage_seen = False  # 是否收到过上游真实 usage（用于估算/完整标记）

        completed = False
        try:
            async with client.stream("POST", self._endpoint, json=body, headers=headers) as resp:
                await self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    if text and ttft is None:
                        ttft = (time.monotonic() - t0) * 1000.0
                    if text:
                        completion_tokens += len(text)
                    finish = choice.get("finish_reason")
                    usage_raw = chunk.get("usage")
                    if usage_raw:
                        usage_seen = True
                        prompt_tokens = int(usage_raw.get("prompt_tokens", prompt_tokens))
                        completion_tokens = int(usage_raw.get("completion_tokens", completion_tokens))
                    if text or finish:
                        yield StreamEvent(
                            delta=text,
                            finish_reason=finish,
                            ttft_ms=ttft,
                        )
            completed = True
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"Upstream timeout ({self.provider.name}): {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream network error ({self.provider.name}): {exc}") from exc
        finally:
            # 仅在正常结束时补发 usage 汇总事件；取消/异常不补发
            if completed:
                try:
                    yield StreamEvent(
                        delta="",
                        finish_reason=None,
                        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                                    complete=usage_seen, estimated=not usage_seen),
                        ttft_ms=ttft,
                    )
                except GeneratorExit:
                    pass
