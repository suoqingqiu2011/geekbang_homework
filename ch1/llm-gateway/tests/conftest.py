"""pytest 公共夹具：mock 上游服务 + 网关应用。"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest_asyncio
import uvicorn
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

from .mock_upstream import build_mock_app  # noqa: E402


# ─────────────────────────────────────────────────────────────
# Session-scoped mock upstream server
# ─────────────────────────────────────────────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="session")
async def mock_url() -> AsyncIterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(build_mock_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.02)
    url = f"http://127.0.0.1:{port}"
    yield url
    server.should_exit = True
    thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────
# Test gateway configuration factory
# ─────────────────────────────────────────────────────────────
def write_test_config(path: Path, mock_url: str, *, rate: dict[str, float] | None = None,
                      model_limit: float | None = None, bucket_capacity: float | None = None) -> None:
    providers = {
        "fail": {
            "api_base_url": mock_url + "/v1",
            "api_key_env": "MOCK_KEY",
            "cost_per_million_tokens": 1.0,
            "thinking_capability": 0.5,
            "capacity": 10,
            "timeout": 10.0,
            "model_aliases": {"fail500": "err500", "failauth": "errauth"},
        },
        "ok": {
            "api_base_url": mock_url + "/v1",
            "api_key_env": "MOCK_KEY",
            "cost_per_million_tokens": 3.0,
            "thinking_capability": 0.7,
            "capacity": 10,
            "timeout": 10.0,
            "model_aliases": {"ok": "ok", "broken": "broken", "badjson": "badjson"},
        },
        "timeout": {
            "api_base_url": mock_url + "/v1",
            "api_key_env": "MOCK_KEY",
            "cost_per_million_tokens": 4.0,
            "thinking_capability": 0.6,
            "capacity": 10,
            "timeout": 10.0,
            "model_aliases": {"slow": "timeout"},
        },
        "anthropic": {
            "api_base_url": mock_url,
            "api_key_env": "MOCK_KEY",
            "cost_per_million_tokens": 5.0,
            "thinking_capability": 0.9,
            "capacity": 10,
            "timeout": 10.0,
            "model_aliases": {"claude-ok": "claude-ok"},
        },
    }
    models = [
        {"name": "ok", "provider": "ok", "routing_weight": 1.0},
        {"name": "fail500", "provider": "fail", "routing_weight": 1.0},
        {"name": "failauth", "provider": "fail", "routing_weight": 1.0},
        {"name": "broken", "provider": "ok", "routing_weight": 1.0},
        {"name": "badjson", "provider": "ok", "routing_weight": 1.0},
        {"name": "slow", "provider": "timeout", "routing_weight": 1.0},
        {"name": "claude-ok", "provider": "anthropic", "routing_weight": 1.0},
    ]
    cfg = {
        "server": {"host": "0.0.0.0", "port": 8000, "workers": 1, "log_level": "WARNING"},
        "storage": {"db_path": str(path.parent / "test_metrics.db"), "tracing_ttl": 3600.0},
        "rate_limit": {
            "refill_rate": 100.0,
            "bucket_capacity": bucket_capacity if bucket_capacity is not None else 500.0,
            "per_model": model_limit if model_limit is not None else 10000.0,
            "per_endpoint": (rate or {}).get("endpoint", 10000.0),
            "per_project": (rate or {}).get("project", 10000.0),
            "per_user": (rate or {}).get("user", 10000.0),
        },
        "circuit_breaker": {"failure_threshold": 2, "recovery_timeout": 30.0, "half_open_max_calls": 2},
        "http_pool": {
            "max_connections": 100, "max_keepalive": 20,
            "connect_timeout": 2.0, "read_timeout": 3.0,
        },
        "api_keys": ["sk-test"],
        "providers": providers,
        "available_models": models,
    }
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


@pytest_asyncio.fixture
async def gateway(tmp_path, mock_url):
    """启动一个指向 mock 上游的网关（每测试独立）。"""
    cfg_path = tmp_path / "gateway.yaml"
    write_test_config(cfg_path, mock_url)
    os.environ["GATEWAY_CONFIG_FILE"] = str(cfg_path)
    os.environ["GATEWAY_DB_PATH"] = str(tmp_path / "test_metrics.db")
    os.environ["MOCK_KEY"] = "sk-test"

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        # 缩短重试策略，保证测试速度
        app.state.ctx.retry.policy.max_attempts = 2
        app.state.ctx.retry.policy.base_backoff = 0.01
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway-test", timeout=20.0
        ) as client:
            yield {"client": client, "app": app, "ctx": app.state.ctx, "cfg_path": cfg_path}
