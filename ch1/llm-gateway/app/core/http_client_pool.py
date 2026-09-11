"""
SharedHttpClientPool — 网关级共享 HTTP 连接池（G15 修复，SPEC §4.13）。

问题：每个 Adapter 各自创建 httpx.AsyncClient → 连接池爆炸；
      Key 变更需重建整个客户端。
方案：每个 provider base_url 对应唯一 AsyncClient，全网关共享；
      密钥轮换仅重建对应 provider 的客户端（通过 recreate_for_url）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("llm-gateway.http-pool")


class SharedHttpClientPool:
    """按 base_url 收敛的共享 AsyncClient 池。"""

    def __init__(
        self,
        max_connections: int = 100,
        max_keepalive: int = 20,
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
    ):
        self.max_connections = max_connections
        self.max_keepalive = max_keepalive
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._url_clients: dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        logger.info("HTTP Client Pool initialized (max=%d, keepalive=%d)",
                    self.max_connections, self.max_keepalive)

    async def close(self) -> None:
        async with self._lock:
            for client in self._url_clients.values():
                await client.aclose()
            self._url_clients.clear()
            logger.info("HTTP Client Pool closed")

    def _build_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive,
        )
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.connect_timeout,
            pool=self.connect_timeout,
        )
        # trust_env=False：网关对上游应为直连，不走系统/环境代理。
        # 否则 Windows 系统代理会把本地 127.0.0.1 的上游请求转发出去返回 502。
        return httpx.AsyncClient(timeout=timeout, limits=limits, trust_env=False)

    async def get_client(self, base_url: str) -> httpx.AsyncClient:
        """获取（或创建）base_url 对应的共享客户端。"""
        existing = self._url_clients.get(base_url)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._url_clients.get(base_url)
            if existing is not None:
                return existing
            client = self._build_client()
            self._url_clients[base_url] = client
            logger.debug("Created shared client for URL: %s", base_url)
            return client

    async def recreate_for_url(self, base_url: str) -> httpx.AsyncClient:
        """
        轮换某 provider 的 API Key 时调用：销毁旧客户端，重建新客户端。
        （同一 base_url 新 Key 同样复用该客户端）
        """
        async with self._lock:
            old = self._url_clients.pop(base_url, None)
            if old is not None:
                await old.aclose()
            client = self._build_client()
            self._url_clients[base_url] = client
            logger.info("Recreated shared client for URL: %s", base_url)
            return client

    async def get_or_recreate(self, base_url: str, force: bool = False) -> httpx.AsyncClient:
        if force:
            return await self.recreate_for_url(base_url)
        return await self.get_client(base_url)
