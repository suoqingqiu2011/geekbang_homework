"""
Router — 模型路由（SPEC §4.6 / §11-5）。

主策略：成本 + 延迟最低优先；thinking 权重提升"解决能力"。
辅助：Priority / Weighted Round-Robin（以 routing_weight 加权）。
硬约束：熔断状态、预算耗尽、容量。
输出：按分数升序的候选链（fallback 链）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..core.circuit_breaker import CircuitBreakerManager
from ..errors import ModelUnavailableError
from ..storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway.router")


@dataclass
class Candidate:
    """单个路由候选（含上游真实模型名解析）。"""

    model_name: str            # 对外模型名（OpenAI 兼容）
    provider: str
    upstream_model: str        # 供应商侧真实模型名（alias 解析后）
    cost_per_million: float
    thinking_capability: float
    routing_weight: float
    score: float = 0.0


class Router:
    """动态模型路由器。"""

    def __init__(
        self,
        circuit: CircuitBreakerManager,
        cost_weight: float = 0.6,
        thinking_weight: float = 0.4,
        thinking_mode_weight: float = 0.75,
    ) -> None:
        self._circuit = circuit
        self.cost_weight = cost_weight
        self.thinking_weight = thinking_weight
        self.thinking_mode_weight = thinking_mode_weight

    # ── 候选构建 ─────────────────────────────────────────────
    def build_candidates(self, models, providers) -> list[Candidate]:
        """从配置构建全部候选（按可用模型展开）。"""
        cands: list[Candidate] = []
        for entry in models:
            prov = providers.get(entry.provider)
            if prov is None:
                continue
            alias = prov.model_aliases.get(entry.name, entry.name)
            cands.append(
                Candidate(
                    model_name=entry.name,
                    provider=prov.name,
                    upstream_model=alias,
                    cost_per_million=prov.cost_per_million_tokens,
                    thinking_capability=prov.thinking_capability,
                    routing_weight=max(entry.routing_weight, 0.05),
                )
            )
        return cands

    # ── 评分 ─────────────────────────────────────────────────
    def score_candidates(self, cands: list[Candidate], thinking_required: bool) -> list[Candidate]:
        """按 成本/延迟(用 thinking 代理) + 解决能力 打分，升序排列（低=优）。"""
        if not cands:
            return []
        max_cost = max(c.cost_per_million for c in cands) or 1.0
        # thinking 模式：thinking_mode_weight 提升"解决能力"权重（成本让位）
        w_think = self.thinking_mode_weight if thinking_required else self.thinking_weight
        w_cost = 1.0 - w_think
        for c in cands:
            cost_norm = c.cost_per_million / max_cost
            lack_think = 1.0 - c.thinking_capability
            raw = w_cost * cost_norm + w_think * lack_think
            c.score = raw / c.routing_weight
        return sorted(cands, key=lambda c: c.score)

    # ── 路由主入口 ───────────────────────────────────────────
    async def route(
        self,
        store: MetricsStore,
        models,
        providers,
        *,
        requested_model: Optional[str] = None,
        thinking_required: bool = False,
        budget_usd: float = 0.0,
        spent_usd: float = 0.0,
    ) -> list[Candidate]:
        """
        返回按优先级排序的候选链。
        若请求指定模型，则仅返回该模型（及其 alias 解析）。
        熔断 OPEN / 预算耗尽的候选被过滤。
        全部被过滤 → ModelUnavailableError。
        """
        cands = self.build_candidates(models, providers)

        if requested_model:
            matched = [c for c in cands if c.model_name == requested_model]
            if not matched:
                raise ModelUnavailableError(f"Model not available: {requested_model}")
            cands = matched

        # 预算硬约束：预算无法覆盖最小成本（1 token）即视为耗尽
        if budget_usd > 0:
            min_cost = min((c.cost_per_million for c in cands), default=0.0) / 1_000_000.0
            if spent_usd + min_cost > budget_usd:
                from ..errors import BudgetExhaustedError
                raise BudgetExhaustedError(
                    f"Budget exhausted: spent=${spent_usd:.6f}, min cost=${min_cost:.6f} > budget=${budget_usd:.6f}"
                )

        ordered = self.score_candidates(cands, thinking_required)

        available: list[Candidate] = []
        for c in ordered:
            # store 为 None 时跳过熔断过滤（单元测试场景）
            if store is None:
                available.append(c)
                continue
            decision = await self._circuit.can_pass_request(store, c.provider)
            if decision.allowed:
                available.append(c)

        if not available:
            raise ModelUnavailableError(
                "No available model candidate (all filtered by circuit breaker or constraints)."
            )
        return available
