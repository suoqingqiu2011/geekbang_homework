from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SLUG = re.compile(r"[^a-z0-9]+")
_OPEN = re.compile(r"^---[ \t]*\r?\n")
_CLOSE = re.compile(r"\r?\n---[ \t]*(?:\r?\n|$)")
_CITATION_REF = re.compile(r"\[(\d+)\](?!\()")
_CITATION_LINE = re.compile(r"^\[(\d+)\]\s+(.+)$")


def slugify(key: str) -> str:
    slug = _SLUG.sub("-", key.strip().lower()).strip("-")
    return slug[:60] or "memory"


def concept_relpath(record: dict) -> str:
    return f"{slugify(record['key'])}-{record['id'][-8:]}.md"


def _emit(value, indent: int) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}"
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_emit(item, indent + 1))
            elif isinstance(item, list) and item and any(isinstance(entry, (dict, list)) for entry in item):
                lines.append(f"{pad}{key}:")
                lines.append(_emit(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_emit(item, indent + 1))
            else:
                lines.append(f"{pad}- {json.dumps(item, ensure_ascii=False)}")
        return "\n".join(lines)
    return pad + json.dumps(value, ensure_ascii=False)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def build_body(content: str, sources: list[str]) -> str:
    lines = ["# Content", "", content.strip(), ""]
    if sources:
        lines += ["# Citations", ""]
        for index, source in enumerate(sources, 1):
            lines.append(f"[{index}] {source}")
        lines.append("")
    return "\n".join(lines)


def citation_errors(body: str, sources: list[str]) -> list[str]:
    errors: list[str] = []
    if body.count("# Citations") != 1:
        errors.append("body must contain exactly one '# Citations' section")
        return errors
    content, _, citations = body.partition("# Citations")
    lines = [line.strip() for line in citations.splitlines() if line.strip()]
    if len(lines) != len(sources):
        errors.append(f"citation lines ({len(lines)}) != sources ({len(sources)})")
        return errors
    for expected, line in enumerate(lines, 1):
        match = _CITATION_LINE.match(line)
        if not match:
            errors.append(f"citation line {expected} must be '[n] resource': {line[:80]}")
            continue
        number = int(match.group(1))
        resource = match.group(2).strip()
        if number != expected:
            errors.append(f"citation numbering mismatch at line {expected}: [{number}]")
        if resource != sources[expected - 1]:
            errors.append(f"citation [{number}] does not match source #{expected}")
    for match in _CITATION_REF.finditer(content):
        number = int(match.group(1))
        if number < 1 or number > len(sources):
            errors.append(f"citation reference [{number}] is outside sources 1..{len(sources)}")
    return errors


def split_body(text: str) -> str:
    opening = _OPEN.match(text)
    if not opening:
        return text
    rest = text[opening.end():]
    closing = _CLOSE.search(rest)
    if not closing:
        return ""
    return rest[closing.end():]


def extract_content(body: str) -> str:
    text = body.strip()
    marker = text.find("\n# Citations")
    if marker != -1:
        text = text[:marker]
    if text.startswith("# Content"):
        text = text[len("# Content"):]
    return text.strip()


def write_document(target: Path, frontmatter: dict, body: str) -> None:
    document = "---\n" + _emit(frontmatter, 0) + "\n---\n\n" + body.rstrip() + "\n"
    atomic_write_text(target, document)
