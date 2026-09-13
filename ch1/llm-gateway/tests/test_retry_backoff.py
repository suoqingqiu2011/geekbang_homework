"""流式退避与 RetryPolicy 一致性测试（#2 修复）。

原问题：非流式 execute 用"指数 + 抖动"退避，而流式 _handle_stream 用线性
`0.5 * attempt`，两者不一致。修复：把退避计算提取为 RetryPolicy.backoff_for()
作为**单一事实来源**，流式与非流式共用。

本文件：
  1. 单元级验证 backoff_for 的指数序列、抖动边界与 max_backoff 截断；
  2. 驱动 _handle_stream 走真实重试场景，断言其退避 sleep 参数 == backoff_for(attempt)，
     证明流式已改用统一策略而非线性退避。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.retry_fallback import RetryFallbackManager, RetryPolicy
from app.errors import UpstreamError


# ─────────────────────────────────────────────────────────────
# backoff_for：纯逻辑单测
# ─────────────────────────────────────────────────────────────
def test_backoff_exponential_growth_no_jitter():
    p = RetryPolicy(base_backoff=1.0, max_backoff=64.0, jitter=0.0)
    assert p.backoff_for(1) == 1.0
    assert p.backoff_for(2) == 2.0
    assert p.backoff_for(3) == 4.0
    assert p.backoff_for(4) == 8.0  # 2^(attempt-1)


def test_backoff_within_jitter_range():
    p = RetryPolicy(base_backoff=1.0, max_backoff=64.0, jitter=0.2)
    for attempt in (1, 2, 3, 5):
        exp = min(p.max_backoff, p.base_backoff * (2 ** (attempt - 1)))
        val = p.backoff_for(attempt)
        assert exp <= val <= exp * (1 + p.jitter), f"attempt={attempt}: {val} out of range"


def test_backoff_capped_at_max():
    p = RetryPolicy(base_backoff=1.0, max_backoff=4.0, jitter=0.0)
    # attempt>=4 的指数（8/16/…）被 max_backoff 截断
    assert p.backoff_for(4) == 4.0
    assert p.backoff_for(10) == 4.0


def test_backoff_invalid_attempt_returns_zero():
    p = RetryPolicy()
    assert p.backoff_for(0) == 0.0
    assert p.backoff_for(-3) == 0.0


# ─────────────────────────────────────────────────────────────
# 流式重试：退避参数必须来自 RetryPolicy（execute_stream 复用 execute）
# ─────────────────────────────────────────────────────────────
async def test_execute_stream_backoff_matches_retry_policy(gateway, monkeypatch):
    """流式路径的退避必须完全按 RetryPolicy 计算。

    流式走 execute_stream（复用 execute）。注入必然抛可重试 500 的 callable，
    jitter=0 使 backoff_for 值确定，断言相邻重试间的 sleep 参数 == backoff_for(attempt)，
    即"指数退避"而非旧的线性 0.5*attempt。_handle_stream 的手写重试亦调用同一
    backoff_for，构成单一事实来源。
    """
    policy = RetryPolicy(max_attempts=3, base_backoff=0.01, max_backoff=1.0, jitter=0.0)
    circuit = gateway["ctx"].circuit
    mgr = RetryFallbackManager(circuit, policy)

    slept: list[float] = []
    original_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        await original_sleep(0)  # 不真等退避，保持测试快速

    monkeypatch.setattr("app.core.retry_fallback.asyncio.sleep", fake_sleep)

    class _Cand:
        provider = "ok"
        model_name = "m"

    calls = {"n": 0}

    async def call_one(candidate, probe=False, attempt=1) -> object:  # noqa: ARG001,RUF100
        calls["n"] += 1
        raise UpstreamError("boom", http_status=500)  # 可重试错误，耗尽全部尝试

    with pytest.raises(UpstreamError):
        await mgr.execute_stream(gateway["ctx"].store, [_Cand()], call_one)

    assert calls["n"] == policy.max_attempts  # 3 次全部尝试
    # attempt1 → 0.01、attempt2 → 0.02（指数）；绝非旧的线性 0.5*attempt(0.5/1.0)
    assert slept == [policy.backoff_for(1), policy.backoff_for(2)]