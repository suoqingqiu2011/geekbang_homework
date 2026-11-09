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

# 预算预估 / 路由评分时的 prompt:completion 占比经验值。
# 多数对话场景 prompt 短于 completion，约 4:6；若业务场景差异大可在此调整。
_PROMPT_RATIO_DEFAULT = 0.4


def estimate_prompt_tokens(messages: list[dict]) -> int:
    """
    粗估 prompt token 数（不引入 tokenizer 依赖）。

    CJK 字符按 1 字符 ≈ 1 token，其余字符按 4 字符 ≈ 1 token。
    仅用于 context_window 软过滤，不参与计费，精度要求低。
    """
    total = 0.0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            continue
        for ch in content:
            total += 1.0 if ord(ch) > 0x2E7F else 0.25
    return max(1, int(total))


@dataclass
class Candidate:
    """单个路由候选（含上游真实模型名解析）。

    成本已拆为 prompt/completion 两价（真实账单两者单价通常差数倍），
    评分时用加权均价（按 _PROMPT_RATIO_DEFAULT 混合）保持单一维度；
    预算预估与最终计费分别按实际 prompt/completion token 用量乘对应单价。
    """

    model_name: str            # 对外模型名（OpenAI 兼容）
    provider: str
    upstream_model: str        # 供应商侧真实模型名（alias 解析后）
    cost_prompt_per_million: float = 0.0
    cost_completion_per_million: float = 0.0
    # 兼容旧字段：单一均价视图（评分维度使用）
    cost_per_million: float = 0.0
    thinking_capability: float = 0.0
    routing_weight: float = 1.0
    score: float = 0.0

    def __post_init__(self) -> None:
        """若只给了分价未给均价，自动按 prompt 占比加权；反之不覆盖均价。"""
        if self.cost_per_million == 0.0 and (
            self.cost_prompt_per_million or self.cost_completion_per_million
        ):
            self.cost_per_million = (
                self.cost_prompt_per_million * _PROMPT_RATIO_DEFAULT
                + self.cost_completion_per_million * (1.0 - _PROMPT_RATIO_DEFAULT)
            )

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """按分价估算指定 prompt/completion 用量对应的成本（预算拦截用）。"""
        return (
            prompt_tokens / 1_000_000.0 * self.cost_prompt_per_million
            + completion_tokens / 1_000_000.0 * self.cost_completion_per_million
        )


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
            # 分价优先；若 ProviderConfig 只有旧均价字段，resolve_effective_costs 自动回退
            cost_prompt, cost_completion = prov.resolve_effective_costs()
            weighted_avg = (
                cost_prompt * _PROMPT_RATIO_DEFAULT
                + cost_completion * (1.0 - _PROMPT_RATIO_DEFAULT)
            )
            cands.append(
                Candidate(
                    model_name=entry.name,
                    provider=prov.name,
                    upstream_model=alias,
                    cost_prompt_per_million=cost_prompt,
                    cost_completion_per_million=cost_completion,
                    cost_per_million=weighted_avg,
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
        prompt_tokens_estimate: int = 0,
    ) -> list[Candidate]:
        """
        返回按优先级排序的候选链。
        若请求指定模型，则仅返回该模型（及其 alias 解析）。
        上下文窗口超限的候选被过滤；若全部超限则回退到未过滤列表（软约束）。
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

        # ★ 上下文窗口软过滤：仅对显式配置了 context_window(>0) 的模型生效。
        #   若过滤后为空，回退到未过滤列表——宁可让上游返回 400，也不在本层制造硬失败，
        #   避免"配错一个窗口值就让原本可用的请求全部不可用"。
        if prompt_tokens_estimate > 0:
            windows: dict[str, int] = {}
            for entry in models:
                w = int(getattr(entry, "context_window", 0) or 0)
                if w > 0:
                    windows[entry.name] = max(windows.get(entry.name, 0), w)
            if windows:
                need = prompt_tokens_estimate + (int(max_tokens) if max_tokens else 0)
                kept_ctx = [
                    c for c in ordered
                    if windows.get(c.model_name, 0) <= 0 or need <= windows[c.model_name]
                ]
                if kept_ctx:
                    for c in ordered:
                        if c not in kept_ctx:
                            logger.info(
                                "Candidate filtered by context_window: %s (need ~%d > %d)",
                                c.model_name, need, windows[c.model_name],
                            )
                    ordered = kept_ctx
                else:
                    logger.warning(
                        "All candidates exceed context_window (need ~%d); "
                        "falling back to unfiltered list", need,
                    )

        # ★ 可用性先行：先剔除无 Key / 熔断 OPEN 的候选，再做预算过滤。
        #   否则"候选池本来就为空"会被预算块先行拦下并报 402（budget_exhausted），
        #   把"无可用模型"误报成"预算超支"——客户端据此会放弃重试而非稍后重试。
        available: list[Candidate] = []
        for c in ordered:
            # 无 Key 的 provider（api_key_env 已配置但环境变量缺失）为静态不可用：
            # 路由阶段直接剔除，避免发请求时产生非法空 Bearer 头、无效重试与误开熔断。
            prov = providers.get(c.provider)
            if prov is not None and not prov.has_api_key():
                logger.info(
                    "Candidate filtered: %s (missing api key env %s)",
                    c.provider, prov.api_key_env,
                )
                continue
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

        # ★ 缺陷C 修复：预算核算从"最小成本(1 token)"升级为"候选最大可能成本"——
        #   按 max_tokens（缺省 _DEFAULT_EST_TOKENS）× 单价预估，调用前就剔除
        #   可能超支的候选，防止实际 token 用量超预算后才被发现（费用无法撤销）。
        #   分价模式下按 prompt/completion 分别计费（典型占比 _PROMPT_RATIO_DEFAULT），
        #   更贴近真实账单（两者单价通常差数倍）。
        if budget_usd > 0:
            # prompt 用量采用请求的真实估算（长 prompt 场景不再按 completion 上限
            # 的固定占比折算而系统性低估）；completion 采用 max_tokens 上限。
            completion_est = int(max_tokens) if max_tokens else _DEFAULT_EST_TOKENS
            prompt_est = (
                int(prompt_tokens_estimate)
                if prompt_tokens_estimate > 0
                else int(completion_est * _PROMPT_RATIO_DEFAULT)
            )
            kept: list[Candidate] = []
            for c in available:
                est_cost = c.estimate_cost(prompt_est, completion_est)
                if spent_usd + est_cost <= budget_usd:
                    kept.append(c)
                else:
                    logger.info(
                        "Candidate filtered by budget: %s (est max cost $%.6f > remaining $%.6f)",
                        c.model_name, est_cost, budget_usd - spent_usd,
                    )
            available = kept
            if not available:
                raise BudgetExhaustedError(
                    f"Budget exhausted: spent=${spent_usd:.6f}, budget=${budget_usd:.6f}, "
                    f"no candidate fits estimated max cost "
                    f"(prompt_est={prompt_est}, completion_est={completion_est})"
                )

        return available
