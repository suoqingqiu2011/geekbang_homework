"""断连主动轮询兜底（方案B）的单元测试。

覆盖 CancellationHandler 断连信号接线与 `_anext_or_disconnect` 竞争消费逻辑：
- loop 1) register_signal 检测到已断连 → signal.cancelled == True
- loop 2) unregister 清理监听任务与状态（防 task 泄漏）
- loop 3) _anext_or_disconnect：正常事件 / 已触发信号 / 流结束 三种返回
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from app.api.chat import _anext_or_disconnect
from app.core.cancellation import CancelSignal


class _FakeRequest:
    """is_disconnected 即时返回给定值的伪请求，规避 ASGI 传输层的不可测性。"""

    def __init__(self, disconnected: bool) -> None:
        self._d = disconnected

    async def is_disconnected(self) -> bool:
        return self._d


# ─────────────────────────────────────────────────────────────
# register_signal：断连检测 → 信号触发
# ─────────────────────────────────────────────────────────────
async def test_register_signal_detects_already_disconnected(gateway):
    ctx = gateway["ctx"]
    req = _FakeRequest(True)
    signal = await ctx.cancellation.register_signal("poll-1", req)
    assert signal is not None
    # 监听任务立即发现断连并触发信号（is_disconnected 返回 True，无需等下一轮询）
    await asyncio.sleep(0.05)
    assert signal.cancelled is True
    await ctx.cancellation.unregister("poll-1", fire=False)


async def test_unregister_cleans_up_task_and_state(gateway):
    ctx = gateway["ctx"]
    req = _FakeRequest(False)  # 未断连 → 监听任务持续轮询，等待 unregister 终止
    signal = await ctx.cancellation.register_signal("poll-2", req)
    task = ctx.cancellation._tasks["poll-2"]
    assert not task.done()
    assert ctx.cancellation._signals["poll-2"] is signal

    await ctx.cancellation.unregister("poll-2", fire=False)

    assert "poll-2" not in ctx.cancellation._tasks
    assert "poll-2" not in ctx.cancellation._signals
    # 等待任务真正取消，避免 "Task was destroyed but it is pending" 告警
    await asyncio.sleep(0.05)
    assert task.done() or task.cancelled()


# ─────────────────────────────────────────────────────────────
# _anext_or_disconnect：竞争消费
# ─────────────────────────────────────────────────────────────
async def test_anext_returns_event_when_no_disconnect(gateway):
    signal = CancelSignal()

    async def gen():
        yield "e1"

    ev, disconnected = await _anext_or_disconnect(gen(), signal)
    assert disconnected is False
    assert ev == "e1"


async def test_anext_disconnects_when_signal_fired(gateway):
    signal = CancelSignal()
    signal.fire()

    async def gen():
        yield "e1"  # 即使上游有事件，也应被断连信号抢先

    ev, disconnected = await _anext_or_disconnect(gen(), signal)
    assert disconnected is True
    assert ev is None


async def test_anext_stop_when_stream_exhausted(gateway):
    signal = CancelSignal()

    async def gen():
        if False:  # pragma: no cover - 空生成器保证立即 StopAsyncIteration
            yield "x"

    with pytest.raises(StopAsyncIteration):
        await _anext_or_disconnect(gen(), signal)


async def test_anext_sequence_until_exhausted(gateway):
    """持续消费直到流结束：前面拿事件，最后抛 StopAsyncIteration。"""
    signal = CancelSignal()

    async def gen():
        yield "a"
        yield "b"

    ag = gen()
    ev, disc = await _anext_or_disconnect(ag, signal)
    assert ev == "a" and disc is False
    ev, disc = await _anext_or_disconnect(ag, signal)
    assert ev == "b" and disc is False
    with pytest.raises(StopAsyncIteration):
        await _anext_or_disconnect(ag, signal)


async def test_anext_blocked_then_disconnect(gateway):
    """上游阻塞（永不产生事件）时，信号一旦触发即可打断——兜底核心收益。"""
    signal = CancelSignal()

    async def gen():
        await asyncio.Event().wait()  # 永不完成 → 模拟上游停顿
        yield "never"

    coro = _anext_or_disconnect(gen(), signal)

    async def fire_later():
        await asyncio.sleep(0.01)
        signal.fire()

    f = asyncio.ensure_future(coro)
    await fire_later()
    ev, disconnected = await f
    assert disconnected is True
    assert ev is None


async def test_anext_blocked_then_cancelled_from_outside(gateway):
    """即便外部直接取消竞争协程，也不应把 CancelledError 泄漏成"未取回的异常"。"""
    signal = CancelSignal()

    async def gen():
        await asyncio.Event().wait()
        yield "never"

    coro = asyncio.ensure_future(_anext_or_disconnect(gen(), signal))
    await asyncio.sleep(0.01)
    coro.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coro
    # 不 assert：主要验证不产生崩溃/告警即可；再等待任务清空
    await asyncio.sleep(0.05)