"""索引构建、版本化原子提交、清单校验与双路加载。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import SimpleVectorStore
from llama_index.core.vector_stores.utils import metadata_dict_to_node

from rag_core.config import RagError, Settings
from rag_core.corpus import Corpus, corpus_file_hashes, corpus_fingerprint
from rag_core.retrieval import ChineseBM25Retriever, bm25_params, load_bm25_retriever

INDEX_SCHEMA_VERSION = 1
# 分块/融合/元数据口径变化时递增：同一份语料在新旧流水线下产出的节点不同，
# 不能靠 index_id 复用旧目录。
INDEX_PIPELINE_VERSION = 3


@dataclass
class LoadedIndex:
    index: VectorStoreIndex
    nodes: list[TextNode]
    bm25: ChineseBM25Retriever
    manifest: dict
    index_dir: Path

    @property
    def index_id(self) -> str:
        return self.index_dir.name


def bm25_node_ids(loaded: LoadedIndex) -> set[str]:
    return _bm25_ids(loaded.bm25)


def _bm25_ids(bm25: ChineseBM25Retriever) -> set[str]:
    return {metadata_dict_to_node(item).node_id for item in bm25.corpus}


def _core_manifest(settings: Settings, corpus: Corpus) -> dict:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "embed": {"backend": settings.embed_backend, "model": settings.embed_model, "dim": settings.embed_dim},
        "chunk": {
            "size": settings.chunk_size,
            "overlap": settings.chunk_overlap,
            "splitter": "SentenceSplitter",
            "unit": corpus.chunk_unit,
        },
        "bm25": bm25_params(),
        "pipeline": {"version": INDEX_PIPELINE_VERSION},
        "corpus": {"sha256": corpus.sha256},
    }


def _index_id(core: dict) -> str:
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _verify_persisted(index_dir: Path, expected: int, embed_model: Any) -> None:
    vector_store = SimpleVectorStore.from_persist_dir(str(index_dir / "vector"))
    storage_context = StorageContext.from_defaults(vector_store=vector_store, persist_dir=str(index_dir / "vector"))
    index = load_index_from_storage(storage_context, embed_model=embed_model)
    actual = len(index.docstore.docs)
    if actual != expected:
        raise RagError(
            "RAG-E04", f"向量索引落盘自检失败：期望 {expected} 节点，实际 {actual}",
            "indexer._verify_persisted", "删除 storage/ 后重新运行 step1",
        )
    bm25 = ChineseBM25Retriever.from_persist_dir(str(index_dir / "bm25"))
    if len(bm25.corpus) != expected:
        raise RagError(
            "RAG-E04", f"BM25 落盘自检失败：期望 {expected} 节点，实际 {len(bm25.corpus)}",
            "indexer._verify_persisted", "删除 storage/ 后重新运行 step1",
        )


def _write_pointer(settings: Settings, dir_name: str, built_at: str) -> None:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    tmp_pointer = settings.storage_dir / ".current.json.tmp"
    with tmp_pointer.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"dir": dir_name, "built_at": built_at}, ensure_ascii=False, indent=2))
    os.replace(tmp_pointer, settings.storage_dir / "current.json")


def _measure_embed_dim(embed_model: Any, where: str) -> int:
    """实测模型输出维度：配置里的维度只是期望值，不能证明模型真的这么输出。"""
    try:
        return len(embed_model.get_text_embedding("dimension probe"))
    except Exception as exc:
        raise RagError(
            "RAG-E05", f"Embedding 维度探测失败（{type(exc).__name__}）: {exc}",
            where, "检查 Embedding 依赖/网络/模型缓存",
        ) from exc


def build_index(settings: Settings, embed_model: Any, corpus: Corpus) -> tuple[str, Path]:
    actual_dim = _measure_embed_dim(embed_model, "indexer.build_index")
    if actual_dim != settings.embed_dim:
        raise RagError(
            "RAG-E02",
            f"Embedding 实际维度 {actual_dim} 与配置维度 {settings.embed_dim} 不一致",
            "indexer.build_index", "修正 RAG_EMBED_DIM / LOCAL_EMBED_DIM，或换回对应模型后重建",
        )

    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    core = _core_manifest(settings, corpus)
    index_id = _index_id(core)
    final_dir = settings.storage_dir / f"index-{index_id}"
    tmp_dir = settings.storage_dir / f".tmp-{index_id}-{int(time.time() * 1000)}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")

    try:
        vector_store = SimpleVectorStore()
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        index = VectorStoreIndex(
            nodes=corpus.nodes,
            storage_context=storage_context,
            embed_model=embed_model,
            show_progress=False,
        )
        index.storage_context.persist(persist_dir=str(tmp_dir / "vector"))

        bm25_dir = tmp_dir / "bm25"
        bm25_dir.mkdir(parents=True, exist_ok=True)
        ChineseBM25Retriever(nodes=corpus.nodes, similarity_top_k=settings.candidate_k).persist(str(bm25_dir))

        manifest = dict(core)
        manifest["index_id"] = f"index-{index_id}"
        manifest["built_at"] = built_at
        manifest["nodes"] = len(corpus.nodes)
        manifest["files"] = [
            {"path": item.path, "sha256": item.sha256, "bytes": item.bytes, "lines": item.lines}
            for item in corpus.files
        ]
        with (tmp_dir / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2))

        _verify_persisted(tmp_dir, len(corpus.nodes), embed_model)
    except RagError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RagError(
            "RAG-E05", f"建库失败（{type(exc).__name__}）: {exc}",
            "indexer.build_index", "检查 Embedding 依赖/网络/模型缓存；旧索引与指针均未被修改",
        ) from exc

    try:
        if final_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                reused = json.loads((final_dir / "manifest.json").read_text(encoding="utf-8"))
                built_at = str(reused.get("built_at", built_at))
            except Exception as exc:
                # 复用目录前至少要让清单不可解析这件事可见，不能静默吞掉。
                print(
                    f"  [warn] 现有索引清单不可解析（{type(exc).__name__}），沿用本次构建时间: {final_dir.name}",
                    file=sys.stderr,
                )
        else:
            os.replace(tmp_dir, final_dir)
        _write_pointer(settings, final_dir.name, built_at)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RagError(
            "RAG-E05", f"索引提交失败（{type(exc).__name__}）: {exc}",
            "indexer.build_index", "检查 storage/ 的权限与磁盘空间；失败时指针保持不变",
        ) from exc

    prune_indexes(settings.storage_dir, settings.index_keep, final_dir.name)
    return final_dir.name, final_dir


def prune_indexes(storage_dir: Path, keep: int, current: str) -> int:
    """只保留最近 keep 个索引版本；keep<=0 表示不清理。"""
    if keep <= 0 or not storage_dir.is_dir():
        return 0
    versions = [path for path in storage_dir.iterdir() if path.is_dir() and path.name.startswith("index-")]
    versions.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    removed = 0
    for path in versions[keep:]:
        if path.name == current:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def read_pointer(settings: Settings) -> dict:
    pointer = settings.storage_dir / "current.json"
    if not pointer.is_file():
        raise RagError(
            "RAG-E04", "未找到索引指针 storage/current.json",
            "indexer.read_pointer", "先运行 scripts/step1_build_index.py 建库",
        )
    try:
        return json.loads(pointer.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RagError(
            "RAG-E04", f"索引指针无法解析（{type(exc).__name__}）",
            "indexer.read_pointer", "删除 storage/current.json 后重新建库",
        ) from exc


def _check_compat(settings: Settings, manifest: dict) -> None:
    embed = manifest.get("embed") or {}
    if (
        embed.get("backend") != settings.embed_backend
        or embed.get("model") != settings.embed_model
        or int(embed.get("dim", -1)) != settings.embed_dim
    ):
        raise RagError(
            "RAG-E02",
            "索引 Embedding 配置与当前不一致："
            f"索引={embed.get('backend')}/{embed.get('model')}/{embed.get('dim')}d，"
            f"当前={settings.embed_backend}/{settings.embed_model}/{settings.embed_dim}d",
            "indexer._check_compat", "用当前配置重新运行 step1 建库",
        )
    chunk = manifest.get("chunk") or {}
    if int(chunk.get("size", -1)) != settings.chunk_size or int(chunk.get("overlap", -1)) != settings.chunk_overlap:
        raise RagError("RAG-E02", "索引分块参数与当前不一致", "indexer._check_compat", "用当前参数重新运行 step1 建库")
    # BM25 预处理参数也必须与 manifest 一致；缺项按不一致处理，无法确认口径就不加载。
    recorded_bm25 = manifest.get("bm25") or {}
    current_bm25 = bm25_params()
    if recorded_bm25 != current_bm25:
        raise RagError(
            "RAG-E02",
            f"索引 BM25 参数与当前实现不一致：索引={recorded_bm25!r}，当前={current_bm25!r}",
            "indexer._check_compat", "删除 storage/ 后用当前参数重新运行 step1 建库",
        )
    pipeline = manifest.get("pipeline") or {}
    if pipeline.get("version") != INDEX_PIPELINE_VERSION:
        raise RagError(
            "RAG-E02",
            f"索引流水线版本不兼容（索引={pipeline.get('version')}，当前={INDEX_PIPELINE_VERSION}）",
            "indexer._check_compat", "删除 storage/ 后用当前代码重新建库",
        )


def _corpus_diff(manifest: dict, settings: Settings) -> str:
    recorded = {item.get("path"): item.get("sha256") for item in manifest.get("files") or []}
    current = corpus_file_hashes(settings)
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(rel for rel in set(current) & set(recorded) if current[rel] != recorded[rel])

    def brief(items: list[str]) -> str:
        shown = ", ".join(str(item) for item in items[:8])
        return shown + (f" 等 {len(items)} 个" if len(items) > 8 else "")

    parts = []
    if added:
        parts.append(f"新增: {brief(added)}")
    if changed:
        parts.append(f"修改: {brief(changed)}")
    if removed:
        parts.append(f"删除: {brief(removed)}")
    return "；".join(parts) or "文件清单与指纹不一致"


def _resolve_index_dir(settings: Settings, pointer: dict) -> Path:
    """指针只能指向 storage/ 下的一级 index-* 目录，杜绝路径穿越。"""
    name = str(pointer.get("dir", ""))
    root = settings.storage_dir.resolve()
    index_dir = (settings.storage_dir / name).resolve()
    if index_dir.parent != root or not index_dir.name.startswith("index-"):
        raise RagError(
            "RAG-E04", f"索引指针目录非法: {name or '<空>'}",
            "indexer.load_index", "删除 storage/current.json 后重新运行 step1 建库",
        )
    return index_dir


def load_index(settings: Settings, embed_model: Any, allow_stale: bool = False) -> LoadedIndex:
    pointer = read_pointer(settings)
    index_dir = _resolve_index_dir(settings, pointer)
    manifest_path = index_dir / "manifest.json"
    if not index_dir.is_dir() or not manifest_path.is_file():
        raise RagError(
            "RAG-E04", f"索引目录或清单缺失: {index_dir.name or '<空>'}",
            "indexer.load_index", "重新运行 step1 建库",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RagError(
            "RAG-E04", f"索引清单无法解析（{type(exc).__name__}）",
            "indexer.load_index", "重新运行 step1 建库",
        ) from exc

    if manifest.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise RagError(
            "RAG-E04",
            f"索引清单结构版本不支持（索引={manifest.get('schema_version')}，期望={INDEX_SCHEMA_VERSION}）",
            "indexer.load_index", "删除 storage/ 后用当前代码重建",
        )

    _check_compat(settings, manifest)

    if not allow_stale:
        current = corpus_fingerprint(settings)
        if manifest.get("corpus", {}).get("sha256") != current:
            raise RagError(
                "RAG-E04", "资料已变更，索引与当前语料不一致 -> " + _corpus_diff(manifest, settings),
                "indexer.load_index", "重新运行 step1 建库，或追加 --allow-stale 强制使用旧索引",
            )

    actual_dim = _measure_embed_dim(embed_model, "indexer.load_index")
    if actual_dim != settings.embed_dim:
        raise RagError(
            "RAG-E02",
            f"当前 Embedding 实际维度 {actual_dim} 与索引维度 {settings.embed_dim} 不一致",
            "indexer.load_index", "换回建库时的模型，或重新运行 step1 建库",
        )

    vector_store = SimpleVectorStore.from_persist_dir(str(index_dir / "vector"))
    storage_context = StorageContext.from_defaults(vector_store=vector_store, persist_dir=str(index_dir / "vector"))
    index = load_index_from_storage(storage_context, embed_model=embed_model)
    nodes = [node for node in index.docstore.docs.values() if isinstance(node, TextNode)]
    bm25 = load_bm25_retriever(index_dir, settings)

    # 查询期也要保证两路覆盖同一批节点，否则会出现「检索报告里有、融合里没有」的静默缺口。
    vector_ids = {node.node_id for node in nodes}
    bm25_ids = _bm25_ids(bm25)
    if vector_ids != bm25_ids:
        raise RagError(
            "RAG-E04",
            f"双路索引节点集合不一致：vector={len(vector_ids)}，bm25={len(bm25_ids)}，"
            f"交集={len(vector_ids & bm25_ids)}",
            "indexer.load_index", "删除 storage/ 后重新运行 step1 重建双路索引",
        )

    return LoadedIndex(index=index, nodes=nodes, bm25=bm25, manifest=manifest, index_dir=index_dir)
