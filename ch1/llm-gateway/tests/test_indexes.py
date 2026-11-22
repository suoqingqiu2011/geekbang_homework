"""Step 4：admin 查询相关索引存在性回归。"""

from __future__ import annotations

from app.storage.metrics_store import MetricsStore

_EXPECTED = {
    "idx_usage_provider_model",
    "idx_usage_model",
    "idx_usage_created_provider_model",
    "idx_traces_provider_model",
    "idx_error_provider_created",
    "idx_error_provider_model_created",
}


async def test_admin_query_indexes_created(tmp_path):
    store = MetricsStore(str(tmp_path / "indexes.db"))
    await store.initialize()
    try:
        rows = await (
            await store._require_db().execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        ).fetchall()
        names = {row["name"] for row in rows}
        assert _EXPECTED <= names, f"missing indexes: {_EXPECTED - names}"
    finally:
        await store.close()
