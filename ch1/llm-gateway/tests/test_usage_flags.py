"""上游无 usage 时估算/完整性标注测试。

验证两处优化：
  1) 无上游 usage 时显式标注 estimated=True / complete=False，而非静默当真值；
  2) 流式下字符数仅作估算兜底（estimated=True），不被当作真实 token 上报/计费。
"""

from __future__ import annotations

from typing import Any

from app.storage.metrics_store import MetricsStore


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer sk-test"}


def _req(model: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


async def _usage_row(db) -> dict[str, Any] | None:
    row = await (
        await db.execute(
            "SELECT complete, estimated, prompt_tokens, completion_tokens "
            "FROM usage_metrics"
        )
    ).fetchone()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# OpenAI 兼容族 —— 非流式
# ─────────────────────────────────────────────────────────────
async def test_nonstream_with_usage_is_complete_real(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("ok"), headers=_headers()
    )
    assert r.status_code == 200
    u = r.json()["usage"]
    # 上游真实 usage → 标记完整、非估算
    assert u["complete"] is True
    assert u["estimated"] is False
    assert u["completion_tokens"] == 8  # 来自 mock 真实 usage


async def test_nonstream_no_usage_marked_estimated(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("nousage"), headers=_headers()
    )
    assert r.status_code == 200
    u = r.json()["usage"]
    # 无上游 usage → 显式标注估算 + 不完整，且不伪造 token 数
    assert u["complete"] is False
    assert u["estimated"] is True
    assert u["prompt_tokens"] == 0
    assert u["completion_tokens"] == 0


# ─────────────────────────────────────────────────────────────
# OpenAI 兼容族 —— 流式（usage 只落库，不返回给客户端，故查 DB）
# ─────────────────────────────────────────────────────────────
async def test_stream_with_usage_is_real_in_db(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("ok", stream=True), headers=_headers()
    )
    assert r.status_code == 200
    row = await _usage_row(gateway["ctx"].store._require_db())
    assert row is not None
    # 上游 usage chunk → 真实、完整
    assert row["estimated"] == 0
    assert row["complete"] == 1
    assert row["completion_tokens"] == 3  # mock 真实 usage


async def test_stream_no_usage_char_count_is_estimated_in_db(gateway):
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("nousage", stream=True), headers=_headers()
    )
    assert r.status_code == 200
    row = await _usage_row(gateway["ctx"].store._require_db())
    assert row is not None
    # 无上游 usage：字符数(len)仅作估算兜底，标记 estimated + 不完整，
    # 而非当作真实 token。
    assert row["estimated"] == 1
    assert row["complete"] == 0
    assert row["completion_tokens"] == len("Hello world")  # 字符数而非真实 token


# ─────────────────────────────────────────────────────────────
# Anthropic —— 非流式 & 流式
# ─────────────────────────────────────────────────────────────
async def test_anthropic_nonstream_no_usage_marked_estimated(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions", json=_req("claude-nousage"), headers=_headers()
    )
    assert r.status_code == 200
    u = r.json()["usage"]
    assert u["complete"] is False
    assert u["estimated"] is True


async def test_anthropic_stream_no_usage_estimated_in_db(multi_gateway):
    r = await multi_gateway["client"].post(
        "/v1/chat/completions",
        json=_req("claude-nousage", stream=True),
        headers=_headers(),
    )
    assert r.status_code == 200
    row = await _usage_row(multi_gateway["ctx"].store._require_db())
    assert row is not None
    # 无上游 message_delta usage → 字符数估算、不完整
    assert row["estimated"] == 1
    assert row["complete"] == 0
    assert row["completion_tokens"] == len("hello")  # 字符数估算


# ─────────────────────────────────────────────────────────────
# 估算行不计费：estimated=True 的行 cost_usd 必须为 0
# ─────────────────────────────────────────────────────────────
async def _total_cost(gateway) -> float:
    row = await (
        await gateway["ctx"].store._require_db().execute(
            "SELECT SUM(cost_usd) AS cost FROM usage_metrics"
        )
    ).fetchone()
    return float(row["cost"])


