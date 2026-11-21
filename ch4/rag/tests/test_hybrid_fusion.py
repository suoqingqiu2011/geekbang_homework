"""混合检索：RRF 权重必须真实参与融合（官方实现会忽略 retriever_weights）。"""

from __future__ import annotations

from typing import List, Sequence

import pytest
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.llms.mock import MockLLM
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES, QueryFusionRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from rag_core.retrieval import WeightedRRFRetriever, hit_map, route_overlap, to_rows


class _FixedRetriever(BaseRetriever):
    """按固定顺序返回节点，用于构造两路不同的排名。"""

    def __init__(self, nodes: List[TextNode], zero: Sequence[str] = ()) -> None:
        self.nodes = nodes
        self.zero = set(zero)
        super().__init__()

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        return [
            NodeWithScore(
                node=node,
                score=0.0 if node.node_id in self.zero else 1.0 - index * 0.1,
            )
            for index, node in enumerate(self.nodes)
        ]


def _retriever(
    routes: List[BaseRetriever],
    weights: List[float],
    top_k: int = 2,
) -> WeightedRRFRetriever:
    return WeightedRRFRetriever(
        retrievers=routes,
        llm=MockLLM(),
        weights=weights,
        rrf_k=60.0,
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=top_k,
        num_queries=1,
        use_async=False,
    )


def _fuse(weights: List[float]) -> List[NodeWithScore]:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")
    return _retriever([_FixedRetriever([a, b]), _FixedRetriever([b, a])], weights).retrieve("q")


def test_weights_change_ranking() -> None:
    assert _fuse([0.9, 0.1])[0].node.node_id == "a"
    assert _fuse([0.1, 0.9])[0].node.node_id == "b"


def test_weighted_scores_match_formula() -> None:
    scores = {item.node.node_id: item.score for item in _fuse([0.8, 0.2])}
    assert scores["a"] == pytest.approx(0.8 / 60 + 0.2 / 61)
    assert scores["b"] == pytest.approx(0.8 / 61 + 0.2 / 60)


def test_zero_score_candidates_do_not_enter_fusion() -> None:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")
    retriever = _retriever(
        [_FixedRetriever([a, b]), _FixedRetriever([b, a], zero=("a",))],
        [0.5, 0.5],
    )

    scores = {item.node.node_id: item.score for item in retriever.retrieve("q")}

    assert scores["a"] == pytest.approx(0.5 / 60)
    assert scores["b"] == pytest.approx(0.5 / 61 + 0.5 / 60)


def test_fuse_reuses_given_route_results() -> None:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")
    retriever = _retriever([_FixedRetriever([a]), _FixedRetriever([b])], [0.9, 0.1])

    fused = retriever.fuse(
        [
            [NodeWithScore(node=a, score=1.0), NodeWithScore(node=b, score=0.5)],
            [NodeWithScore(node=b, score=1.0), NodeWithScore(node=a, score=0.5)],
        ]
    )

    assert [item.node.node_id for item in fused] == ["a", "b"]
    assert fused[0].score == pytest.approx(0.9 / 60 + 0.1 / 61)


def test_equal_weights_match_official_rrf() -> None:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")
    c = TextNode(text="C", id_="c")

    ours = _retriever(
        [_FixedRetriever([a, b]), _FixedRetriever([b, c])],
        [1.0, 1.0],
        top_k=3,
    )
    official = QueryFusionRetriever(
        retrievers=[_FixedRetriever([a, b]), _FixedRetriever([b, c])],
        llm=MockLLM(),
        mode=FUSION_MODES.RECIPROCAL_RANK,
        similarity_top_k=3,
        num_queries=1,
        use_async=False,
    )

    def rows(items: List[NodeWithScore]) -> List[tuple[str, float]]:
        return [(item.node.node_id, round(item.score, 10)) for item in items]

    assert rows(ours.retrieve("q")) == rows(official.retrieve("q"))


def test_hit_map_ranks_follow_score_order() -> None:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")

    merged = hit_map(
        {"vector": [NodeWithScore(node=b, score=0.2), NodeWithScore(node=a, score=0.9)]}
    )

    assert merged["a"]["vector"] == 1
    assert merged["b"]["vector"] == 2


def test_route_overlap_has_only_two_route_buckets() -> None:
    a = TextNode(text="A", id_="a")
    b = TextNode(text="B", id_="b")
    merged = {"a": {"vector": 1, "bm25": 1}, "b": {"vector": 2}}
    rows = to_rows(
        [NodeWithScore(node=a, score=1.0), NodeWithScore(node=b, score=0.5)],
        "rrf",
        merged,
    )

    assert route_overlap(merged, rows) == {"both": 1, "vector_only": 1, "bm25_only": 0}
