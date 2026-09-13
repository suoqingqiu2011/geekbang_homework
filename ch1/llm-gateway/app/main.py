"""
FastAPI 应用入口 — 组装、生命周期、全局错误处理、配置热更新（M4）。

启动：
    uvicorn app.main:create_app --factory
或：
    python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from watchfiles import Change, awatch

from .api.chat import router as chat_router
from .context import AppContext
from .core.auth import AuthCheck
from .core.cancellation import CancellationHandler
from .core.circuit_breaker import CircuitBreakerManager
from .core.config_manager import ConfigurationManager
from .core.http_client_pool import SharedHttpClientPool
from .core.prompt_renderer import PromptRenderer
from .core.rate_limiter import RateLimiter
from .core.retry_fallback import RetryFallbackManager
from .core.router import Router
from .core.validator import StructuredOutputValidator
from .errors import GatewayError
from .storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway")


def _jsonable(value: object) -> object:
    """将 Pydantic 校验错误递归转换为 JSON 可序列化的结构。

    exc.errors() 中自定义 validator 抛出的 ValueError 等异常对象会出现在
    'ctx' 字段里，直接 json.dumps 会失败；此处统一转为字符串。
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _build_context(cfg_mgr: ConfigurationManager) -> AppContext:
    cfg = cfg_mgr.config
    store = MetricsStore(cfg.db_path)
    http_pool = SharedHttpClientPool(
        max_connections=cfg.http_pool.max_connections,
        max_keepalive=cfg.http_pool.max_keepalive,
        connect_timeout=cfg.http_pool.connect_timeout,
        read_timeout=cfg.http_pool.read_timeout,
    )
    circuit = CircuitBreakerManager(
        failure_threshold=cfg.circuit.failure_threshold,
        recovery_timeout=cfg.circuit.recovery_timeout,
        half_open_max_calls=cfg.circuit.half_open_max_calls,
    )
    return AppContext(
        config_manager=cfg_mgr,
        store=store,
        http_pool=http_pool,
        auth=AuthCheck(),
        rate_limiter=RateLimiter(
            rates={
                "model": cfg.rate_limit.per_model,
                "endpoint": cfg.rate_limit.per_endpoint,
                "project": cfg.rate_limit.per_project,
                "user": cfg.rate_limit.per_user,
            },
            bucket_capacity=cfg.rate_limit.bucket_capacity,
        ),
        circuit=circuit,
        router=Router(circuit),
        retry=RetryFallbackManager(circuit),
        validator=StructuredOutputValidator(),
        prompt_renderer=PromptRenderer(store),
        cancellation=CancellationHandler(),
    )


async def _reconfigure(ctx: AppContext) -> None:
    """配置热更新后刷新运行时参数（M4）。"""
    cfg = ctx.config_manager.config
    await ctx.rate_limiter.update_rates(
        {
            "model": cfg.rate_limit.per_model,
            "endpoint": cfg.rate_limit.per_endpoint,
            "project": cfg.rate_limit.per_project,
            "user": cfg.rate_limit.per_user,
        }
    )
    ctx.circuit.update_params(
        cfg.circuit.failure_threshold,
        cfg.circuit.recovery_timeout,
        cfg.circuit.half_open_max_calls,
    )
    # ★ 缺陷B 修复：连接池参数纳入热更新（原仅限流/熔断刷新，http_pool 需重启才生效）
    await ctx.http_pool.update_params(
        cfg.http_pool.max_connections,
        cfg.http_pool.max_keepalive,
        cfg.http_pool.connect_timeout,
        cfg.http_pool.read_timeout,
    )
    logger.info("Runtime params refreshed after config reload (v%d)", ctx.config_manager.version)


async def _config_watcher(ctx: AppContext, stop_event: asyncio.Event) -> None:
    """监听配置文件变更 → 原子重载 + 运行时参数刷新。"""
    path = ctx.config_manager._config_file_path  # noqa: SLF001 - 内部封装简化
    try:
        async for changes in awatch(path.parent, stop_event=stop_event):
            for _change, changed_path in changes:
                if str(changed_path).endswith((".yaml", ".yml")):
                    ok = await ctx.config_manager.reload()
                    if ok:
                        await _reconfigure(ctx)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - watcher 崩溃不影响服务
        logger.exception("Config watcher stopped unexpectedly")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()  # 环境变量优先级：进程 env > .env > YAML

    cfg_mgr = ConfigurationManager(
        os.environ.get("GATEWAY_CONFIG_FILE", str(
            Path(__file__).resolve().parents[1] / "config" / "gateway.yaml"
        ))
    )
    await cfg_mgr.reload()
    if not cfg_mgr.config.loaded:
        raise RuntimeError("Gateway config failed to load — check config/gateway.yaml")

    ctx = _build_context(cfg_mgr)
    await ctx.store.initialize()
    await ctx.http_pool.initialize()
    app.state.ctx = ctx

    logging.basicConfig(
        level=getattr(logging, cfg_mgr.config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    stop_event = asyncio.Event()
    watcher = asyncio.create_task(_config_watcher(ctx, stop_event), name="config-watcher")
    logger.info("LLM Gateway started (config v%d, %d providers)",
                cfg_mgr.version, len(cfg_mgr.config.providers))

    try:
        yield
    finally:
        stop_event.set()
        watcher.cancel()
        await ctx.http_pool.close()
        await ctx.store.close()
        logger.info("LLM Gateway shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway",
        version="1.0.0",
        description="OpenAI 兼容的自建 LLM 网关（智能路由 / 容错 / 限流 / 观测 / 结构化输出）",
        lifespan=lifespan,
    )
    app.include_router(chat_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return {
            "status": "ok",
            "config_version": ctx.config_manager.version,
            "providers": list(ctx.config_manager.config.providers.keys()),
        }

    # ── 全局异常处理 ────────────────────────────────────────
    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_response())

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed",
                    "type": "invalid_request",
                    "details": _jsonable(exc.errors()),
                    "trace_id": "",
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal gateway error",
                    "type": "internal_error",
                    "trace_id": "",
                }
            },
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
