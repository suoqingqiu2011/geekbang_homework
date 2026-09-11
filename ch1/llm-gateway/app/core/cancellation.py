"""
CancellationHandler — 客户端断连与上游连接联动清理（G4 修复，SPEC §4.9）。

核心：共享 CancelSignal。断连检测与上游流任务共同监听，
任一触发 → 全部取消 → 关闭上游流上下文，释放资源。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("llm-gateway.cancellation")


class CancelSignal:
    """共享取消信号。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def fire(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> bool:
        """等待直到被取消；返回是否已取消。"""
        await self._event.wait()
        return True


class CancellationHandler:
    """监听客户端断连并传播取消信号。"""

    def __init__(self, poll_interval: float = 0.1) -> None:
        self._poll_interval = poll_interval
        self._tasks: dict[str, asyncio.Task] = {}
        self._signals: dict[str, CancelSignal] = {}

    async def register_signal(self, trace_id: str, request) -> CancelSignal:
        signal = CancelSignal()
        task = asyncio.create_task(
            self._listen_for_disconnect(request, trace_id, signal),
            name=f"disconnect-watch:{trace_id}",
        )
        self._tasks[trace_id] = task
        self._signals[trace_id] = signal
        return signal

    async def unregister(self, trace_id: str, fire: bool = True) -> None:
        task = self._tasks.pop(trace_id, None)
        if task is not None and not task.done():
            task.cancel()
        signal = self._signals.pop(trace_id, None)
        if fire and signal is not None and not signal.cancelled:
            signal.fire()

    async def _listen_for_disconnect(
        self, request, trace_id: str, signal: CancelSignal
    ) -> None:
        try:
            while True:
                if await request.is_disconnected():
                    signal.fire()
                    logger.info("Client disconnected: trace_id=%s", trace_id)
                    return
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 监听器异常不影响主流程
            logger.exception("disconnect watcher failed: trace_id=%s", trace_id)
