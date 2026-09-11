"""AppContext — 运行时组件容器（挂在 app.state.ctx）。"""

from __future__ import annotations

from dataclasses import dataclass

from .core.auth import AuthCheck
from .core.cancellation import CancellationHandler
from .core.circuit_breaker import CircuitBreakerManager
from .core.config_manager import ConfigurationManager
from .core.http_client_pool import SharedHttpClientPool
from .core.prompt_renderer import PromptRenderer
from .core.rate_limiter import RateLimiter
from .core.retry_fallback import RetryFallbackManager, RetryPolicy
from .core.router import Router
from .core.validator import StructuredOutputValidator
from .storage.metrics_store import MetricsStore


@dataclass
class AppContext:
    config_manager: ConfigurationManager
    store: MetricsStore
    http_pool: SharedHttpClientPool
    auth: AuthCheck
    rate_limiter: RateLimiter
    circuit: CircuitBreakerManager
    router: Router
    retry: RetryFallbackManager
    validator: StructuredOutputValidator
    prompt_renderer: PromptRenderer
    cancellation: CancellationHandler
