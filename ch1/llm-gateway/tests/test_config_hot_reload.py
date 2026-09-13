"""配置热更新原子性单元测试（G17 / M4）。"""

from __future__ import annotations

import yaml

from app.core.config_manager import ConfigurationManager
from app.core.http_client_pool import SharedHttpClientPool


def _write(path, providers=None, api_keys=None):
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "0.0.0.0", "port": 8000, "workers": 4, "log_level": "INFO"},
                "providers": providers or {
                    "p1": {
                        "api_base_url": "http://p1/v1",
                        "cost_per_million_tokens": 1.0,
                        "thinking_capability": 0.5,
                        "capacity": 5,
                    }
                },
                "api_keys": api_keys or ["k1"],
                "available_models": [{"name": "m1", "provider": "p1", "routing_weight": 1.0}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


async def test_reload_success(tmp_path):
    p = tmp_path / "c.yaml"
    _write(p)
    cm = ConfigurationManager(config_path=str(p))
    ok = await cm.reload()
    assert ok
    v1 = cm.version
    assert cm.config.loaded
    assert cm.config.providers["p1"].api_base_url == "http://p1/v1"

    # 热更新：新增 provider
    _write(p, providers={
        "p1": {"api_base_url": "http://p1/v1", "cost_per_million_tokens": 1.0, "thinking_capability": 0.5, "capacity": 5},
        "p2": {"api_base_url": "http://p2/v1", "cost_per_million_tokens": 2.0, "thinking_capability": 0.8, "capacity": 5},
    })
    ok = await cm.reload()
    assert ok
    assert cm.version == v1 + 1
    assert "p2" in cm.config.providers


async def test_reload_failure_keeps_old(tmp_path):
    p = tmp_path / "c.yaml"
    _write(p)
    cm = ConfigurationManager(config_path=str(p))
    await cm.reload()
    v1 = cm.version
    old_providers = dict(cm.config.providers)

    # 写入损坏 YAML
    p.write_text("providers: [unclosed\n  bad", encoding="utf-8")
    ok = await cm.reload()
    assert ok is False
    assert cm.version == v1  # 版本不变
    assert cm.config.providers.keys() == old_providers.keys()  # 旧配置保留


async def test_env_override(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    _write(p)
    monkeypatch.setenv("GATEWAY_PORT", "9999")
    cm = ConfigurationManager(config_path=str(p))
    await cm.reload()
    assert cm.config.port == 9999


async def test_api_keys_env_override(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    _write(p)
    monkeypatch.setenv("GATEWAY_API_KEYS", "env-key-1,env-key-2")
    cm = ConfigurationManager(config_path=str(p))
    await cm.reload()
    assert cm.get_api_keys() == ["env-key-1", "env-key-2"]


# ─────────────────────────────────────────────────────────────
# ★ 缺陷B 回归：连接池参数热更新——旧客户端关闭重建，新参数生效
# ─────────────────────────────────────────────────────────────
async def test_http_pool_update_params_rebuilds_clients():
    pool = SharedHttpClientPool(
        max_connections=10, max_keepalive=3, connect_timeout=1.0, read_timeout=2.0
    )
    await pool.initialize()
    try:
        c1 = await pool.get_client("http://a")
        assert pool._url_clients["http://a"] is c1

        # 热更新：参数刷新 + 旧客户端全部关闭重建（httpx 参数在创建时固化）
        await pool.update_params(50, 10, 3.0, 9.0)
        assert pool.max_connections == 50
        assert pool.max_keepalive == 10
        assert pool.connect_timeout == 3.0
        assert pool.read_timeout == 9.0
        assert pool._url_clients == {}  # 旧客户端已从池中移除

        # 重新获取：按新参数惰性重建（新实例 + 新 timeout 生效）
        c2 = await pool.get_client("http://a")
        assert c2 is not c1
        assert c2.timeout.connect == 3.0
        assert c2.timeout.read == 9.0
        # 未更新的 URL 也重建为同一新实例
        c3 = await pool.get_client("http://a")
        assert c3 is c2
    finally:
        await pool.close()
