"""Step 5：CLI 与 admin 查询能力对齐测试。"""

from __future__ import annotations

from types import SimpleNamespace

from cli.gateway_cli import _summary, build_parser
from app.storage.metrics_store import MetricsStore


def test_cli_parser_supports_admin_filters():
    parser = build_parser()

    a = parser.parse_args(["summary", "--group-by", "provider_model", "--days", "30"])
    assert a.group_by == "provider_model"
    assert a.days == 30

    a = parser.parse_args([
        "traces", "--provider", "p1", "--model", "m1", "--days", "7",
    ])
    assert (a.provider, a.model, a.days) == ("p1", "m1", 7)

    a = parser.parse_args(["traces", "--status", "partial_failed"])
    assert a.status == "partial_failed"

    a = parser.parse_args(["daily", "--provider", "p1", "--model", "m1"])
    assert (a.provider, a.model) == ("p1", "m1")


async def test_cli_summary_grouped_output(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    store = MetricsStore(str(db_path))
    await store.initialize()
    await store.record_usage(
        billing_id="c1", trace_id="c1", provider="p1", model="m1",
        prompt_tokens=10, completion_tokens=5, latency_ms=100.0, cost_usd=0.01,
    )
    await store.close()

    await _summary(SimpleNamespace(db=str(db_path), group_by="provider_model", days=0))
    out = capsys.readouterr().out
    assert "group_by=provider_model" in out
    assert "p1" in out and "m1" in out
    assert "totals:" in out
    assert "error_rate=" in out


async def test_cli_summary_default_output_unchanged(tmp_path, capsys):
    db_path = tmp_path / "cli-flat.db"
    store = MetricsStore(str(db_path))
    await store.initialize()
    await store.close()

    await _summary(SimpleNamespace(db=str(db_path), group_by=None, days=0))
    out = capsys.readouterr().out
    assert "requests" in out
    assert "providers" in out
    assert "days" in out
    assert "group_by=" not in out