async def test_estimated_row_not_billed(gateway):
    # 无上游 usage 的流式（11 字符估算）：不计费
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("nousage", stream=True), headers=_headers()
    )
    assert r.status_code == 200
    row = await (
        await gateway["ctx"].store._require_db().execute(
            "SELECT estimated, complete, completion_tokens FROM usage_metrics"
        )
    ).fetchone()
    # 确认它确实是估算行，且字符数被记录为估算
    assert row["estimated"] == 1
    assert row["complete"] == 0
    assert row["completion_tokens"] == len("Hello world")
    # 估算行不计入真实计费
    assert await _total_cost(gateway) == 0.0


async def test_estimated_nonstream_not_billed(gateway):
    # 非流式无 usage：同样不计费
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("nousage"), headers=_headers()
    )
    assert r.status_code == 200
    assert r.json()["usage"]["estimated"] is True
    assert await _total_cost(gateway) == 0.0


async def test_real_row_incurs_cost(gateway):
    # 基准对照：真实 usage 行正常计费（cost > 0）
    r = await gateway["client"].post(
        "/v1/chat/completions", json=_req("ok", stream=True), headers=_headers()
    )
    assert r.status_code == 200
    assert await _total_cost(gateway) > 0.0


# ─────────────────────────────────────────────────────────────
# 存储层：complete/estimated 标记确实持久化到计费表
# ─────────────────────────────────────────────────────────────
async def test_record_usage_persists_flags(tmp_path):
    store = MetricsStore(str(tmp_path / "flag.db"))
    try:
        await store.initialize()
        ok = await store.record_usage(
            billing_id="b1", trace_id="t1", provider="p", model="m",
            prompt_tokens=1, completion_tokens=2, latency_ms=1,
            complete=True, estimated=False,
        )
        est = await store.record_usage(
            billing_id="b2", trace_id="t2", provider="p", model="m",
            prompt_tokens=0, completion_tokens=5, latency_ms=1,
            complete=False, estimated=True,
        )
        assert ok and est
        rows = await (
            await store._require_db().execute(
                "SELECT billing_id, complete, estimated FROM usage_metrics"
            )
        ).fetchall()
        flags = {r["billing_id"]: (r["complete"], r["estimated"]) for r in rows}
        assert flags["b1"] == (1, 0)  # 真实、完整
        assert flags["b2"] == (0, 1)  # 估算、不完整
    finally:
        await store.close()


# ─────────────────────────────────────────────────────────────
# ★ 缺陷C 回归：预算累计花费 query_spent_usd 只聚合真实花费
# ─────────────────────────────────────────────────────────────
async def test_query_spent_usd_aggregates_real_only(tmp_path):
    store = MetricsStore(str(tmp_path / "spend.db"))
    try:
        await store.initialize()
        # 真实行：正常计费
        await store.record_usage(
            billing_id="b1", trace_id="t1", provider="p", model="m",
            prompt_tokens=1000, completion_tokens=1000, latency_ms=1,
            cost_usd=0.001, complete=True, estimated=False, project_id="proj",
        )
        # 估算行：cost_usd=0，不应计入累计预算
        await store.record_usage(
            billing_id="b2", trace_id="t2", provider="p", model="m",
            prompt_tokens=0, completion_tokens=5, latency_ms=1,
            cost_usd=0.0, complete=False, estimated=True, project_id="proj",
        )
        # 其他项目隔离
        await store.record_usage(
            billing_id="b3", trace_id="t3", provider="p", model="m",
            prompt_tokens=10, completion_tokens=10, latency_ms=1,
            cost_usd=0.002, complete=True, estimated=False, project_id="other",
        )
        assert await store.query_spent_usd("proj") == 0.001
        assert await store.query_spent_usd("other") == 0.002
        assert await store.query_spent_usd("nope") == 0.0
    finally:
        await store.close()