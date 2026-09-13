"""Router 单元测试（成本/延迟/thinking 评分 + 预算 + 熔断过滤）。"""

from __future__ import annotations

import pytest

from app.core.circuit_breaker import CircuitBreakerManager
from app.core.config_manager import ProviderConfig
from app.core.router import Candidate, Router
from app.errors import BudgetExhaustedError, ModelUnavailableError


def _providers() -> dict[str, ProviderConfig]:
    return {
        "cheap": ProviderConfig(
            name="cheap", api_base_url="http://x/v1", cost_per_million_tokens=1.0,
            thinking_capability=0.5, capacity=10, timeout=10.0,
            model_aliases={"cheap-a": "cheap-a"},
        ),
        "pricey": ProviderConfig(
            name="pricey", api_base_url="http://y/v1", cost_per_million_tokens=10.0,
            thinking_capability=0.9, capacity=10, timeout=10.0,
            model_aliases={"pricey-a": "pricey-a"},
        ),
    }


def _models() -> list:
    from app.core.config_manager import ModelEntry

    return [
        ModelEntry(name="cheap-a", provider="cheap", routing_weight=1.0),
        ModelEntry(name="pricey-a", provider="pricey", routing_weight=1.0),
    ]


async def test_scoring_cost_first():
    router = Router(CircuitBreakerManager())
    cands = router.build_candidates(_models(), _providers())
    ordered = router.score_candidates(cands, thinking_required=False)
    assert ordered[0].model_name == "cheap-a"  # 成本低优先
    assert ordered[0].score < ordered[1].score


async def test_scoring_thinking_boost():
    router = Router(CircuitBreakerManager())
    cands = router.build_candidates(_models(), _providers())
    ordered = router.score_candidates(cands, thinking_required=True)
    assert ordered[0].model_name == "pricey-a"  # thinking 权重提升解决能力


async def test_requested_model_only():
    router = Router(CircuitBreakerManager())
    cands = await router.route(
        None, _models(), _providers(), requested_model="pricey-a"
    )
    assert len(cands) == 1 and cands[0].model_name == "pricey-a"


async def test_unknown_model_raises():
    router = Router(CircuitBreakerManager())
    with pytest.raises(ModelUnavailableError):
        await router.route(None, _models(), _providers(), requested_model="nope")


async def test_budget_exhausted():
    router = Router(CircuitBreakerManager())
    with pytest.raises(BudgetExhaustedError):
        await router.route(
            None, _models(), _providers(), budget_usd=1e-10, spent_usd=0.0
        )


# ─────────────────────────────────────────────────────────────
# ★ 缺陷C 回归：预算按候选最大可能成本核算，而非仅 1 token 最小成本
# ─────────────────────────────────────────────────────────────
async def test_budget_filters_pricey_candidate_keeps_cheap():
    """预算覆盖便宜候选的预估最大成本(2048t)，但不够贵候选 → 只保留便宜候选。"""
    router = Router(CircuitBreakerManager())
    cands = await router.route(
        None, _models(), _providers(), budget_usd=0.003, spent_usd=0.0
    )
    # cheap est=2048/1e6*1.0=0.002048 ≤ 0.003；pricey est=2048/1e6*10=0.02048 > 0.003
    assert [c.model_name for c in cands] == ["cheap-a"]


async def test_budget_max_tokens_reduces_est_cost():
    """指定 max_tokens 后按实际预估上限核算：max_tokens=100 时预算 0.0005 仍够 cheap。"""
    router = Router(CircuitBreakerManager())
    cands = await router.route(
        None, _models(), _providers(), budget_usd=0.0005, spent_usd=0.0, max_tokens=100
    )
    # cheap est=100/1e6*1.0=0.0001 ≤ 0.0005；pricey est=100/1e6*10=0.001 > 0.0005
    assert [c.model_name for c in cands] == ["cheap-a"]


async def test_budget_spent_usd_filters_all_when_exhausted():
    """累计花费 spent_usd 计入核算：预算被历史花费占满后无候选可留 → BudgetExhaustedError。"""
    router = Router(CircuitBreakerManager())
    with pytest.raises(BudgetExhaustedError):
        await router.route(
            None, _models(), _providers(), budget_usd=0.003, spent_usd=0.0029
        )


async def test_circuit_filter(tmp_path):
    from app.storage.metrics_store import MetricsStore

    store = MetricsStore(str(tmp_path / "cb.db"))
    await store.initialize()
    circuit = CircuitBreakerManager(failure_threshold=2, recovery_timeout=30.0, half_open_max_calls=2)
    try:
        # 打满 cheap 的熔断
        await circuit.record_failure(store, "cheap")
        await circuit.record_failure(store, "cheap")

        router = Router(circuit)
        cands = await router.route(store, _models(), _providers())
        assert all(c.provider != "cheap" for c in cands)
        assert cands[0].provider == "pricey"
    finally:
        await store.close()
