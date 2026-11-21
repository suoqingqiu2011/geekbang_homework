"""三路检索：向量、中文 BM25、RRF 混合。

ChineseBM25Retriever 契约：
1. 语料侧与查询侧使用同一套 jieba 分词 + stopwords/stemmer/token_pattern；
2. 覆写 persist / from_persist_dir，落盘 tokenizer=jieba 标记；
3. 加载时标记不是 jieba 立即 RAG-E02，不降级；
4. jieba 缺失立即 RAG-E01，绝不静默回退默认分词。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

from rag_core.config import RagError, Settings

# bm25s 首次导入时会执行 bm25s/utils/benchmark.py，其中探测 POSIX 的 resource 模块；
# Windows 没有该模块，于是它在 import 期直接 print 一行提示。该功能只影响
# benchmark 的内存统计（get_max_usage 返回 None），检索本身不受影响；但这行会绕过
# Tee 只出现在屏幕，破坏「屏幕与 output/*.md 同源」，所以在首次导入时就地吞掉。
with contextlib.redirect_stdout(io.StringIO()):
    try:
        import bm25s

        _BM25S_READY = True
    except ImportError:
        bm25s = None
        _BM25S_READY = False


def _require_bm25s() -> Any:
    """依赖缺失延迟到构造/加载阶段报错，避免 import rag_core.* 直接失败。"""
    if not _BM25S_READY:
        raise RagError(
            "RAG-E01", "缺少依赖 bm25s，BM25 无法构建",
            "retrieval._require_bm25s", "pip install -r requirements.txt",
        )
    return bm25s


with contextlib.redirect_stdout(io.StringIO()):
    try:
        from llama_index.retrievers.bm25 import BM25Retriever
    except ImportError:
        class BM25Retriever:  # type: ignore[no-redef]
            """bm25s 缺失时的占位基类；实际使用由 _require_bm25s 抛 RAG-E01。"""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                _require_bm25s()

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.retrievers import QueryFusionRetriever, VectorIndexRetriever
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle, TextNode
from llama_index.core.vector_stores.utils import metadata_dict_to_node, node_to_metadata_dict

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _JIEBA_READY = True
except ImportError:
    jieba = None
    _JIEBA_READY = False

TOKENIZER_MARK = "jieba"
META_FILENAME = "retriever.json"
# BM25 预处理参数的唯一真源：建库、落盘、manifest 与加载校验都读这里，
# 避免「语料侧过滤英文停用词、查询侧不过滤」这类静默不一致。
BM25_STOPWORDS: str | None = None
BM25_STEMMER: str | None = None
BM25_TOKEN_PATTERN = r"(?u)\b\w\w+\b"


def bm25_params() -> dict:
    return {
        "tokenizer": TOKENIZER_MARK,
        "stopwords": BM25_STOPWORDS,
        "stemmer": BM25_STEMMER,
        "token_pattern": BM25_TOKEN_PATTERN,
    }


def _require_jieba() -> Any:
    if not _JIEBA_READY:
        raise RagError(
            "RAG-E01", "缺少依赖 jieba，中文 BM25 无法分词（禁止回退默认分词）",
            "retrieval._require_jieba", "pip install -r requirements.txt",
        )
    return jieba


def _segment(text: str) -> str:
    tokens = [token for token in _require_jieba().lcut(text) if token.strip()]
    return " ".join(tokens)


@dataclass
class Row:
    rank: int
    score: float
    kind: str
    node: TextNode
    hits: dict[str, int] = field(default_factory=dict)

    @property
    def meta(self) -> dict:
        return dict(self.node.metadata or {})


class ChineseBM25Retriever(BM25Retriever):
    def __init__(
        self,
        nodes: List[TextNode] | None = None,
        existing_bm25: Any = None,
        similarity_top_k: int = 10,
        verbose: bool = False,
        **kwargs: Any,
    ) -> None:
        _require_bm25s()
        _require_jieba()
        # 查询侧参数先固定下来，语料侧 tokenize 使用同一口径，两侧必须同构。
        self.stemmer = BM25_STEMMER
        self.skip_stemming = True
        self.token_pattern = BM25_TOKEN_PATTERN
        self.similarity_top_k = similarity_top_k

        if existing_bm25 is not None:
            self.bm25 = existing_bm25
            self.corpus = existing_bm25.corpus
        else:
            if not nodes:
                raise ValueError("Please pass nodes or an existing BM25 object.")
            self.corpus = [node_to_metadata_dict(node) for node in nodes]
            segmented = [_segment(node.get_content(metadata_mode=MetadataMode.EMBED)) for node in nodes]
            engine = bm25s.BM25()
            engine.index(
                bm25s.tokenize(
                    segmented,
                    stopwords=BM25_STOPWORDS,
                    stemmer=None,
                    token_pattern=self.token_pattern,
                    show_progress=False,
                ),
                show_progress=False,
            )
            self.bm25 = engine

        BaseRetriever.__init__(
            self,
            callback_manager=None,
            object_map=None,
            objects=None,
            verbose=verbose,
        )

    def retrieve(self, str_or_query_bundle: Any) -> List[NodeWithScore]:
        if isinstance(str_or_query_bundle, str):
            segmented = _segment(str_or_query_bundle)
        else:
            segmented = _segment(str_or_query_bundle.query_str)
        return super().retrieve(segmented)

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        # 查询侧显式传 stopwords/stemmer/token_pattern，避免官方默认 english
        # 停用词表与语料侧 None 不一致。
        tokenized_query = _require_bm25s().tokenize(
            query_bundle.query_str,
            stopwords=BM25_STOPWORDS,
            stemmer=BM25_STEMMER,
            token_pattern=BM25_TOKEN_PATTERN,
            show_progress=self._verbose,
        )
        indexes, scores = self.bm25.retrieve(
            tokenized_query, k=self.similarity_top_k, show_progress=self._verbose
        )
        nodes: List[NodeWithScore] = []
        for idx, score in zip(indexes[0], scores[0]):
            node_dict = idx if isinstance(idx, dict) else self.corpus[int(idx)]
            nodes.append(NodeWithScore(node=metadata_dict_to_node(node_dict), score=float(score)))
        return nodes

    def persist(self, path: str, **kwargs: Any) -> None:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        self.bm25.save(str(target), corpus=self.corpus, **kwargs)
        meta = {
            "similarity_top_k": self.similarity_top_k,
            "verbose": bool(getattr(self, "_verbose", False)),
            **bm25_params(),
        }
        with (target / META_FILENAME).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False, indent=2))

    @classmethod
    def from_persist_dir(cls, path: str, **kwargs: Any) -> "ChineseBM25Retriever":
        _require_bm25s()
        meta_path = Path(path) / META_FILENAME
        if not meta_path.is_file():
            raise RagError(
                "RAG-E02", f"BM25 索引缺少 {META_FILENAME}，无法确认分词方式",
                "ChineseBM25Retriever.from_persist_dir", "删除 storage/ 后重新运行 step1",
            )
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RagError(
                "RAG-E02", f"BM25 索引标记无法解析（{type(exc).__name__}）",
                "ChineseBM25Retriever.from_persist_dir", "删除 storage/ 后重新运行 step1",
            ) from exc

        # 分词器与预处理参数必须与当前实现完全一致，缺项也视为不一致：
        # 无法确认口径的索引一律拒绝加载，不做静默降级。
        mismatched = [
            f"{key}: 索引={meta.get(key)!r} 当前={expected!r}"
            for key, expected in bm25_params().items()
            if meta.get(key) != expected
        ]
        if mismatched:
            raise RagError(
                "RAG-E02",
                "BM25 索引参数与当前实现不一致（" + "; ".join(mismatched) + "），拒绝加载",
                "ChineseBM25Retriever.from_persist_dir", "删除 storage/ 后用当前配置重新建库",
            )

        engine = bm25s.BM25.load(str(path), load_corpus=True, **kwargs)
        return cls(
            existing_bm25=engine,
            similarity_top_k=int(meta.get("similarity_top_k", 10)),
            verbose=bool(meta.get("verbose", False)),
        )


def build_vector_retriever(index: Any, settings: Settings) -> VectorIndexRetriever:
    return VectorIndexRetriever(index=index, similarity_top_k=settings.candidate_k)


def build_bm25_retriever(nodes: List[TextNode], settings: Settings) -> ChineseBM25Retriever:
    return ChineseBM25Retriever(nodes=nodes, similarity_top_k=settings.candidate_k)


def load_bm25_retriever(index_dir: Path, settings: Settings) -> ChineseBM25Retriever:
    bm25_dir = index_dir / "bm25"
    if not bm25_dir.is_dir():
        raise RagError(
            "RAG-E04", f"BM25 索引目录缺失: {bm25_dir.name}",
            "retrieval.load_bm25_retriever", "重新运行 step1 建库",
        )
    retriever = ChineseBM25Retriever.from_persist_dir(str(bm25_dir))
    retriever.similarity_top_k = settings.candidate_k
    return retriever


class WeightedRRFRetriever(QueryFusionRetriever):
    """标准 RRF 的加权扩展：score = Σ w/(rrf_k+rank)。

    官方 _reciprocal_rerank_fusion 完全不读 retriever_weights（等权写死），
    直接传参只是看起来可调。这里让权重真实生效；默认 1/1 时与官方
    RECIPROCAL_RANK 等权一致。融合前先过滤 score=0 的候选并按剩余名次
    重新排序，键统一用 node_id，与 hit_map 的统计口径一致。
    """

    def __init__(self, weights: List[float], rrf_k: float = 60.0, **kwargs: Any) -> None:
        super().__init__(retriever_weights=weights, **kwargs)
        self.weights = list(weights)
        self.rrf_k = rrf_k

    def _reciprocal_rerank_fusion(
        self, results: dict[tuple[str, int], list[NodeWithScore]]
    ) -> list[NodeWithScore]:
        fused: dict[str, float] = {}
        nodes: dict[str, TextNode] = {}
        for (_query, route), items in results.items():
            weight = self.weights[route % len(self.weights)]
            # 先过滤零分候选再重新排名：BM25 对无词项交集的文档返回 0 分，
            # 但仍占名次；直接参与融合会让噪声候选靠名次得分。
            ranked = [
                item
                for item in sorted(items, key=lambda x: x.score or 0.0, reverse=True)
                if item.score
            ]
            for rank, item in enumerate(ranked):
                key = item.node.node_id
                nodes.setdefault(key, item.node)
                fused[key] = fused.get(key, 0.0) + weight / (self.rrf_k + rank)
        ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
        return [NodeWithScore(node=nodes[key], score=score) for key, score in ordered]

    def fuse(self, routes: list[list[NodeWithScore]]) -> list[NodeWithScore]:
        """对已取到的各路结果直接融合，避免为了出对比报告再检索一遍。"""
        results = {("", index): items for index, items in enumerate(routes)}
        return self._reciprocal_rerank_fusion(results)[: self.similarity_top_k]


def build_hybrid_retriever(
    index: Any,
    bm25: ChineseBM25Retriever,
    settings: Settings,
    llm: Any,
) -> QueryFusionRetriever:
    from llama_index.core.retrievers.fusion_retriever import FUSION_MODES

    return WeightedRRFRetriever(
        retrievers=[build_vector_retriever(index, settings), bm25],
        llm=llm,
        similarity_top_k=settings.top_k,
        num_queries=1,
        mode=FUSION_MODES.RECIPROCAL_RANK,
        weights=list(settings.hybrid_weights),
        rrf_k=settings.rrf_k,
        use_async=False,
        verbose=False,
    )


def _ranked(results: list[NodeWithScore]) -> list[NodeWithScore]:
    """统一排名口径：标签里的 #n 必须与融合时使用的 rank 一致。"""
    return sorted(results, key=lambda item: item.score or 0.0, reverse=True)


