"""BM25 契约：分词标记落盘、标记不符 E02、jieba 缺失 E01。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from llama_index.core.schema import TextNode

from rag_core import retrieval
from rag_core.config import RagError, load_settings
from rag_core.corpus import build_corpus
from rag_core.retrieval import META_FILENAME, ChineseBM25Retriever


def _build(tmp_path: Path) -> ChineseBM25Retriever:
    settings = load_settings("embed")
    corpus = build_corpus(settings)
    retriever = ChineseBM25Retriever(nodes=corpus.nodes, similarity_top_k=5)
    retriever.persist(str(tmp_path))
    return retriever


def test_persist_writes_jieba_marker(tmp_path: Path) -> None:
    _build(tmp_path)
    meta = json.loads((tmp_path / META_FILENAME).read_text(encoding="utf-8"))
    assert meta["tokenizer"] == "jieba"


def test_marker_mismatch_raises_e02(tmp_path: Path) -> None:
    _build(tmp_path)
    meta_path = tmp_path / META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tokenizer"] = "whitespace"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(RagError) as info:
        ChineseBM25Retriever.from_persist_dir(str(tmp_path))
    assert info.value.code == "RAG-E02"


def test_missing_marker_raises_e02(tmp_path: Path) -> None:
    _build(tmp_path)
    (tmp_path / META_FILENAME).unlink()
    with pytest.raises(RagError) as info:
        ChineseBM25Retriever.from_persist_dir(str(tmp_path))
    assert info.value.code == "RAG-E02"


def test_preprocessing_mismatch_raises_e02(tmp_path: Path) -> None:
    _build(tmp_path)
    meta_path = tmp_path / META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["stopwords"] = "en"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(RagError) as info:
        ChineseBM25Retriever.from_persist_dir(str(tmp_path))
    assert info.value.code == "RAG-E02"


def test_bm25s_missing_raises_e01_at_use_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "_BM25S_READY", False)
    with pytest.raises(RagError) as info:
        ChineseBM25Retriever(nodes=[])
    assert info.value.code == "RAG-E01"


def test_jieba_missing_raises_e01(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "_JIEBA_READY", False)
    with pytest.raises(RagError) as info:
        ChineseBM25Retriever(nodes=[])
    assert info.value.code == "RAG-E01"


def test_retrieval_hits_validity_chunk(tmp_path: Path) -> None:
    retriever = _build(tmp_path)
    loaded = ChineseBM25Retriever.from_persist_dir(str(tmp_path))
    loaded.similarity_top_k = 5
    results = loaded.retrieve("登录会话的有效期是多少分钟？")
    assert results
    assert any("会话有效期" in item.node.text for item in results)


def test_query_side_uses_same_stopwords_as_corpus() -> None:
    node = TextNode(text="the session token", id_="stopword-probe")
    retriever = ChineseBM25Retriever(nodes=[node], similarity_top_k=1)

    results = retriever.retrieve("the")

    assert results
    assert results[0].score > 0


def test_import_retrieval_is_quiet() -> None:
    """bm25s 在 Windows 上的 platform 提示不得混进屏幕输出（会破坏 Tee 同源）。"""
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import rag_core.retrieval"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "resource module not available" not in result.stdout
