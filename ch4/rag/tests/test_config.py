"""配置层：后端切换、参数校验与密钥缺失的错误码。"""

from __future__ import annotations

import pytest

from rag_core.config import RagError, load_settings


def test_default_backend_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EMBED_BACKEND", "local")
    settings = load_settings("embed")
    assert settings.embed_backend == "local"
    assert settings.embed_dim == 512
    assert settings.embed_key_env == ""


def test_invalid_backend_raises_e03(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EMBED_BACKEND", "hybrid")
    with pytest.raises(RagError) as info:
        load_settings("embed")
    assert info.value.code == "RAG-E03"


def test_overlap_must_be_less_than_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_CHUNK_SIZE", "100")
    monkeypatch.setenv("RAG_CHUNK_OVERLAP", "100")
    with pytest.raises(RagError) as info:
        load_settings("embed")
    assert info.value.code == "RAG-E03"


def test_candidate_k_must_cover_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_TOP_K", "20")
    monkeypatch.setenv("RAG_CANDIDATE_K", "5")
    with pytest.raises(RagError) as info:
        load_settings("embed")
    assert info.value.code == "RAG-E03"


def test_cloud_backend_requires_dashscope_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EMBED_BACKEND", "cloud")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    with pytest.raises(RagError) as info:
        load_settings("embed")
    assert info.value.code == "RAG-E03"


def test_llm_requires_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with pytest.raises(RagError) as info:
        load_settings("llm")
    assert info.value.code == "RAG-E03"
