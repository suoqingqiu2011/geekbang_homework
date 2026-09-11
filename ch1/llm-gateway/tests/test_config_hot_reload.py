"""配置热更新原子性单元测试（G17 / M4）。"""

from __future__ import annotations

import yaml

from app.core.config_manager import ConfigurationManager


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
