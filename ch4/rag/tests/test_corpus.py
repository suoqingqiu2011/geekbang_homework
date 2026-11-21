"""语料层：分块、来源、行号、章节与稳定节点 ID。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from llama_index.core.schema import MetadataMode

from rag_core.config import load_settings
from rag_core.corpus import EMBED_EXCLUDED_METADATA, build_corpus, corpus_fingerprint, iter_source_files


def _synthetic_corpus(tmp_path: Path, files: dict[str, str]):
    data = tmp_path / "data"
    data.mkdir()
    for name, body in files.items():
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return build_corpus(replace(load_settings("embed"), data_dir=data))


def test_source_files_only_md_and_ts() -> None:
    settings = load_settings("embed")
    files = iter_source_files(settings.data_dir)
    assert files, "data/ 下应当存在资料副本"
    assert all(path.suffix.lower() in {".md", ".ts"} for path in files)
    assert not any("node_modules" in path.parts for path in files)


def test_nodes_carry_source_section_and_lines(tmp_path: Path) -> None:
    corpus = _synthetic_corpus(
        tmp_path,
        {"doc.md": "## 6. 会话有效期\n\n有效期为 **15 分钟**。\n"},
    )

    target = next(node for node in corpus.nodes if "15 分钟" in node.text)
    meta = dict(target.metadata or {})
    assert meta["source"] == "doc.md"
    assert meta["start_line"] > 0
    assert meta["end_line"] >= meta["start_line"]
    assert meta["section"] == "6. 会话有效期"
    assert len(meta["chunk_hash"]) == 12


def test_node_ids_are_unique() -> None:
    settings = load_settings("embed")
    corpus = build_corpus(settings)
    ids = [node.node_id for node in corpus.nodes]
    assert len(ids) == len(set(ids))


def test_every_node_maps_back_to_its_source_lines() -> None:
    """重叠块也必须能按 start_line/end_line 回源核对，不允许退化为全文件首次匹配。"""
    settings = load_settings("embed")
    corpus = build_corpus(settings)
    for node in corpus.nodes:
        meta = dict(node.metadata or {})
        lines = (settings.data_dir / meta["source"]).read_text(encoding="utf-8").splitlines()
        start, end = meta["start_line"], meta["end_line"]
        assert start > 0 and end >= start, node.node_id
        assert node.text in "\n".join(lines[start - 1:end]), node.node_id


def test_fingerprint_is_stable_across_calls() -> None:
    settings = load_settings("embed")
    assert corpus_fingerprint(settings) == corpus_fingerprint(settings)
    assert corpus_fingerprint(settings) == build_corpus(settings).sha256


def test_chunk_unit_is_recorded() -> None:
    corpus = build_corpus(load_settings("embed"))
    assert corpus.chunk_unit in {"token", "char"}


def test_noisy_metadata_is_excluded_from_embedding() -> None:
    corpus = build_corpus(load_settings("embed"))
    node = corpus.nodes[0]
    embed = node.get_content(metadata_mode=MetadataMode.EMBED)

    for key in EMBED_EXCLUDED_METADATA:
        assert key not in embed
    assert "source" in embed


def test_duplicate_chunks_are_dropped(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    body = "# 标题\n\n同一段正文，用于验证去重。\n"
    (data / "a.md").write_text(body, encoding="utf-8")
    (data / "b.md").write_text(body, encoding="utf-8")

    corpus = build_corpus(replace(load_settings("embed"), data_dir=data))

    assert corpus.dropped_duplicates == 1
    assert len(corpus.nodes) == 1


def test_section_labels_are_normalized(tmp_path: Path) -> None:
    corpus = _synthetic_corpus(
        tmp_path,
        {
            "doc.md": "## 6. 会话有效期\n\n有效期 15 分钟。\n",
            "code.ts": "export function login() {\n  return true;\n}\n",
        },
    )
    sections = {str(item.metadata["section"]) for item in corpus.nodes}

    assert "6. 会话有效期" in sections
    assert "function login" in sections
    assert not any(label.startswith("#") for label in sections)
    assert not any(label.startswith("export") for label in sections)


def test_empty_file_is_listed_but_yields_no_nodes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "a.md").write_text("# A\n\n正文\n", encoding="utf-8")
    (data / "empty.md").write_text("   \n", encoding="utf-8")
    settings = replace(load_settings("embed"), data_dir=data)

    corpus = build_corpus(settings)

    assert {item.path for item in corpus.files} == {"a.md", "empty.md"}
    assert len(corpus.nodes) == 1
    assert corpus.sha256 == corpus_fingerprint(settings)
