"""
AuthCheck — Bearer API Key 鉴权（SPEC §4.1）。

- 网关级 Key 校验（TTL 缓存加速热路径）
- 401：未携带/非法；403：无权限
- Key 可动态轮换（get_api_keys 每次实时读取配置/环境）
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from cachetools import TTLCache

from ..errors import InvalidAuthError

logger = logging.getLogger("llm-gateway.auth")


class AuthCheck:
    """API Key 校验器。"""

    def __init__(self, cache_ttl: float = 5.0) -> None:
        # key -> (valid, expire_ts)；内部指纹为 (key, 有效Key集合指纹)
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=4096, ttl=cache_ttl)

    def _valid_keys(self, get_keys) -> list[str]:
        """实时获取有效 Key 集合（支持热更新/轮换）。"""
        return get_keys()

    async def check(self, auth_header: str | None, get_keys) -> Optional[str]:
        """
        校验 Authorization 头。
        返回 None=通过；否则抛出 InvalidAuthError / ForbiddenError。
        """
        if not auth_header or not auth_header.lower().startswith("bearer "):
            raise InvalidAuthError("Missing or invalid Authorization header. Use 'Bearer <api_key>'.")

        key = auth_header[7:].strip()
        if not key:
            raise InvalidAuthError("Empty API key.")

        valid_keys = self._valid_keys(get_keys)
        # 指纹缓存：Key 集合变更（轮换）时自动失效
        fingerprint = "|".join(valid_keys)
        cache_key = f"{fingerprint}::{key}"

        cached = self._cache.get(cache_key)
        if cached is not None:
            if not cached:
                raise InvalidAuthError("Invalid API key.")
            return key

        valid = any(hmac.compare_digest(key, k) for k in valid_keys if k)
        self._cache[cache_key] = valid
        if not valid:
            logger.warning("Auth failed: invalid API key (fingerprint=%s...)", fingerprint[:8])
            raise InvalidAuthError("Invalid API key.")
        return key
