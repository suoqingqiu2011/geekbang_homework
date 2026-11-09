"""计费幂等（恰好一次）单元测试。"""

from __future__ import annotations

import time

import pytest

from app.storage.metrics_store import MetricsStore


async def test_record_usage_idempotent(tmp_path):
    store = MetricsStore(str(tmp_path / "billing.db"))
    await store.initialize()
    try:
        kw = dict(
            billing_id="b1", trace_id="t1", provider="ok", model="ok",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
            ttft_ms=50.0, cost_usd=0.001,
        )
        first = await store.record_usage(**kw)
        second = await store.record_usage(**kw)  # 同 billing_id 重复
        assert first is True
        assert second is False

        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM usage_metrics WHERE billing_id='b1'"
        )
        row = await cursor.fetchone()
        assert row["c"] == 1  # 恰好一次
    finally:
        await store.close()


async def test_daily_aggregate_bumped(tmp_path):
    store = MetricsStore(str(tmp_path / "agg.db"))
    await store.initialize()
    try:
        await store.record_usage(
            billing_id="a", trace_id="t", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
        )
        await store.record_usage(
            billing_id="b", trace_id="t2", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=200.0, cost_usd=0.02,
        )
        rows = await store.query_daily_aggregates(days=1)
        assert rows and rows[0]["requests"] == 2
        assert abs(rows[0]["cost_usd"] - 0.03) < 1e-9
        # 分列累计：两次 10/5 → prompt 20 / completion 10 / total 30
        assert rows[0]["prompt_tokens"] == 20
        assert rows[0]["completion_tokens"] == 10
        assert rows[0]["total_tokens"] == 30
    finally:
        await store.close()


async def test_query_summary(tmp_path):
    store = MetricsStore(str(tmp_path / "sum.db"))
    await store.initialize()
    try:
        await store.record_usage(
            billing_id="c", trace_id="t", provider="p", model="m",
            prompt_tokens=10, completion_tokens=5, latency_ms=100.0,
            cost_usd=0.01, ttft_ms=50.0,
        )
        await store.record_error("t", "p", "upstream_error", "boom")
        s = await store.query_summary()
        assert s["requests"] == 1
        assert s["errors"] == 1
        assert s["prompt_tokens"] == 10
        assert s["completion_tokens"] == 5
        assert s["total_tokens"] == 15
        assert s["avg_ttft_ms"] == 50.0
    finally:
        await store.close()


async def test_record_trace_status_from_runtime(tmp_path):
    """状态列须与 payload 中的 runtime 一致（snapshot 把指标固化在 runtime 下）。"""
    store = MetricsStore(str(tmp_path / "trace.db"))
    await store.initialize()
    try:
        await store.record_trace({
            "trace_id": "trace-err-1",
            "user_id": "u1",
            "runtime": {
                "model": "m1",
                "provider": "p1",
                "status": "error",
                "error_code": "upstream_error",
                "latency_ms": 88.0,
            },
        })
        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT model, provider, status, error_code, latency_ms, payload "
            "FROM traces WHERE trace_id = 'trace-err-1'"
        )
        row = await cursor.fetchone()
        assert row["model"] == "m1"
        assert row["provider"] == "p1"
        assert row["status"] == "error"          # 不再是默认 success
        assert row["error_code"] == "upstream_error"
        assert abs(row["latency_ms"] - 88.0) < 1e-9
        payload = __import__("json").loads(row["payload"])
        assert payload["status"] == "error"      # 状态列与 payload 一致

        # 无 runtime 指标时仍可用顶层兜底值
        await store.record_trace({"trace_id": "trace-ok-1", "status": "success"})
        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT status FROM traces WHERE trace_id = 'trace-ok-1'"
        )
        row = await cursor.fetchone()
        assert row["status"] == "success"
    finally:
        await store.close()


async def test_completion_id_no_trailing_dash():
    """响应 id 应为 chatcmpl-<24 位 hex>，不得残留尾部裸连字符。"""
    from app.api.chat import _completion_id

    cid = _completion_id("72648c9c-c659-4d56-accb-1f2e3d4c5b6a")
    assert cid == "chatcmpl-72648c9cc6594d56accb1f2e"
    assert not cid.endswith("-")
    assert cid.count("-") == 1  # 仅前缀分隔符


