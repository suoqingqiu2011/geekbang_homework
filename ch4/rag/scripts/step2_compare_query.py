"""第 2 步：向量 / 中文 BM25 / RRF 三路对比，并用混合证据生成带引用回答。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag_core.answering import answer, render_answer
from rag_core.config import OUTPUT_DIR, RagError, load_settings
from rag_core.corpus import build_corpus
from rag_core.formatting import Tee, ensure_utf8_stdio, heading, print_rows, rrf_note, section, src_label
from rag_core.indexer import build_index, load_index
from rag_core.providers import get_embed_model, get_llm
from rag_core.retrieval import (
    build_hybrid_retriever,
    build_vector_retriever,
    hit_map,
    route_overlap,
    to_rows,
)

REPORT = OUTPUT_DIR / "step2_compare_query.md"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="三路检索对比")
    parser.add_argument("--question", default="", help="覆盖默认问题")
    parser.add_argument("--allow-stale", action="store_true", help="允许语料变更后继续使用旧索引")
    parser.add_argument("--auto-build", action="store_true", help="索引缺失或过期时自动重建")
    return parser.parse_args()


def _load(settings, embed_model, allow_stale: bool, auto_build: bool, tee: Tee):
    try:
        return load_index(settings, embed_model, allow_stale=allow_stale)
    except RagError as exc:
        if not (auto_build and exc.code == "RAG-E04"):
            raise
        tee(f"  索引不可用（{exc.code}），按 --auto-build 重建 ...")
        corpus = build_corpus(settings)
        index_id, _ = build_index(settings, embed_model, corpus)
        tee(f"  已重建: {index_id}")
        return load_index(settings, embed_model, allow_stale=False)


def run(tee: Tee, args: argparse.Namespace) -> None:
    settings = load_settings("all")
    question = (args.question or settings.question).strip()

    heading(tee, "第 2 步 三路检索对比 + 混合检索回答")
    tee(f"问题: {question}")
    tee("说明: q4-2.md 正文写“前 3 条”、代码块写“前 5 条”，本报告统一展示 Top-5。")

    embed_model = get_embed_model(settings)
    loaded = _load(settings, embed_model, args.allow_stale, args.auto_build, tee)

    tee("")
    tee(f"索引: {loaded.index_id}  节点数: {len(loaded.nodes)}")
    tee(f"Embedding: {settings.embed_backend}/{settings.embed_model}/{settings.embed_dim}d")
    tee(f"BM25 分词: jieba   候选深度: {settings.candidate_k}   展示条数: {settings.top_k}")

    llm = get_llm(settings.llm_primary)
    hybrid = build_hybrid_retriever(loaded.index, loaded.bm25, settings, llm)
    vector_results = build_vector_retriever(loaded.index, settings).retrieve(question)
    bm25_results = loaded.bm25.retrieve(question)
    # 三路共用同一次检索结果：向量/BM25 各查一次，RRF 直接在这批结果上算，
    # 避免 QueryFusionRetriever 内部再查一遍（重复 Embedding 调用）。
    hybrid_results = hybrid.fuse([vector_results, bm25_results])

    merged = hit_map({"vector": vector_results, "bm25": bm25_results})
    print_rows(tee, f"向量检索 Top-{settings.top_k}",
               to_rows(vector_results, "cos", merged, settings.top_k), "余弦相似度，越大越相关")
    print_rows(tee, f"BM25 检索 Top-{settings.top_k}",
               to_rows(bm25_results, "bm25", merged, settings.top_k), "BM25 得分，越大越相关")

    hybrid_rows = to_rows(hybrid_results, "rrf", merged, settings.top_k)
    print_rows(tee, f"RRF 混合检索 Top-{settings.top_k}", hybrid_rows, rrf_note(settings))

    stats = route_overlap(merged, hybrid_rows)
    hybrid_ids = [row.node.node_id for row in hybrid_rows]
    tee("")
    tee("混合命中构成:")
    tee(f"  双路命中={stats['both']}  仅向量={stats['vector_only']}  仅BM25={stats['bm25_only']}")
    tee(f"  自检: 唯一节点={len(set(hybrid_ids))}/{len(hybrid_ids)}  双路命中={stats['both']}")
    if len(hybrid_ids) != len(set(hybrid_ids)):
        raise RagError(
            "RAG-E07", "混合检索结果存在重复节点",
            "step2.run", "检查 RRF 融合键是否统一为 node_id",
        )
    if question == settings.question and stats["both"] == 0:
        raise RagError(
            "RAG-E07", "混合检索没有双路命中节点",
            "step2.run", "检查向量/BM25 是否返回同一批节点",
        )

    section(tee, "混合检索证据")
    for row in hybrid_rows:
        tee(f"  [{row.rank}] {src_label(row.meta)}")

    result = answer(settings, question, hybrid_rows)
    render_answer(tee, result, hybrid_rows)
    tee("")
    tee(f"证据文件: {REPORT}")


def main() -> int:
    ensure_utf8_stdio()
    args = _parse_args()
    with Tee(REPORT) as tee:
        try:
            run(tee, args)
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
