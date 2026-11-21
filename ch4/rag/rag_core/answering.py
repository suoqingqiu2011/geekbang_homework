"""带引用约束的提示词构造、调用与引用解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag_core.config import Settings
from rag_core.formatting import Tee, section, src_label
from rag_core.providers import complete_with_fallback
from rag_core.retrieval import Row

# 支持 [1]、[1][2]、[1,2] 等写法；排除 [1](url) 这类 Markdown 链接。
# 不加 (?<!\w) 后顾：中文紧邻引用（如「有效期为 15 分钟[1]」）也必须识别。
_CITATION = re.compile(r"\[(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)\](?!\()")

_RULES = """你是严谨的技术文档问答助手，只能依据【证据】回答，不得使用证据之外的知识。
回答要求：
1. 第一行用一句话给出直接答案；
2. 每个事实性陈述后必须标注证据编号，格式为 [1]、[2]；
3. 只允许引用【证据】中出现过的编号，不得编造编号；
4. 若证据不足以回答，第一行输出「证据不足：<缺少什么>」，不要猜测。"""


@dataclass
class AnswerResult:
    text: str
    model: str
    cited: list[int] = field(default_factory=list)
    invalid: list[int] = field(default_factory=list)
    prompt: str = ""

    @property
    def citation_ok(self) -> bool:
        return bool(self.cited) and not self.invalid


def build_prompt(question: str, rows: list[Row]) -> str:
    blocks = [f"[{index}] 来源: {src_label(row.meta)}\n内容: {row.node.text}" for index, row in enumerate(rows, start=1)]
    return f"{_RULES}\n\n【证据】\n" + "\n\n".join(blocks) + f"\n\n【问题】{question}\n【回答】"


def parse_citations(text: str, evidence_count: int) -> tuple[list[int], list[int]]:
    seen: list[int] = []
    invalid: list[int] = []
    for match in _CITATION.finditer(text):
        for raw in re.split(r"[,，]", match.group(1)):
            value = int(raw)
            if value < 1 or value > evidence_count:
                if value not in invalid:
                    invalid.append(value)
            elif value not in seen:
                seen.append(value)
    return seen, invalid


def answer(settings: Settings, question: str, rows: list[Row]) -> AnswerResult:
    # 空证据绝不能进入生成环节：没有可引用的材料时，模型只会编。
    if not rows:
        return AnswerResult(
            text="证据不足：混合检索未返回任何证据，未调用生成模型。",
            model="(未调用)",
        )

    prompt = build_prompt(question, rows)
    text, model = complete_with_fallback(settings, prompt)
    cited, invalid = parse_citations(text, len(rows))
    return AnswerResult(text=text, model=model, cited=cited, invalid=invalid, prompt=prompt)


def render_answer(tee: Tee, result: AnswerResult, rows: list[Row], title: str = "混合检索回答") -> None:
    """step2/step3 共用的回答与引用校验渲染。"""
    section(tee, f"{title}（模型: {result.model}）")
    for line in result.text.splitlines():
        tee(line)
    section(tee, "引用校验")
    for index in result.cited:
        tee(f"  [{index}] {src_label(rows[index - 1].meta)}")
    if result.invalid:
        tee(f"  ⚠ 越界编号: {result.invalid}")
    tee(f"  结论: {'通过' if result.citation_ok else '未通过（无有效引用或存在越界编号）'}")
