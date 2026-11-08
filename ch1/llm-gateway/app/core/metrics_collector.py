"""
GatewayMetrics — 进程内指标收集器（Prometheus 文本格式，无第三方依赖）。

设计边界（SPEC §11 决策：不引入 Prometheus 客户端 / OTel）：
- 仅暴露低成本计数器与计量器，不改动既有存储与计费口径；
- 指标按进程维度统计；多 worker 部署时各 worker 独立计数（/metrics 命中单个 worker），
  精确的跨进程历史数据仍以 SQLite（usage_metrics / daily_aggregates）+ CLI 为准。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import DefaultDict, Optional


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


class GatewayMetrics:
    """最小可用指标集：请求量 / 耗时 / 限流拒绝 / 上游尝试 / 构建与运行态。"""

    def __init__(self, version: str = "1.0.0") -> None:
        self.version = version
        self.started_at = time.time()
        self._requests: DefaultDict[tuple[str, str], int] = defaultdict(int)
        self._duration_sum: DefaultDict[str, float] = defaultdict(float)
        self._duration_count: DefaultDict[str, int] = defaultdict(int)
        self._rate_limit_rejected = 0
        self._upstream_attempts = 0

    # ── 写入侧 ───────────────────────────────────────────────
    def record_request(self, endpoint: str, status: str, duration_s: float) -> None:
        self._requests[(endpoint, status)] += 1
        self._duration_sum[endpoint] += max(duration_s, 0.0)
        self._duration_count[endpoint] += 1

    def inc_rate_limit_rejected(self) -> None:
        self._rate_limit_rejected += 1

    def inc_upstream_attempt(self) -> None:
        self._upstream_attempts += 1

    # ── 渲染侧（Prometheus text exposition format）────────────
    def render(self, *, config_version: int, uptime_s: Optional[float] = None) -> str:
        uptime_value = (time.time() - self.started_at) if uptime_s is None else uptime_s
        lines: list[str] = []

        def section(name: str, metric_type: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")

        section(
            "llm_gateway_requests_total",
            "counter",
            "Total HTTP requests by endpoint label and result status.",
        )
        for (endpoint, status), count in sorted(self._requests.items()):
            lines.append(
                f'llm_gateway_requests_total{{endpoint="{_escape_label(endpoint)}",'
                f'status="{_escape_label(status)}"}} {count}'
            )

        section(
            "llm_gateway_request_duration_seconds",
            "summary",
            "Handler duration in seconds (for streaming requests: time until response starts).",
        )
        for endpoint, total in sorted(self._duration_sum.items()):
            label = f'endpoint="{_escape_label(endpoint)}"'
            lines.append(f"llm_gateway_request_duration_seconds_sum{{{label}}} {total:.6f}")
            lines.append(
                f"llm_gateway_request_duration_seconds_count{{{label}}} "
                f"{self._duration_count.get(endpoint, 0)}"
            )

        section(
            "llm_gateway_rate_limit_rejected_total",
            "counter",
            "Requests rejected by the gateway rate limiter.",
        )
        lines.append(f"llm_gateway_rate_limit_rejected_total {self._rate_limit_rejected}")

        section(
            "llm_gateway_upstream_attempts_total",
            "counter",
            "Upstream provider call attempts (including retries and fallbacks).",
        )
        lines.append(f"llm_gateway_upstream_attempts_total {self._upstream_attempts}")

        section("llm_gateway_config_version", "gauge", "Currently loaded configuration version.")
        lines.append(f"llm_gateway_config_version {config_version}")

        section("llm_gateway_uptime_seconds", "gauge", "Process uptime in seconds.")
        lines.append(f"llm_gateway_uptime_seconds {uptime_value:.3f}")

        section("llm_gateway_build_info", "gauge", "Gateway build information.")
        lines.append(
            f'llm_gateway_build_info{{version="{_escape_label(self.version)}"}} 1'
        )
        return "\n".join(lines) + "\n"
