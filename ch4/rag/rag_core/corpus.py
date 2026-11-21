"""读取 data/ 语料、分块，并回填章节与行号。"""

from __future__ import annotations

import bisect
import hashlib
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import TextNode

from rag_core.config import ALLOWED_SUFFIXES, EXCLUDED_DIRS, RagError, Settings

_MD_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_TS_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?P<kind>function|class|interface|type|const|let|var|enum|namespace)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class FileEntry:
    path: str
    sha256: str
    bytes: int
    lines: int


@dataclass
class Corpus:
    nodes: list[TextNode]
    files: list[FileEntry]
    sha256: str
    total_chars: int
    chunk_unit: str
    dropped_duplicates: int


# 只用于回源/去重的元数据不进入 Embedding 与 BM25 文本：
# 十六进制哈希与行号会稀释语义；且文件任何改动都会改变 file_sha256 与行号，
# 使未改动分块的向量也跟着漂移，重建后检索结果不稳定。
EMBED_EXCLUDED_METADATA = ("file_sha256", "chunk_hash", "chunk_index", "start_line", "end_line")


def iter_source_files(data_dir: Path) -> list[Path]:
    if not data_dir.is_dir():
        raise RagError(
            "RAG-E03", f"资料目录不存在: {data_dir}",
            "corpus.iter_source_files", "确认 rag/data 已按约定放入资料副本",
        )
    found: list[Path] = []
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        parts = path.relative_to(data_dir).parts[:-1]
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        found.append(path)
    return found


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_file_hashes(settings: Settings) -> dict[str, str]:
    """相对路径 → sha256；语料指纹与陈旧检测共用同一口径。"""
    return {
        path.relative_to(settings.data_dir).as_posix(): _file_sha256(path)
        for path in iter_source_files(settings.data_dir)
    }


def _fingerprint_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    rows = [f"{rel}:{digest}" for rel, digest in pairs]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def corpus_fingerprint(settings: Settings) -> str:
    return _fingerprint_pairs(corpus_file_hashes(settings).items())


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _line_of(offset: int, starts: list[int]) -> int:
    return bisect.bisect_right(starts, offset)


def _section_label(match: re.Match[str], suffix: str) -> str:
    """统一口径：只保留标题/声明本身，不带 Markdown 记号，最多 80 字符。"""
    if suffix.lower() == ".md":
        return match.group(2).strip()[:80]
    return f"{match.group('kind')} {match.group('name')}"[:80]


def _section_index(text: str, suffix: str, starts: list[int]) -> list[tuple[int, str]]:
    pattern = _MD_HEADING if suffix.lower() == ".md" else _TS_DECL
    return [(_line_of(match.start(), starts), _section_label(match, suffix)) for match in pattern.finditer(text)]


def _section_for(line_no: int, sections: list[tuple[int, str]]) -> str:
    if line_no <= 0:
        return "(未定位)"
    label = "(文件头部)"
    for start_line, title in sections:
        if start_line > line_no:
            break
        label = title
    return label


def _build_splitter(settings: Settings) -> tuple[SentenceSplitter, str]:
    """返回 (splitter, chunk_unit)。chunk_unit 必须进 manifest：
    同一组 384/76 在 token 与字符两种口径下切出的块完全不同。
    """
    kwargs: dict = {
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "include_metadata": True,
    }
    unit = "token"
    try:
        from llama_index.core.utils import get_tokenizer

        get_tokenizer()
    except (ImportError, OSError, ValueError, RuntimeError):
        # 只降级依赖缺失/词表加载类异常；实现错误（AttributeError 等）不应被吞掉。
        kwargs["tokenizer"] = list
        unit = "char"
        print("  [warn] tiktoken 词表不可用，分块改为按字符计数", file=sys.stderr)
    return SentenceSplitter(**kwargs), unit


def build_corpus(settings: Settings) -> Corpus:
    files = iter_source_files(settings.data_dir)
    if not files:
        raise RagError(
            "RAG-E03", f"资料目录没有可索引的 .md/.ts 文件: {settings.data_dir}",
            "corpus.build_corpus", "先按约定把资料副本放入 rag/data/",
        )

    splitter, chunk_unit = _build_splitter(settings)
    nodes: list[TextNode] = []
    entries: list[FileEntry] = []
    seen_chunks: set[str] = set()
    dropped_duplicates = 0
    total_chars = 0

    for path in files:
        rel = path.relative_to(settings.data_dir).as_posix()
        # 只读一次：哈希与正文都从同一份字节来，避免二次读取造成 TOCTOU。
        # 正文统一成 LF，避免 \r 进入 Embedding / BM25 / 提示词。
        payload = path.read_bytes()
        text = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        file_sha = hashlib.sha256(payload).hexdigest()
        # 空文件也登记进 files，保证 manifest.files 与语料指纹的输入集合一致。
        entries.append(FileEntry(rel, file_sha, len(payload), text.count("\n") + 1))
        total_chars += len(text)
        if not text.strip():
            continue

        document = Document(text=text, metadata={"source": rel, "file_sha256": file_sha})
        split_nodes = splitter.get_nodes_from_documents([document])
        starts = _line_starts(text)
        sections = _section_index(text, path.suffix, starts)

        # 块起点游标：overlap 会让下一块起点落在上一块内部，搜索窗口必须回到
        # 上一块起点而不是上一块末尾，否则 find 必然落空并回退到全文件首次匹配，
        # 重复文本（样板代码、import 块、表格行）的行号与章节就会错位。
        prev_start = -1

        for position, node in enumerate(split_nodes):
            needle = (node.text or "").strip()
            if not needle:
                continue
            offset = text.find(needle, 0 if prev_start < 0 else prev_start)
            if offset < 0:
                offset = text.find(needle)
            if offset < 0:
                start_line = end_line = -1
            else:
                prev_start = offset
                end_offset = offset + len(needle)
                start_line = _line_of(offset, starts)
                end_line = _line_of(max(end_offset - 1, offset), starts)

            digest = hashlib.sha256(needle.encode("utf-8")).hexdigest()
            # 正文完全相同的块只保留首次出现：重复文件、复制粘贴的样板会挤占 Top-K。
            if digest in seen_chunks:
                dropped_duplicates += 1
                continue
            seen_chunks.add(digest)

            chunk_hash = digest[:12]
            metadata = {
                "source": rel,
                "file_sha256": file_sha,
                "section": _section_for(start_line, sections),
                "start_line": start_line,
                "end_line": end_line,
                "chunk_hash": chunk_hash,
                "chunk_index": position,
            }
            nodes.append(
                TextNode(
                    id_=f"{rel}:{start_line}-{end_line}:{chunk_hash}:{position}",
                    text=needle,
                    metadata=metadata,
                    excluded_embed_metadata_keys=list(EMBED_EXCLUDED_METADATA),
                    excluded_llm_metadata_keys=list(EMBED_EXCLUDED_METADATA),
                )
            )

    if not nodes:
        raise RagError("RAG-E03", "分块结果为空", "corpus.build_corpus", "检查资料文件内容")

    return Corpus(
        nodes=nodes,
        files=entries,
        sha256=_fingerprint_pairs((item.path, item.sha256) for item in entries),
        total_chars=total_chars,
        chunk_unit=chunk_unit,
        dropped_duplicates=dropped_duplicates,
    )