# ─────────────────────────────────────────────────────────────
# M2 回归：明细与日报必须同事务提交（不得只落半条账）
# ─────────────────────────────────────────────────────────────
async def test_usage_and_daily_aggregate_atomic(tmp_path):
    """日报累加失败时整体回滚：usage_metrics 不得残留明细行。"""
    store = MetricsStore(str(tmp_path / "atomic.db"))
    await store.initialize()
    try:
        async def _boom(*args, **kwargs):
            raise RuntimeError("bump failed")

        store._bump_daily_aggregate = _boom  # noqa: SLF001
        with pytest.raises(RuntimeError):
            await store.record_usage(
                billing_id="atomic", trace_id="t", provider="p", model="m",
                prompt_tokens=1, completion_tokens=1, latency_ms=1.0, cost_usd=0.1,
            )

        cursor = await store._require_db().execute(  # noqa: SLF001
            "SELECT COUNT(*) AS c FROM usage_metrics WHERE billing_id='atomic'"
        )
        assert (await cursor.fetchone())["c"] == 0  # 明细与日报同回滚
    finally:
        await store.close()


# ─────────────────────────────────────────────────────────────
# M3 回归：预算查询必须命中 (project_id, cost_usd) 覆盖索引
# ─────────────────────────────────────────────────────────────
async def test_spent_query_uses_covering_index(tmp_path):
    """query_spent_usd 对应的查询计划须为覆盖索引搜索，而非全表扫描。"""
    store = MetricsStore(str(tmp_path / "idx.db"))
    await store.initialize()
    try:
        cursor = await store._require_db().execute(  # noqa: SLF001
            "EXPLAIN QUERY PLAN SELECT COALESCE(SUM(cost_usd), 0.0) "
            "FROM usage_metrics WHERE project_id = ?",
            ("p",),
        )
        plan = " ".join(str(row["detail"]) for row in await cursor.fetchall())
        assert "idx_usage_project_cost" in plan  # 索引已建且被选中
        assert "SCAN usage_metrics" not in plan  # 未退化为全表扫描
    finally:
        await store.close()


async def test_unbilled_attempt_cost_is_observation_only(tmp_path):
    """M8 观察模式：失败花费单独落表、幂等，且不改变 query_spent_usd。"""
    store = MetricsStore(str(tmp_path / "unbilled.db"))
    await store.initialize()
    try:
        fields = {
            "billing_id": "b-1",
            "trace_id": "t-1",
            "project_id": "p1",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "reason": "upstream_timeout",
            "prompt_tokens": 1000,
            "cost_usd": 0.002,
        }
        assert await store.record_unbilled_attempt_cost(**fields) is True
        # 同一 (billing_id, provider, model) 重复写入被忽略（客户端重试不重复累加）
        assert await store.record_unbilled_attempt_cost(**fields) is False

        summary = await store.summarize_unbilled_attempt_costs(days=1)
        assert summary["count"] == 1
        assert summary["cost_usd"] == pytest.approx(0.002)
        assert summary["by_reason"][0]["reason"] == "upstream_timeout"

        # ★ 阶段一约束：观察数据不进入预算
        assert await store.query_spent_usd("p1") == 0.0
    finally:
        await store.close()


async def test_purge_unbilled_attempt_costs_only_removes_expired(tmp_path):
    """观察表清理：只删超过保留期的行，且不影响 usage_metrics。"""
    store = MetricsStore(str(tmp_path / "purge.db"))
    await store.initialize()
    try:
        await store.record_unbilled_attempt_cost(
            billing_id="new", trace_id="t-new", project_id="p1", provider="deepseek",
            model="deepseek-chat", reason="upstream_timeout", prompt_tokens=10, cost_usd=0.01,
        )
        db = store._require_db()  # noqa: SLF001
        await db.execute(
            "INSERT INTO unbilled_attempt_costs "
            "(attempt_key, billing_id, trace_id, project_id, provider, model, reason, "
            " prompt_tokens, cost_usd, estimated, created_at) "
            "VALUES ('old','old','t-old','p1','deepseek','deepseek-chat','upstream_timeout',"
            "        10, 0.01, 1, ?)",
            (time.time() - 60 * 86400,),  # 60 天前
        )
        await db.commit()
        # 一条已记账的正式明细，清理不得触及
        await store.record_usage(
            billing_id="billed", trace_id="t-billed", provider="deepseek",
            model="deepseek-chat", prompt_tokens=10, completion_tokens=10,
            latency_ms=1.0, cost_usd=0.02, project_id="p1",
        )

        removed = await store.purge_unbilled_attempt_costs(30)
        assert removed == 1  # 仅过期行被删
        cursor = await db.execute("SELECT COUNT(*) AS c FROM unbilled_attempt_costs")
        assert (await cursor.fetchone())["c"] == 1  # 新行保留
        assert await store.query_spent_usd("p1") == pytest.approx(0.02)  # 账目未受影响
    finally:
        await store.close()


async def test_purge_disabled_when_retention_non_positive(tmp_path):
    """retention_days <= 0 时不做清理（监护人策略，避免误删）。"""
    store = MetricsStore(str(tmp_path / "purge-off.db"))
    await store.initialize()
    try:
        assert await store.purge_unbilled_attempt_costs(0) == 0
        assert await store.purge_unbilled_attempt_costs(-1) == 0
    finally:
        await store.close()
