"""CircuitBreaker 状态机单元测试（SQLite 持久化）。"""

from __future__ import annotations

import asyncio

from app.core.circuit_breaker import CircuitBreakerManager
from app.storage.metrics_store import MetricsStore


async def _store(tmp_path):
    store = MetricsStore(str(tmp_path / "cb.db"))
    await store.initialize()
    return store


async def test_closed_to_open(tmp_path):
    store = await _store(tmp_path)
    cb = CircuitBreakerManager(failure_threshold=2, recovery_timeout=60.0, half_open_max_calls=1)
    try:
        assert (await cb.can_pass_request(store, "p1")).allowed
        await cb.record_failure(store, "p1")
        assert (await cb.can_pass_request(store, "p1")).allowed
        await cb.record_failure(store, "p1")
        # 达到阈值 → OPEN
        decision = await cb.can_pass_request(store, "p1")
        assert not decision.allowed
        assert decision.reason == "circuit_open"
        st = await store.get_circuit_state("p1")
        assert st["state"] == "open"
    finally:
        await store.close()


async def test_half_open_probe(tmp_path):
    store = await _store(tmp_path)
    cb = CircuitBreakerManager(failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)
    try:
        await cb.record_failure(store, "p1")
        await cb.record_failure(store, "p1")
        assert not (await cb.can_pass_request(store, "p1")).allowed  # open
        await asyncio.sleep(0.1)  # 恢复窗口过
        decision = await cb.can_pass_request(store, "p1")  # half-open 探针
        assert decision.allowed
        assert decision.reason == "half_open_probe"
        # 探针成功 → 闭合
        await cb.record_success(store, "p1")
        st = await store.get_circuit_state("p1")
        assert st["state"] == "closed"
    finally:
        await store.close()


async def test_probe_failure_reopens(tmp_path):
    store = await _store(tmp_path)
    cb = CircuitBreakerManager(failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)
    try:
        await cb.record_failure(store, "p1")
        await cb.record_failure(store, "p1")
        await asyncio.sleep(0.1)
        assert (await cb.can_pass_request(store, "p1")).allowed  # probe
        await cb.record_failure(store, "p1")  # probe 失败
        st = await store.get_circuit_state("p1")
        assert st["state"] == "open"  # 重新 OPEN
    finally:
        await store.close()


async def test_half_open_max_calls(tmp_path):
    store = await _store(tmp_path)
    cb = CircuitBreakerManager(failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)
    try:
        await cb.record_failure(store, "p1")
        await cb.record_failure(store, "p1")
        await asyncio.sleep(0.1)
        assert (await cb.can_pass_request(store, "p1")).allowed  # 第 1 枚探针
        decision = await cb.can_pass_request(store, "p1")        # 超过 half_open_max_calls
        assert not decision.allowed
        assert decision.reason == "circuit_open"
    finally:
        await store.close()


async def test_success_resets_failures(tmp_path):
    store = await _store(tmp_path)
    cb = CircuitBreakerManager(failure_threshold=2, recovery_timeout=60.0, half_open_max_calls=1)
    try:
        await cb.record_failure(store, "p1")
        await cb.record_success(store, "p1")  # 成功重置
        assert (await cb.can_pass_request(store, "p1")).allowed
        st = await store.get_circuit_state("p1")
        assert st["consecutive_failures"] == 0
    finally:
        await store.close()
