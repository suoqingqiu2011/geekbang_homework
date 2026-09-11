"""
ConfigurationManager — 配置加载与原子热更新（G17 修复，对应 SPEC §4.12）。

要点：
  - 配置来源优先级：环境变量 > .env > YAML 配置文件 > 默认值
  - reload() 先完整解析到新对象，成功后才原子替换指针，失败保留旧配置
  - 由文件系统 Watcher（见 main.py）触发 reload，无需重启服务（M4）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger("llm-gateway.config")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "gateway.yaml"


@dataclass
class ProviderConfig:
    name: str
    api_base_url: str
    api_key_env: str = ""
    cost_per_million_tokens: float = 1.0
    thinking_capability: float = 0.5
    capacity: int = 10
    timeout: float = 60.0
    model_aliases: dict[str, str] = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        """从环境变量动态解析 API Key（支持后续轮换，H4）。"""
        if not self.api_key_env:
            return ""
        return os.environ.get(self.api_key_env, "")


@dataclass
class RateLimitConfig:
    refill_rate: float = 10.0
    bucket_capacity: float = 50.0
    per_model: float = 10.0
    per_endpoint: float = 100.0
    per_project: float = 1000.0
    per_user: float = 5000.0


@dataclass
class CircuitConfig:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3


@dataclass
class HttpPoolConfig:
    max_connections: int = 100
    max_keepalive: int = 20
    connect_timeout: float = 5.0
    read_timeout: float = 60.0


@dataclass
class ModelEntry:
    name: str
    provider: str
    routing_weight: float = 1.0


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    log_level: str = "INFO"
    db_path: str = "data/metrics.db"
    tracing_ttl: float = 3600.0
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    circuit: CircuitConfig = field(default_factory=CircuitConfig)
    http_pool: HttpPoolConfig = field(default_factory=HttpPoolConfig)
    api_keys: list[str] = field(default_factory=list)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    available_models: list[ModelEntry] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return len(self.providers) > 0


def _get(d: dict, key: str, default: Any = None) -> Any:
    return d.get(key, default) if d else default


class ConfigurationManager:
    """带原子热更新的配置管理器。"""

    def __init__(self, config_path: Optional[str] = None, env_prefix: str = "GATEWAY_"):
        self._config = AppConfig()
        self._config_file_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._env_prefix = env_prefix
        self._lock = asyncio.Lock()
        self._version = 0
        self._last_load_time: float = 0.0

    # ── 公开访问 ─────────────────────────────────────────────
    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def version(self) -> int:
        return self._version

    @property
    def last_load_time(self) -> float:
        return self._last_load_time

    def get_api_keys(self) -> list[str]:
        """网关级 API Key（env 覆盖优先）。"""
        env_keys = os.environ.get(f"{self._env_prefix}API_KEYS", "")
        if env_keys.strip():
            return [k.strip() for k in env_keys.split(",") if k.strip()]
        return list(self._config.api_keys)

    # ── 加载与热更新 ─────────────────────────────────────────
    async def reload(self) -> bool:
        """
        ★ G17：原子重载。
          1) 从磁盘完整解析出新配置
          2) 成功后才原子替换 self._config
          3) 任何异常保留旧配置
        """
        async with self._lock:
            old_version = self._version
            try:
                new_config = await asyncio.to_thread(self._parse_full_config)
                object.__setattr__(self._config, "__dict__", dict(new_config.__dict__))
                self._version += 1
                self._last_load_time = time.time()
                logger.info(
                    "Configuration reloaded (v%d): %d providers, %d models",
                    self._version,
                    len(self._config.providers),
                    len(self._config.available_models),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - 解析失败不崩溃
                logger.error("Config reload FAILED (kept v%d): %s", old_version, exc, exc_info=True)
                return False

    # ── 解析 ─────────────────────────────────────────────────
    def _parse_full_config(self) -> AppConfig:
        cfg = AppConfig()
        raw: dict = {}
        if self._config_file_path.exists():
            with open(self._config_file_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        # server
        server = raw.get("server", {}) or {}
        cfg.host = self._env_str("HOST", _get(server, "host", cfg.host))
        cfg.port = self._env_int("PORT", _get(server, "port", cfg.port))
        cfg.workers = self._env_int("WORKERS", _get(server, "workers", cfg.workers))
        cfg.log_level = self._env_str("LOG_LEVEL", _get(server, "log_level", cfg.log_level))

        # storage
        storage = raw.get("storage", {}) or {}
        cfg.db_path = self._env_str("DB_PATH", _get(storage, "db_path", cfg.db_path))
        cfg.tracing_ttl = self._env_float("TRACING_TTL", _get(storage, "tracing_ttl", cfg.tracing_ttl))

        # rate_limit
        rl = raw.get("rate_limit", {}) or {}
        cfg.rate_limit = RateLimitConfig(
            refill_rate=self._env_float("RATE_REFILL", _get(rl, "refill_rate", 10.0)),
            bucket_capacity=self._env_float("RATE_BUCKET", _get(rl, "bucket_capacity", 50.0)),
            per_model=self._env_float("RATE_MODEL", _get(rl, "per_model", 10.0)),
            per_endpoint=self._env_float("RATE_ENDPOINT", _get(rl, "per_endpoint", 100.0)),
            per_project=self._env_float("RATE_PROJECT", _get(rl, "per_project", 1000.0)),
            per_user=self._env_float("RATE_USER", _get(rl, "per_user", 5000.0)),
        )

        # circuit_breaker
        cb = raw.get("circuit_breaker", {}) or {}
        cfg.circuit = CircuitConfig(
            failure_threshold=self._env_int("CB_THRESHOLD", _get(cb, "failure_threshold", 5)),
            recovery_timeout=self._env_float("CB_RECOVERY", _get(cb, "recovery_timeout", 30.0)),
            half_open_max_calls=self._env_int("CB_HALF_OPEN", _get(cb, "half_open_max_calls", 3)),
        )

        # http_pool
        hp = raw.get("http_pool", {}) or {}
        cfg.http_pool = HttpPoolConfig(
            max_connections=self._env_int("POOL_CONNS", _get(hp, "max_connections", 100)),
            max_keepalive=self._env_int("POOL_KEEPALIVE", _get(hp, "max_keepalive", 20)),
            connect_timeout=self._env_float("POOL_CONNECT_TO", _get(hp, "connect_timeout", 5.0)),
            read_timeout=self._env_float("POOL_READ_TO", _get(hp, "read_timeout", 60.0)),
        )

        # api_keys
        cfg.api_keys = [
            str(k) for k in (_get(raw, "api_keys", []) or [])
        ]

        # providers
        cfg.providers = {}
        for name, p in (_get(raw, "providers", {}) or {}).items():
            cfg.providers[name] = ProviderConfig(
                name=str(name),
                api_base_url=str(_get(p, "api_base_url", "")),
                api_key_env=str(_get(p, "api_key_env", "")),
                cost_per_million_tokens=float(_get(p, "cost_per_million_tokens", 1.0)),
                thinking_capability=float(_get(p, "thinking_capability", 0.5)),
                capacity=int(_get(p, "capacity", 10)),
                timeout=float(_get(p, "timeout", 60.0)),
                model_aliases={str(k): str(v) for k, v in (_get(p, "model_aliases", {}) or {}).items()},
            )

        # available_models
        cfg.available_models = []
        for m in (_get(raw, "available_models", []) or []):
            if isinstance(m, dict) and m.get("name") and m.get("provider"):
                cfg.available_models.append(
                    ModelEntry(
                        name=str(m["name"]),
                        provider=str(m["provider"]),
                        routing_weight=float(_get(m, "routing_weight", 1.0)),
                    )
                )

        if not cfg.providers:
            raise ValueError("No providers defined in config")
        return cfg

    # ── env 辅助 ─────────────────────────────────────────────
    def _env_str(self, key: str, default: Any) -> str:
        return os.environ.get(f"{self._env_prefix}{key}", str(default))

    def _env_int(self, key: str, default: Any) -> int:
        try:
            return int(os.environ.get(f"{self._env_prefix}{key}", str(default)))
        except (TypeError, ValueError):
            return int(default)

    def _env_float(self, key: str, default: Any) -> float:
        try:
            return float(os.environ.get(f"{self._env_prefix}{key}", str(default)))
        except (TypeError, ValueError):
            return float(default)