def hit_map(routes: dict[str, list[NodeWithScore]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for name, results in routes.items():
        for rank, item in enumerate(_ranked(results), start=1):
            merged.setdefault(item.node.node_id, {})[name] = rank
    return merged


def to_rows(
    results: list[NodeWithScore],
    kind: str,
    merged: dict[str, dict[str, int]] | None = None,
    limit: int | None = None,
) -> list[Row]:
    ordered = _ranked(results)
    picked = ordered[:limit] if limit else ordered
    rows: list[Row] = []
    for rank, item in enumerate(picked, start=1):
        node = item.node
        if not isinstance(node, TextNode):
            continue
        rows.append(
            Row(
                rank=rank,
                score=float(item.score) if item.score is not None else 0.0,
                kind=kind,
                node=node,
                hits=dict((merged or {}).get(node.node_id, {})),
            )
        )
    return rows


def route_overlap(merged: dict[str, dict[str, int]], rows: list[Row]) -> dict[str, int]:
    """只统计由两路召回的行；融合结果必然来自 vector/bm25，不存在第三类来源。"""
    stats = {"both": 0, "vector_only": 0, "bm25_only": 0}
    for row in rows:
        routes = set(merged.get(row.node.node_id, {}).keys())
        if {"vector", "bm25"} <= routes:
            stats["both"] += 1
        elif routes == {"vector"}:
            stats["vector_only"] += 1
        elif routes == {"bm25"}:
            stats["bm25_only"] += 1
    return stats
