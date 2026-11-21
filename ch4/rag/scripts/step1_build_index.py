"""第 1 步：分块 -> 向量索引 + 中文 BM25 索引 -> 双路落盘并自检。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag_core.config import OUTPUT_DIR, RagError, load_settings
from rag_core.corpus import build_corpus
from rag_core.formatting import Tee, ensure_utf8_stdio, heading, src_label
from rag_core.indexer import bm25_node_ids, build_index, load_index
from rag_core.providers import get_embed_model

REPORT = OUTPUT_DIR / "step1_build_index.md"


def _sample_index(nodes, keyword: str) -> int:
    for index, node in enumerate(nodes):
        if keyword in node.text:
            return index
    return min(len(nodes) - 1, len(nodes) // 2)


def run(tee: Tee) -> None:
    heading(tee, "第 1 步 建库：分块 + 向量索引 + 中文 BM25 索引")
    settings = load_settings("embed")
    tee("运行配置（不含密钥）:")
    for line in settings.safe_summary():
        tee(f"  {line}")

    tee("")
    tee("[1/5] 语料扫描")
    corpus = build_corpus(settings)
    tee(f"  资料目录 : {settings.data_dir}")
    tee("  扫描规则 : 递归 .md/.ts，排除 node_modules/dist/.git/.venv/__pycache__")
    tee(f"  文件数   : {len(corpus.files)}")
    tee(f"  字符总数 : {corpus.total_chars:,}")
    for item in corpus.files:
        tee(f"    - {item.path}  ({item.lines} 行, {item.bytes} bytes)")

    tee("")
    tee("[2/5] 分块")
    tee(f"  chunk_size={settings.chunk_size}, chunk_overlap={settings.chunk_overlap}, 节点数={len(corpus.nodes)}")
    tee(f"  分块口径={corpus.chunk_unit}, 去重丢弃={corpus.dropped_duplicates} 块")
    sample = corpus.nodes[_sample_index(corpus.nodes, "有效期")]
    meta = dict(sample.metadata or {})
    tee("  分块样例:")
    tee(f"    node_id : {sample.node_id}")
    tee(f"    source  : {src_label(meta)}")
    tee(f"    section : {meta.get('section')}")
    tee(f"    hash    : {meta.get('chunk_hash')}")
    tee(f"    text    : {sample.text[:200]}")

    tee("")
    tee("[3/5] Embedding")
    tee(f"  backend={settings.embed_backend}, model={settings.embed_model}, dim={settings.embed_dim}")
    embed_model = get_embed_model(settings)

    tee("")
    tee("[4/5] 双路索引落盘与自检")
    index_id, index_dir = build_index(settings, embed_model, corpus)
    loaded = load_index(settings, embed_model)
    vector_ids = {node.node_id for node in loaded.nodes}
    bm25_ids = bm25_node_ids(loaded)
    if len(loaded.nodes) != len(corpus.nodes) or len(bm25_ids) != len(corpus.nodes) or vector_ids != bm25_ids:
        raise RagError(
            "RAG-E05",
            f"双路索引自检失败：语料={len(corpus.nodes)}，vector={len(vector_ids)}，"
            f"bm25={len(bm25_ids)}，集合一致={vector_ids == bm25_ids}",
            "step1.run",
            "删除 storage/ 后重新运行 step1",
        )
    tee(f"  index_id  : {index_id}")
    tee(f"  向量索引  : {index_dir / 'vector'}  (SimpleVectorStore)")
    tee(f"  BM25 索引 : {index_dir / 'bm25'}  (tokenizer=jieba)")
    tee(f"  同一批节点: vector={len(vector_ids)}, bm25={len(bm25_ids)}, 集合一致={vector_ids == bm25_ids}")
    probe = loaded.bm25.retrieve(settings.question)[:1]
    if probe:
        tee(f"  BM25 自检 : Top-1 = {src_label(dict(probe[0].node.metadata or {}))}")
    tee(f"  指针      : {settings.storage_dir / 'current.json'}")

    tee("")
    tee("[5/5] 完成")
    tee(f"  语料指纹 : {corpus.sha256}")
    tee(f"  证据文件 : {REPORT}")


def main() -> int:
    ensure_utf8_stdio()
    with Tee(REPORT) as tee:
        try:
            run(tee)
        except RagError as exc:
            tee("")
            tee(exc.render())
            return exc.exit_code
        except KeyboardInterrupt:
            tee("")
            tee("[EXIT 130] 已中断")
            return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
