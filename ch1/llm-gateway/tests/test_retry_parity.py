"""RetryFallbackManager 单点策略的 parity / 决策测试。

覆盖 D1–D5：
  - D1 首字节后失败终止，不 fallback；
  - D2 partial 观测由 hook 写入（本文件验证 hook 调用与终态结果）；
  - D3 上游 401/403 统一 502；
  - D4 400 response_format 降级不消耗 attempt；
  - D5 成功只记一次熔断、无 probe 分支。
"""

from __future__ import annotations

import pytest

from app.adapters.base import AdapterResult, StreamEvent, Usage
from app.core.circuit_breaker import CircuitBreakerManager, CircuitDecision
from app.core.retry_fallback import (
    FailureAction,
    RetryFallbackManager,
    RetryPolicy,
)
from app.errors import (
    InvalidAuthError,
    StructuredOutputError,
    UpstreamError,
    UpstreamTimeoutError,
)


class _Cand:
    def __init__(self, provider: str, model: str = "m"):
        self.provider = provider
        self.model_name = model


class _CountingCircuit:
    """计数用熔断 stub（验证成功/失败记账次数）。"""

    def __init__(self):
        self.success = 0
        self.failure = 0

    async def can_pass_request(self, store, provider):
        return CircuitDecision(allowed=True, reason="")

    async def record_success(self, store, provider):
        self.success += 1

    async def record_failure(self, store, provider):
        self.failure += 1


def _mgr(circuit=None, *, max_attempts: int = 3) -> RetryFallbackManager:
    return RetryFallbackManager(
        circuit or _CountingCircuit(),
        RetryPolicy(max_attempts=max_attempts, base_backoff=0.01, jitter=0.0),
    )


def _decide(mgr: RetryFallbackManager, exc, attempt: int = 1, **kwargs):
    return mgr.decide_failure(
        _Cand("p"), exc, attempt,
        stream_started=kwargs.get("stream_started", False),
        rf_available=kwargs.get("rf_available", False),
        rf_degraded=kwargs.get("rf_degraded", False),
    )


# ─────────────────────────────────────────────────────────────
# decide_failure 决策矩阵
# ─────────────────────────────────────────────────────────────
def test_decide_retryable_500_retries_with_backoff():
    mgr = _mgr()
    d = _decide(mgr, UpstreamError("boom", http_status=500), attempt=1)
    assert d.action is FailureAction.RETRY
    assert d.backoff == mgr.policy.backoff_for(1)
    assert d.record_circuit_failure is False


def test_decide_500_exhausted_switches_candidate_and_records_failure():
    mgr = _mgr(max_attempts=2)
    d = _decide(mgr, UpstreamError("boom", http_status=500), attempt=2)
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.record_circuit_failure is True


def test_decide_auth_maps_502_and_does_not_retry():
    mgr = _mgr()
    d = _decide(mgr, InvalidAuthError("invalid key"), attempt=1)
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.error.http_status == 502
    assert d.error.code == "upstream_error"
    assert d.record_circuit_failure is True


def test_decide_upstream_401_maps_502():
    mgr = _mgr()
    d = _decide(mgr, UpstreamError("unauthorized", http_status=401), attempt=1)
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.error.http_status == 502
    assert d.record_circuit_failure is True


def test_decide_config_missing_skips_circuit():
    mgr = _mgr()
    d = _decide(
        mgr, InvalidAuthError("missing key", config_missing=True), attempt=1
    )
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.error.http_status == 502
    assert d.record_circuit_failure is False


def test_decide_rf400_degrades_without_consuming_attempt():
    mgr = _mgr(max_attempts=1)
    d = _decide(
        mgr, UpstreamError("rf unsupported", http_status=400),
        attempt=1, rf_available=True, rf_degraded=False,
    )
    assert d.action is FailureAction.DEGRADE_RF
    assert d.record_circuit_failure is False


def test_decide_rf400_after_degrade_switches_candidate():
    mgr = _mgr(max_attempts=1)
    d = _decide(
        mgr, UpstreamError("rf unsupported", http_status=400),
        attempt=1, rf_available=True, rf_degraded=True,
    )
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.record_circuit_failure is True


def test_decide_400_without_rf_switches_candidate():
    mgr = _mgr()
    d = _decide(
        mgr, UpstreamError("bad request", http_status=400),
        attempt=1, rf_available=False,
    )
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.record_circuit_failure is True


def test_decide_committed_stream_aborts():
    mgr = _mgr()
    d = _decide(
        mgr, UpstreamError("boom", http_status=500),
        attempt=1, stream_started=True,
    )
    assert d.action is FailureAction.ABORT_STREAM
    assert d.record_circuit_failure is True


