"""引用解析：连续引用、逗号引用、越界编号与 Markdown 链接。"""

from __future__ import annotations

import pytest
from llama_index.core.schema import TextNode

from rag_core.answering import AnswerResult, answer, parse_citations, render_answer
from rag_core.retrieval import Row


class _FakeTee:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)


def test_consecutive_citations_are_all_parsed() -> None:
    assert parse_citations("结论见 [1][2]。", 3) == ([1, 2], [])


def test_comma_separated_citations_are_parsed() -> None:
    assert parse_citations("结论见 [1, 2]。", 3) == ([1, 2], [])
    assert parse_citations("结论见 [1，2]。", 3) == ([1, 2], [])


def test_citation_right_after_chinese_char_is_parsed() -> None:
    assert parse_citations("有效期为 15 分钟[1]。", 2) == ([1], [])


def test_out_of_range_and_duplicate_are_reported() -> None:
    assert parse_citations("[1][1][9][100]", 3) == ([1], [9, 100])


def test_markdown_link_is_not_treated_as_citation() -> None:
    assert parse_citations("参见 [1](https://example.com) 与 [2]。", 3) == ([2], [])


def test_empty_evidence_short_circuits_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("空证据不得调用生成模型")

    monkeypatch.setattr("rag_core.answering.complete_with_fallback", boom)
    result = answer(None, "问题", [])

    assert "证据不足" in result.text
    assert result.model == "(未调用)"
    assert result.cited == [] and result.invalid == []
    assert result.prompt == ""


def test_render_answer_prints_citation_sources() -> None:
    row = Row(
        rank=1,
        score=1.0,
        kind="rrf",
        node=TextNode(text="证据", metadata={"source": "artifacts/login-flow.md"}),
    )
    result = AnswerResult(text="有效期为 20 分钟 [1]。", model="deepseek-chat", cited=[1])
    tee = _FakeTee()

    render_answer(tee, result, [row], "更新后回答")

    output = "\n".join(tee.lines)
    assert "更新后回答（模型: deepseek-chat）" in output
    assert "[1] artifacts/login-flow.md" in output
    assert "结论: 通过" in output
