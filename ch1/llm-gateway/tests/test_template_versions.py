"""
测试：提示词模板按 template_id/version 联合定位、保留历史、明确版本操作。

覆盖三层：
- 存储层（store.upsert/get/list/delete_template）：历史保留、精确版本定位
- 渲染层（prompt_renderer.render）：最新/指定版本渲染、缓存按版本隔离
- API 层（/v1/chat/completions）：template_version 透传与参数校验
"""

from __future__ import annotations

from typing import Any


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer sk-test"}


def _req(model: str | None = "ok", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────
# 存储层：历史保留 + 精确版本定位
# ─────────────────────────────────────────────────────────────
async def test_upsert_keeps_history_and_increments_version(gateway):
    store = gateway["ctx"].store
    v1 = await store.upsert_template("tpl", "desc", "version-one")
    v2 = await store.upsert_template("tpl", "desc", "version-two")

    assert v1 == 1
    assert v2 == 2

    # 历史版本均保留
    history = await store.list_template_versions("tpl")
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["content"] == "version-one"
    assert history[1]["content"] == "version-two"

    # 不指定版本 → 最新
    latest = await store.get_template("tpl")
    assert latest["version"] == 2
    assert latest["content"] == "version-two"

    # 指定版本 → 精确返回历史
    hist = await store.get_template("tpl", version=1)
    assert hist is not None
    assert hist["version"] == 1
    assert hist["content"] == "version-one"


async def test_get_template_specific_version(gateway):
    store = gateway["ctx"].store
    for i in range(1, 4):
        await store.upsert_template("tpl", "desc", f"content-{i}")

    rec = await store.get_template("tpl", version=2)
    assert rec is not None
    assert rec["content"] == "content-2"


async def test_get_template_missing_version_returns_none(gateway):
    store = gateway["ctx"].store
    await store.upsert_template("tpl", "desc", "x")

    assert await store.get_template("tpl", version=99) is None
    assert await store.get_template("tpl", version=0) is None
    assert await store.get_template("no-such-template") is None


async def test_delete_template_by_version_and_all(gateway):
    store = gateway["ctx"].store
    await store.upsert_template("tpl", "desc", "c1")
    await store.upsert_template("tpl", "desc", "c2")
    await store.upsert_template("tpl", "desc", "c3")

    await store.delete_template("tpl", version=2)
    assert await store.get_template("tpl", version=2) is None
    # 其余版本保留
    survivors = {r["version"] for r in await store.list_template_versions("tpl")}
    assert survivors == {1, 3}

    await store.delete_template("tpl")
    assert await store.list_template_versions("tpl") == []


# ─────────────────────────────────────────────────────────────
# 渲染层：最新/指定版本渲染 + 缓存按版本隔离
# ─────────────────────────────────────────────────────────────
async def test_render_latest_by_default_and_specific_version(gateway):
    renderer = gateway["ctx"].prompt_renderer
    await renderer.upsert("tpl", "desc", "Hola {{who}}")
    await renderer.upsert("tpl", "desc", "Hello {{who}}!")

    # 默认渲染最新版本
    assert await renderer.render("tpl", {"who": "Ada"}) == "Hello Ada!"
    # 精确渲染历史版本
    assert await renderer.render("tpl", {"who": "Ada"}, version=1) == "Hola Ada"


async def test_render_cache_isolated_by_version(gateway):
    renderer = gateway["ctx"].prompt_renderer
    await renderer.upsert("tpl", "desc", "v1:{{v}}")
    # 先缓存 v1
    assert await renderer.render("tpl", {"v": "x"}, version=1) == "v1:x"
    # 新增 v2：invalidate 后默认应取到最新 v2（不被 v1 缓存污染）
    await renderer.upsert("tpl", "desc", "v2:{{v}}")
    assert await renderer.render("tpl", {"v": "y"}) == "v2:y"
    # 历史 v1 缓存仍独立有效
    assert await renderer.render("tpl", {"v": "z"}, version=1) == "v1:z"


async def test_render_specific_version_requires_existing(gateway):
    renderer = gateway["ctx"].prompt_renderer
    await renderer.upsert("tpl", "desc", "x")

    from app.errors import InvalidRequestError

    try:
        await renderer.render("tpl", {}, version=42)
    except InvalidRequestError:
        return
    raise AssertionError("expected InvalidRequestError for missing version")


# ─────────────────────────────────────────────────────────────
# API 层：template_version 透传与校验
# ─────────────────────────────────────────────────────────────
async def test_chat_uses_specified_template_version(gateway):
    store = gateway["ctx"].store
    client = gateway["client"]
    # v1 需要变量 question；v2 无需变量
    await store.upsert_template("tpl", "desc", "R1: {{question}}")
    await store.upsert_template("tpl", "desc", "R2")

    # 指定 v1 但缺 question → 必然取到 v1 → 缺变量报错
    body = _req(template_id="tpl", template_version=1, template_vars={"scratch": "x"})
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "prompt_missing_vars"

    # 指定 v2 → 无必需变量 → 成功（证明确实选中了 v2）
    body = _req(template_id="tpl", template_version=2, template_vars={"scratch": "x"})
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 200

    # 不指定版本 → 默认最新（v2）→ 成功
    body = _req(template_id="tpl", template_vars={"scratch": "x"})
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 200

    # 指定不存在的版本 → invalid_request
    body = _req(template_id="tpl", template_version=99, template_vars={"scratch": "x"})
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_template_version_requires_template_id(gateway):
    client = gateway["client"]
    body = _req(template_version=1)
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_template_version_must_be_positive(gateway):
    client = gateway["client"]
    body = _req(template_id="tpl", template_version=0, template_vars={"a": "b"})
    r = await client.post("/v1/chat/completions", json=body, headers=_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"