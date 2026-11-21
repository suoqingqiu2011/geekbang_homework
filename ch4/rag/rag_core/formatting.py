"""stdout 与文件同源输出（stderr 仅诊断），以及来源标签、排名表格。"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from rag_core.config import Settings
from rag_core.retrieval import Row

_WHITESPACE = re.compile(r"\s+")


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


class Tee:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("w", encoding="utf-8", newline="\n")
        self._broken = False

    def __call__(self, text: str = "") -> None:
        print(text)
        if self._broken:
            return
        try:
            self._handle.write(text + "\n")
            self._handle.flush()
        except OSError as exc:
            # 磁盘满/文件被占用时降级为仅屏幕输出，并明确告知报告已不可信。
            self._broken = True
            print(
                f"  [warn] 报告写入失败（{type(exc).__name__}），后续仅输出到屏幕",
                file=sys.stderr,
            )

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass

    def __enter__(self) -> "Tee":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def heading(tee: Tee, title: str) -> None:
    tee("")
    tee("=" * 72)
    tee(title)
    tee("=" * 72)


def section(tee: Tee, title: str) -> None:
    tee("")
    tee("-" * 72)
    tee(title)
    tee("-" * 72)


def preview(text: str, limit: int = 120) -> str:
    return _WHITESPACE.sub(" ", text or "").strip()[:limit]


def src_ref(meta: dict) -> str:
    source = meta.get("source") or "?"
    start = meta.get("start_line")
    end = meta.get("end_line")
    if isinstance(start, int) and isinstance(end, int) and start > 0:
        return f"{source} L{start}-L{end}"
    return str(source)


def src_label(meta: dict) -> str:
    base = src_ref(meta)
    section_name = (meta.get("section") or "").strip()
    return f"{base} {section_name}".strip()


def rrf_note(settings: Settings) -> str:
    vector, bm25 = settings.hybrid_weights
    return (
        f"加权 RRF=Σ w/({settings.rrf_k:g}+rank)，w=向量 {vector:g} / BM25 {bm25:g}；"
        "默认 1/1 等价标准 RRF，零分候选先过滤；仅用于排序，不可与余弦/BM25 直接比较"
    )


def print_rows(tee: Tee, title: str, rows: list[Row], note: str = "") -> None:
    section(tee, title)
    if note:
        tee(f"  分数语义: {note}")
    if not rows:
        tee("  <无结果>")
        return
    for row in rows:
        hit = ""
        if row.hits:
            hit = "  [" + ", ".join(f"{name}#{rank}" for name, rank in sorted(row.hits.items())) + "]"
        tee(f"  {row.rank:>2}. {row.kind}={row.score:.4f}  {src_label(row.meta)}{hit}")
        tee(f"      {preview(row.node.text)}")
