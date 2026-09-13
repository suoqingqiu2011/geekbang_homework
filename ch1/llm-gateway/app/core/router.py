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
from ..errors import BudgetExhaustedError, ModelUnavailableError
from ..storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway.router")

# 调用方未指定 max_tokens 时，按此 token 上限预估候选最大可能成本（预算核算用）
_DEFAULT_EST_TOKENS = 2048


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
        max_tokens: Optional[int] = None,
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

        ordered = self.score_candidates(cands, thinking_required)

        # ★ 缺陷C 修复：预算核算从"最小成本(1 token)"升级为"候选最大可能成本"——
        #   按 max_tokens（缺省 _DEFAULT_EST_TOKENS）× 单价预估，调用前就剔除
        #   可能超支的候选，防止实际 token 用量超预算后才被发现（费用无法撤销）。
        if budget_usd > 0:
            est_tokens = float(max_tokens) if max_tokens else _DEFAULT_EST_TOKENS
            kept: list[Candidate] = []
            for c in ordered:
                est_cost = est_tokens / 1_000_000.0 * c.cost_per_million
                if spent_usd + est_cost <= budget_usd:
                    kept.append(c)
                else:
                    logger.info(
                        "Candidate filtered by budget: %s (est max cost $%.6f > remaining $%.6f)",
                        c.model_name, est_cost, budget_usd - spent_usd,
                    )
            ordered = kept
            if not ordered:
                raise BudgetExhaustedError(
                    f"Budget exhausted: spent=${spent_usd:.6f}, budget=${budget_usd:.6f}, "
                    f"no candidate fits estimated max cost (est_tokens={int(est_tokens)})"
                )

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
