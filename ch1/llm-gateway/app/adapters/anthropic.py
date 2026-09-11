"""
AnthropicAdapter — Anthropic Messages API 适配器（H1）。

与 OpenAI 兼容协议的差异（鉴权头 / 请求体 / SSE 事件格式 / usage 字段）全部收敛于此。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import httpx

from ..core.config_manager import ProviderConfig
from ..core.http_client_pool import SharedHttpClientPool
from ..errors import InvalidAuthError, UpstreamError, UpstreamTimeoutError
from .base import AdapterResult, BaseAdapter, StreamEvent, Usage

logger = logging.getLogger("llm-gateway.adapter.anthropic")

_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(BaseAdapter):
    """Anthropic Messages API 适配器。"""

    def __init__(self, provider: ProviderConfig, http_pool: SharedHttpClientPool):
        super().__init__(provider, http_pool)
        self._endpoint = provider.api_base_url.rstrip("/") + "/v1/messages"

    # ── 格式转换 ─────────────────────────────────────────────
    @staticmethod
    def _split_system(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[str]]:
        """将 system 消息从 messages 中剥离（Anthropic 使用独立 system 字段）。"""
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(str(m.get("content", "")))
            else:
                rest.append({"role": m["role"], "content": str(m.get("content", ""))})
        return rest, ("\n\n".join(system_parts) or None)

    def _raise_for_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if 200 <= status < 300:
            return
        body = ""
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
            raise UpstreamError(f"Upstream error {status} ({self.provider.name}): {body}", http_status=status)
        raise UpstreamError(f"Upstream error {status} ({self.provider.name}): {body}", http_status=status)

    async def _headers(self, trace_id: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self.provider.resolve_api_key(),
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        headers.update(self._trace_headers(trace_id))
        return headers

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
        rest, system = self._split_system(messages)
        body: dict[str, Any] = {
            "model": model,
            "messages": rest,
            "max_tokens": max_tokens or 2048,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if response_format is not None:
            # Anthropic 不支持原生 response_format，使用提示约束（网关侧仍会校验）
            schema_hint = response_format.get("json_schema", {}).get("name", "json")
            body["system"] = (body.get("system", "") or "") + (
                f"\n\nYou MUST respond with a single valid JSON object matching the schema: {schema_hint}."
            ).strip()
        return body

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
            resp = await client.post(self._endpoint, json=body, headers=await self._headers(trace_id))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"Upstream timeout ({self.provider.name}): {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream network error ({self.provider.name}): {exc}") from exc

        self._raise_for_status(resp)
        data = resp.json()
        latency_ms = (time.monotonic() - t0) * 1000.0

        content = "".join(
            block.get("text", "") for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens", 0)),
            completion_tokens=int(usage_raw.get("output_tokens", 0)),
        )
        stop_map = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}
        return AdapterResult(
            content=content,
            finish_reason=stop_map.get(data.get("stop_reason"), data.get("stop_reason") or "stop"),
            usage=usage,
            ttft_ms=latency_ms,
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
        t0 = time.monotonic()
        ttft: Optional[float] = None
        prompt_tokens = 0
        completion_tokens = 0
        stop_reason: Optional[str] = None
        completed = False

        try:
            async with client.stream("POST", self._endpoint, json=body, headers=await self._headers(trace_id)) as resp:
                self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        usage_raw = event.get("message", {}).get("usage") or {}
                        prompt_tokens = int(usage_raw.get("input_tokens", 0))
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                if ttft is None:
                                    ttft = (time.monotonic() - t0) * 1000.0
                                completion_tokens += len(text)
                                yield StreamEvent(delta=text, ttft_ms=ttft)
                    elif etype == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason")
                        usage_raw = event.get("usage") or {}
                        completion_tokens = int(usage_raw.get("output_tokens", completion_tokens))
                    elif etype == "error":
                        raise UpstreamError(f"Anthropic stream error: {event.get('error')}")
            completed = True
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"Upstream timeout ({self.provider.name}): {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Upstream network error ({self.provider.name}): {exc}") from exc
        finally:
            if completed:
                try:
                    yield StreamEvent(
                        delta="",
                        finish_reason=stop_reason,
                        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
                        ttft_ms=ttft,
                    )
                except GeneratorExit:
                    pass
