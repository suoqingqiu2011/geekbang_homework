"""索引层：离线建库、双路落盘、加载、陈旧检测与配置不匹配。"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from llama_index.core.embeddings.mock_embed_model import MockEmbedding

from rag_core.config import RagError, Settings, load_settings
from rag_core.corpus import build_corpus
from rag_core.indexer import build_index, load_index, prune_indexes
from rag_core.retrieval import bm25_params, build_bm25_retriever


def _settings(tmp_path: Path, data_dir: Path | None = None) -> Settings:
    base = load_settings("embed")
    return replace(
        base,
        data_dir=data_dir or base.data_dir,
        storage_dir=tmp_path / "storage",
        output_dir=tmp_path / "output",
        embed_dim=8,
    )


def _isolated_data(tmp_path: Path) -> Path:
    source = load_settings("embed").data_dir
    target = tmp_path / "data"
    shutil.copytree(source, target)
    return target


def test_build_writes_both_routes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    index_id, index_dir = build_index(settings, model, build_corpus(settings))

    assert index_dir.name == index_id
    assert (index_dir / "manifest.json").is_file()
    assert (index_dir / "vector").is_dir()
    assert (index_dir / "bm25" / "retriever.json").is_file()
    assert (settings.storage_dir / "current.json").is_file()

    manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bm25"] == bm25_params()


def test_load_returns_same_nodes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    corpus = build_corpus(settings)
    index_id, _ = build_index(settings, model, corpus)

    loaded = load_index(settings, model)
    assert loaded.index_id == index_id
    assert len(loaded.nodes) == len(corpus.nodes)


def test_missing_pointer_raises_e04(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(RagError) as info:
        load_index(settings, MockEmbedding(embed_dim=8))
    assert info.value.code == "RAG-E04"


def test_stale_corpus_raises_e04(tmp_path: Path) -> None:
    data = _isolated_data(tmp_path)
    settings = _settings(tmp_path, data)
    model = MockEmbedding(embed_dim=8)
    build_index(settings, model, build_corpus(settings))

    target = data / "artifacts" / "login-flow.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n<!-- changed -->\n", encoding="utf-8")

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E04"
    assert "artifacts/login-flow.md" in info.value.reason


def test_allow_stale_skips_fingerprint_check(tmp_path: Path) -> None:
    data = _isolated_data(tmp_path)
    settings = _settings(tmp_path, data)
    model = MockEmbedding(embed_dim=8)
    build_index(settings, model, build_corpus(settings))

    target = data / "artifacts" / "login-flow.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n<!-- changed -->\n", encoding="utf-8")

    loaded = load_index(settings, model, allow_stale=True)
    assert loaded.nodes


def test_embed_mismatch_raises_e02(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    build_index(settings, model, build_corpus(settings))

    other = replace(settings, embed_dim=1024)
    with pytest.raises(RagError) as info:
        load_index(other, model)
    assert info.value.code == "RAG-E02"


def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    corpus = build_corpus(settings)

    first_id, _ = build_index(settings, model, corpus)
    second_id, _ = build_index(settings, model, corpus)
    assert first_id == second_id
    assert len([p for p in settings.storage_dir.iterdir() if p.name.startswith("index-")]) == 1


def test_actual_embed_dim_mismatch_blocks_build(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(RagError) as info:
        build_index(settings, MockEmbedding(embed_dim=16), build_corpus(settings))
    assert info.value.code == "RAG-E02"
    assert not (settings.storage_dir / "current.json").exists()


def test_actual_embed_dim_mismatch_blocks_load(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    build_index(settings, MockEmbedding(embed_dim=8), build_corpus(settings))
    with pytest.raises(RagError) as info:
        load_index(settings, MockEmbedding(embed_dim=16))
    assert info.value.code == "RAG-E02"


def _tamper(index_dir: Path, mutate) -> None:
    path = index_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def test_manifest_records_pipeline_and_chunk_unit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    corpus = build_corpus(settings)
    _, index_dir = build_index(settings, model, corpus)

    manifest = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pipeline"]["version"] >= 1
    assert manifest["chunk"]["unit"] == corpus.chunk_unit


def test_schema_version_mismatch_raises_e04(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    _, index_dir = build_index(settings, model, build_corpus(settings))
    _tamper(index_dir, lambda manifest: manifest.update(schema_version=99))

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E04"


def test_pipeline_version_mismatch_raises_e02(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    _, index_dir = build_index(settings, model, build_corpus(settings))
    _tamper(index_dir, lambda manifest: manifest["pipeline"].update(version=0))

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E02"


def test_bm25_manifest_mismatch_raises_e02(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    _, index_dir = build_index(settings, model, build_corpus(settings))
    _tamper(index_dir, lambda manifest: manifest["bm25"].update(token_pattern=r"\w+"))

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E02"
    assert "BM25" in info.value.reason


def test_manifest_missing_bm25_raises_e02(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    _, index_dir = build_index(settings, model, build_corpus(settings))
    _tamper(index_dir, lambda manifest: manifest.pop("bm25"))

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E02"


def test_dual_route_mismatch_raises_e04(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    corpus = build_corpus(settings)
    build_index(settings, model, corpus)

    fake = build_bm25_retriever(corpus.nodes[:1], settings)
    monkeypatch.setattr("rag_core.indexer.load_bm25_retriever", lambda *args, **kwargs: fake)

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E04"


def test_commit_failure_maps_to_e05(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("rag_core.indexer.os.replace", boom)
    with pytest.raises(RagError) as info:
        build_index(settings, MockEmbedding(embed_dim=8), build_corpus(settings))
    assert info.value.code == "RAG-E05"
    assert not (settings.storage_dir / "current.json").exists()


def test_pointer_outside_storage_raises_e04(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    model = MockEmbedding(embed_dim=8)
    build_index(settings, model, build_corpus(settings))

    pointer = settings.storage_dir / "current.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    data["dir"] = "../evil"
    pointer.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RagError) as info:
        load_index(settings, model)
    assert info.value.code == "RAG-E04"


def test_prune_keeps_latest_versions(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    for index in range(4):
        path = storage / f"index-{index:08d}"
        path.mkdir()
        os.utime(path, (1000 + index, 1000 + index))

    removed = prune_indexes(storage, 2, "index-00000003")

    assert removed == 2
    assert {path.name for path in storage.iterdir()} == {"index-00000002", "index-00000003"}