def test_decide_committed_config_missing_aborts_without_circuit():
    mgr = _mgr()
    d = _decide(
        mgr, InvalidAuthError("missing key", config_missing=True),
        attempt=1, stream_started=True,
    )
    assert d.action is FailureAction.ABORT_STREAM
    assert d.error.http_status == 502
    assert d.record_circuit_failure is False


def test_decide_timeout_is_retryable():
    mgr = _mgr()
    d = _decide(mgr, UpstreamTimeoutError("timeout"), attempt=1)
    assert d.action is FailureAction.RETRY


def test_decide_structured_output_error_is_not_retryable():
    mgr = _mgr()
    d = _decide(mgr, StructuredOutputError("bad json"), attempt=1)
    assert d.action is FailureAction.NEXT_CANDIDATE
    assert d.record_circuit_failure is True


def test_decide_generic_exception_normalized_and_retryable():
    mgr = _mgr()
    d = _decide(mgr, RuntimeError("connection reset"), attempt=1)
    assert d.action is FailureAction.RETRY
    assert d.error.code == "upstream_error"


# ─────────────────────────────────────────────────────────────
# execute / stream parity：候选失败 → fallback 成功
# ─────────────────────────────────────────────────────────────
async def test_execute_and_stream_share_fallback_state_machine(gateway):
    store = gateway["ctx"].store
    circuit = CircuitBreakerManager(
        failure_threshold=5, recovery_timeout=30.0, half_open_max_calls=2
    )

    # 非流式：pa 两次 500 → pb 成功
    mgr = _mgr(circuit, max_attempts=2)
    calls = {"pa": 0, "pb": 0}

    async def call_one(candidate, attempt=1, rf_degraded=False):
        if candidate.provider == "pa":
            calls["pa"] += 1
            raise UpstreamError("boom", http_status=500)
        calls["pb"] += 1
        return AdapterResult(content="ok", usage=Usage(prompt_tokens=1, completion_tokens=1))

    outcome = await mgr.execute(store, [_Cand("pa"), _Cand("pb")], call_one)
    assert outcome.success is True
    assert outcome.provider == "pb"
    assert outcome.attempts == 3          # pa 2 次 + pb 1 次
    assert calls == {"pa": 2, "pb": 1}
    pa_state = await store.get_circuit_state("pa")
    assert pa_state["consecutive_failures"] == 1  # 候选终态只记一次失败
    pb_state = await store.get_circuit_state("pb")
    assert pb_state["consecutive_failures"] == 0

    # 流式：sa 两次 500 → sb 成功；attempts/熔断应与非流式一致
    mgr2 = _mgr(circuit, max_attempts=2)
    opened = {"sa": 0, "sb": 0}

    async def open_stream(candidate, attempt=1, rf_degraded=False):
        opened[candidate.provider] += 1
        if candidate.provider == "sa":
            async def bad():
                raise UpstreamError("boom", http_status=500)
                yield  # pragma: no cover
            return bad()

        async def good():
            yield StreamEvent(delta="ok", ttft_ms=1.0)
            yield StreamEvent(
                usage=Usage(prompt_tokens=1, completion_tokens=1)
            )
        return good()

    items = [
        item async for item in mgr2.stream(
            store, [_Cand("sa"), _Cand("sb")], open_stream
        )
    ]
    finished = [i for i in items if i.kind == "finished"]
    assert len(finished) == 1
    assert finished[0].outcome.success is True
    assert finished[0].outcome.provider == "sb"
    assert finished[0].outcome.attempts == 3
    assert opened == {"sa": 2, "sb": 1}
    sa_state = await store.get_circuit_state("sa")
    assert sa_state["consecutive_failures"] == 1
    sb_state = await store.get_circuit_state("sb")
    assert sb_state["consecutive_failures"] == 0


# ─────────────────────────────────────────────────────────────
# D4：RF 400 降级不消耗 attempt（execute / stream 一致）
# ─────────────────────────────────────────────────────────────
async def test_execute_rf_degrade_does_not_consume_attempt(gateway):
    store = gateway["ctx"].store
    mgr = _mgr(max_attempts=1)
    calls = {"n": 0}

    async def call_one(candidate, attempt=1, rf_degraded=False):
        calls["n"] += 1
        if not rf_degraded:
            raise UpstreamError("rf unsupported", http_status=400)
        return AdapterResult(content="ok")

    outcome = await mgr.execute(
        store, [_Cand("rfx")], call_one, rf_available=True
    )
    assert outcome.success is True
    assert outcome.attempts == 1       # 逻辑 attempt 只有 1 次
    assert calls["n"] == 2             # 但上游实际调用了 2 次
    state = await store.get_circuit_state("rfx")
    assert state["consecutive_failures"] == 0


