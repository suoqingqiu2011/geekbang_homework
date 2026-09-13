"""流中途取消的半程观测固化测试。

验证两处优化：
  1) 取消识别 `_is_cancellation`：兼容纯 asyncio.CancelledError 与
     Starlette/anyio 结构化并发下包装的 BaseExceptionGroup；
  2) `_record_cancelled_trace` 会把一条 status=cancelled、partial=True、
     含半程字符数的 trace 落库，而不是既不算失败也完全不留痕。

采用单元级验证 `_handle_stream` 取消固化调用的两个核心辅助函数，
避免依赖真实 TCP 断连的时序（保证确定性、稳定不 flaky）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.api.chat import _is_cancellation, _record_cancelled_trace
from app.core.trace_freeze import TraceFreezeRecord


# ─────────────────────────────────────────────────────────────
# 取消识别 _is_cancellation
# ─────────────────────────────────────────────────────────────
def test_is_cancellation_plain_cancelled():
    assert _is_cancellation(asyncio.CancelledError()) is True


def test_is_cancellation_exception_group_wrapping_cancel():
    # Starlette/anyio 结构化并发把取消包装为 BaseExceptionGroup
    exc = BaseExceptionGroup("cancelled", [asyncio.CancelledError()])
    assert _is_cancellation(exc) is True


def test_is_cancellation_nested_group():
    exc = BaseExceptionGroup("outer", [
        BaseExceptionGroup("inner", [ValueError(), asyncio.CancelledError()])
    ])
    assert _is_cancellation(exc) is True


def test_is_cancellation_other_errors_false():
    assert _is_cancellation(ValueError("boom")) is False
    assert _is_cancellation(RuntimeError("boom")) is False
    assert _is_cancellation(ExceptionGroup("", [ValueError(), RuntimeError()])) is False


# ─────────────────────────────────────────────────────────────
# 半程取消固化 _record_cancelled_trace（真实存储层落库）
# ─────────────────────────────────────────────────────────────
async def test_record_cancelled_trace_persists_flagged(gateway):
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="cancel-t1", user_id="u1")

    await _record_cancelled_trace(
        ctx, trace, provider="ok", model="slowstream",
        stream_chars=5, stream_started=True,
    )

    row = await (
        await ctx.store._require_db().execute(
            "SELECT payload FROM traces WHERE trace_id = 'cancel-t1'"
        )
    ).fetchone()
    assert row is not None
    # 状态固化在 payload 中（record_trace 将 runtime 序列化进 payload）
    runtime = json.loads(row["payload"])
    assert runtime["status"] == "cancelled"
    assert runtime["partial"] is True
    assert runtime["stream_started"] is True
    assert runtime["stream_chars"] == 5
    assert runtime["provider"] == "ok"
    assert runtime["model"] == "slowstream"


async def test_record_cancelled_trace_zero_chars(gateway):
    """首 chunk 前即被取消：stream_chars=0、stream_started=False 也应落库。"""
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="cancel-t2")

    await _record_cancelled_trace(
        ctx, trace, provider="ok", model="m",
        stream_chars=0, stream_started=False,
    )

    row = await (
        await ctx.store._require_db().execute(
            "SELECT payload FROM traces WHERE trace_id = 'cancel-t2'"
        )
    ).fetchone()
    runtime = json.loads(row["payload"])
    assert runtime["status"] == "cancelled"
    assert runtime["partial"] is True
    assert runtime["stream_chars"] == 0
    assert runtime["stream_started"] is False


async def test_cancelled_trace_not_an_error(gateway):
    """取消不进入 error 分支：status=cancelled 且无 error_code。"""
    from app.api.chat import _record_cancelled_trace as _rct
    ctx = gateway["ctx"]
    trace = TraceFreezeRecord(trace_id="cancel-t3")
    await _rct(ctx, trace, "ok", "m", 1, True)
    row = await (
        await ctx.store._require_db().execute(
            "SELECT error_code, payload FROM traces WHERE trace_id = 'cancel-t3'"
        )
    ).fetchone()
    runtime = json.loads(row["payload"])
    # 取消不同于失败：status=cancelled，不写 error_code
    assert runtime["status"] == "cancelled"
    assert row["error_code"] is None