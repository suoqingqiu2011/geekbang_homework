"""prompt/completion 分价计费与预算预估回归测试（D1/D3）。"""

from __future__ import annotations

import pytest

from app.core.config_manager import ModelEntry, ProviderConfig
from app.core.router import Candidate, Router
from app.errors import BudgetExhaustedError

_HEADERS = {"Authorization": "Bearer sk-test", "Content-Type": "application/json"}


def _req(model: str, stream: bool = False) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}


# ── 纯函数：分价解析与成本估算 ───────────────────────────────
def test_resolve_effective_costs_split():
    p = ProviderConfig(
        name="p", api_base_url="x",
        cost_per_million_tokens=3.0,
        cost_prompt_per_million_tokens=1.5,
        cost_completion_per_million_tokens=4.0,
    )
    assert p.resolve_effective_costs() == (1.5, 4.0)


def test_resolve_effective_costs_fallback_single_price():
    p = ProviderConfig(name="p", api_base_url="x", cost_per_million_tokens=3.0)
    assert p.resolve_effective_costs() == (3.0, 3.0)


def test_estimate_cost_uses_separate_prices():
    c = Candidate(
        model_name="m", provider="p", upstream_model="m",
        cost_prompt_per_million=1.5, cost_completion_per_million=4.0,
    )
    # 200 prompt + 100 completion = 200*1.5/1e6 + 100*4/1e6
    assert c.estimate_cost(200, 100) == pytest.approx(0.0007)


# ── D1：预算预检必须用真实 prompt 估算 ──────────────────────
async def test_budget_precheck_uses_prompt_estimate():
    providers = {
        "p": ProviderConfig(
            name="p", api_base_url="x",
            cost_prompt_per_million_tokens=1.5,
            cost_completion_per_million_tokens=4.0,
        )
    }
    models = [ModelEntry(name="m", provider="p")]
    router = Router(None)  # store=None 时不会触达熔断

    # prompt 200 + completion 100 = $0.0007 > $0.0005 → 拦截
    with pytest.raises(BudgetExhaustedError):
        await router.route(
            None, models, providers,
            budget_usd=0.0005, prompt_tokens_estimate=200, max_tokens=100,
        )

    # prompt 50 + completion 100 = $0.000475 <= $0.0005 → 放行
    ordered = await router.route(
        None, models, providers,
        budget_usd=0.0005, prompt_tokens_estimate=50, max_tokens=100,
    )
    assert ordered[0].model_name == "m"


# ── 端到端：落库 cost_usd 必须按分价计算 ────────────────────
async def _cost_row(gateway):
    return await (
        await gateway["ctx"].store._require_db().execute(
            "SELECT prompt_tokens, completion_tokens, cost_usd FROM usage_metrics"
        )
    ).fetchone()


async def test_nonstream_billing_uses_split_prices(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("ok"), headers=_HEADERS
    )
    assert r.status_code == 200
    row = await _cost_row(gateway)
    expected = (
        row["prompt_tokens"] / 1_000_000.0 * 1.5
        + row["completion_tokens"] / 1_000_000.0 * 4.0
    )
    assert row["cost_usd"] == pytest.approx(expected, rel=1e-9)


async def test_stream_billing_uses_split_prices(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("ok", stream=True), headers=_HEADERS
    )
    assert r.status_code == 200
    row = await _cost_row(gateway)
    expected = (
        row["prompt_tokens"] / 1_000_000.0 * 1.5
        + row["completion_tokens"] / 1_000_000.0 * 4.0
    )
    assert row["cost_usd"] == pytest.approx(expected, rel=1e-9)