async def test_stream_rf_degrade_does_not_consume_attempt(gateway):
    store = gateway["ctx"].store
    mgr = _mgr(max_attempts=1)
    calls = {"n": 0}

    async def open_stream(candidate, attempt=1, rf_degraded=False):
        calls["n"] += 1
        if not rf_degraded:
            async def bad():
                raise UpstreamError("rf unsupported", http_status=400)
                yield  # pragma: no cover
            return bad()

        async def good():
            yield StreamEvent(delta="ok", ttft_ms=1.0)
            yield StreamEvent(usage=Usage(prompt_tokens=1, completion_tokens=1))
        return good()

    items = [
        item async for item in mgr.stream(
            store, [_Cand("rfs")], open_stream, rf_available=True
        )
    ]
    finished = [i for i in items if i.kind == "finished"]
    assert finished and finished[0].outcome.success is True
    assert finished[0].outcome.attempts == 1
    assert calls["n"] == 2
    state = await store.get_circuit_state("rfs")
    assert state["consecutive_failures"] == 0


# ─────────────────────────────────────────────────────────────
# D1：首字节后失败终止，不 fallback
# ─────────────────────────────────────────────────────────────
async def test_stream_aborts_after_first_byte_without_fallback(gateway):
    store = gateway["ctx"].store
    circuit = CircuitBreakerManager(
        failure_threshold=5, recovery_timeout=30.0, half_open_max_calls=2
    )
    mgr = _mgr(circuit, max_attempts=2)
    opened = {"a": 0, "b": 0}
    partials: list[tuple] = []

    async def open_stream(candidate, attempt=1, rf_degraded=False):
        opened[candidate.provider] += 1
        if candidate.provider == "a":
            async def partial():
                yield StreamEvent(delta="Hello", ttft_ms=1.0)
                raise UpstreamError("broke after first byte", http_status=500)
            return partial()

        async def good():
            yield StreamEvent(delta="world", ttft_ms=1.0)
            yield StreamEvent(usage=Usage(prompt_tokens=1, completion_tokens=1))
        return good()

    async def on_partial(candidate, err, attempt, chars):
        partials.append((candidate.provider, err.code, attempt, chars))

    items = [
        item async for item in mgr.stream(
            store, [_Cand("a"), _Cand("b")], open_stream,
            on_partial_failure=on_partial,
        )
    ]

    assert opened == {"a": 1, "b": 0}  # 候选 b 绝不能被调用
    deltas = [i.event.delta for i in items if i.kind == "delta"]
    assert deltas == ["Hello"]
    assert any(i.kind == "partial_failed" for i in items)
    finished = [i for i in items if i.kind == "finished"]
    assert finished and finished[0].outcome.success is False
    assert finished[0].outcome.partial is True
    assert partials and partials[0][0] == "a" and partials[0][3] == len("Hello")

    state = await store.get_circuit_state("a")
    assert state["consecutive_failures"] == 1


# ─────────────────────────────────────────────────────────────
# D3 + D5：流式 auth 统一 502；成功只记一次熔断
# ─────────────────────────────────────────────────────────────
async def test_stream_auth_error_maps_to_502(gateway):
    store = gateway["ctx"].store
    circuit = _CountingCircuit()
    mgr = _mgr(circuit, max_attempts=1)

    async def open_stream(candidate, attempt=1, rf_degraded=False):
        async def bad():
            raise InvalidAuthError("invalid upstream key")
            yield  # pragma: no cover
        return bad()

    items = [
        item async for item in mgr.stream(store, [_Cand("authp")], open_stream)
    ]
    finished = [i for i in items if i.kind == "finished"]
    assert finished and finished[0].outcome.success is False
    assert finished[0].outcome.error.http_status == 502
    assert finished[0].outcome.error.code == "upstream_error"
    assert circuit.failure == 1


async def test_stream_success_records_circuit_success_once(gateway):
    store = gateway["ctx"].store
    circuit = _CountingCircuit()
    mgr = _mgr(circuit)

    async def open_stream(candidate, attempt=1, rf_degraded=False):
        async def good():
            yield StreamEvent(delta="ok", ttft_ms=1.0)
            yield StreamEvent(usage=Usage(prompt_tokens=1, completion_tokens=1))
        return good()

    items = [
        item async for item in mgr.stream(store, [_Cand("okp")], open_stream)
    ]
    assert any(i.kind == "finished" and i.outcome.success for i in items)
    assert circuit.success == 1  # 不再出现 StopAsyncIteration + 汇总双写
