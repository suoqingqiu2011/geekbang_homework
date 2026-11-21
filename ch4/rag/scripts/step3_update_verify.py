"""第 3 步：把副本中的有效期改为 20 分钟，重建双路索引并断言旧分块退出。

默认流程会先把 15 分钟版本快照到 storage/.baseline/login-flow.md；
`--restore` 用该快照恢复 15 分钟并重建双路索引。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag_core.answering import answer, render_answer
from rag_core.config import OUTPUT_DIR, RagError, Settings, load_settings
from rag_core.corpus import build_corpus
from rag_core.formatting import Tee, ensure_utf8_stdio, heading, print_rows, rrf_note, section
from rag_core.indexer import build_index, load_index
from rag_core.providers import get_embed_model, get_llm
from rag_core.retrieval import build_hybrid_retriever, hit_map, to_rows
from llama_index.core.schema import TextNode

REPORT = OUTPUT_DIR / "step3_update_verify.md"
REPORT_RESTORE = OUTPUT_DIR / "step3_restore.md"
OLD_VALUE = "15 分钟"
NEW_VALUE = "20 分钟"
TARGET_REL = Path("artifacts") / "login-flow.md"
BASELINE_REL = Path(".baseline") / "login-flow.md"
SECTION_KEYWORD = "会话有效期"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _norm(text: str) -> str:
    return text.replace(" ", "").replace("\u3000", "")


def _section_span(lines: list[str], keyword: str) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("#") and keyword in line), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("#"):
        end += 1
    return start, end


def _section_text(text: str, keyword: str) -> str:
    lines = text.splitlines()
    span = _section_span(lines, keyword)
    if span is None:
        return ""
    start, end = span
    return "\n".join(lines[start:end]).strip()


def _section_state(text: str) -> tuple[str, bool, bool]:
    section_text = _section_text(text, SECTION_KEYWORD)
    return section_text, OLD_VALUE in section_text, NEW_VALUE in section_text


def rewrite_section(target: Path, old: str, new: str) -> tuple[str, str, int]:
    """只在目标小节内替换，且按字节读写以保留原有行尾（CRLF 不被归一）。"""
    before = target.read_bytes().decode("utf-8")
    lines = before.splitlines(keepends=True)
    span = _section_span(lines, SECTION_KEYWORD)
    if span is None:
        return before, before, 0

    start, end = span
    count = 0
    for index in range(start, end):
        count += lines[index].count(old)
        lines[index] = lines[index].replace(old, new)
    if count == 0:
        return before, before, 0

    after = "".join(lines)
    target.write_bytes(after.encode("utf-8"))
    return before, after, count


def _baseline_path(settings: Settings) -> Path:
    return settings.storage_dir / BASELINE_REL


def _is_target_node(node: TextNode) -> bool:
    meta = node.metadata or {}
    return meta.get("source") == TARGET_REL.as_posix() and SECTION_KEYWORD in str(meta.get("section") or "")


def _snapshot_baseline(target: Path, baseline: Path) -> bool:
    """只在目标小节仍含旧值时写快照，避免把 20 分钟版本覆盖成基线。"""
    if not target.is_file():
        return False
    data = target.read_bytes()
    if OLD_VALUE not in _section_text(data.decode("utf-8"), SECTION_KEYWORD):
        return False
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(data)
    return True


def _restore_baseline(baseline: Path, target: Path) -> bool:
    if not baseline.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(baseline.read_bytes())
    return True


def _check(tee: Tee, name: str, ok: bool, detail: str) -> bool:
    tee(f"  {'✅' if ok else '❌'} {name}  {detail}")
    return ok


def run(tee: Tee) -> None:
    settings = load_settings("all")
    question = settings.question
    target = settings.data_dir / TARGET_REL
    embed_model = get_embed_model(settings)
    llm = get_llm(settings.llm_primary)

    heading(tee, "第 3 步 资料更新 -> 双路索引重建 -> 旧分块退出验证")
    tee(f"问题     : {question}")
    tee(f"目标资料 : {target}")
    tee(f"更新规则 : {OLD_VALUE} -> {NEW_VALUE}")
    if not target.is_file():
        raise RagError("RAG-E03", f"目标资料不存在: {target}", "step3.run", "确认 rag/data 资料副本完整")

    before_bytes = target.read_bytes()
    before_text = before_bytes.decode("utf-8")
    before_section, has_old, has_new = _section_state(before_text)
    if not before_section:
        raise RagError(
            "RAG-E03", "资料缺少「会话有效期」小节",
            "step3.run", "确认 artifacts/login-flow.md 包含 '## 6. 会话有效期'",
        )
    if not has_old and not has_new:
        raise RagError(
            "RAG-E03", "「会话有效期」小节既不含 15 分钟也不含 20 分钟",
            "step3.run", "检查资料副本的 §6 内容或从 login_demo 重新复制",
        )

    baseline = _baseline_path(settings)
    section(tee, "[0] 基线快照")
    if _snapshot_baseline(target, baseline):
        tee(f"  已保存「{OLD_VALUE}」基线: {baseline}")
    elif baseline.is_file():
        tee(f"  沿用已存在基线: {baseline}")
    else:
        tee(f"  当前资料不含「{OLD_VALUE}」且无基线快照，--restore 将不可用")

    baseline_old = 0

    section(tee, "[A] 更新前基线")
    if has_old:
        old_loaded = load_index(settings, embed_model)
        old_results = build_hybrid_retriever(old_loaded.index, old_loaded.bm25, settings, llm).retrieve(question)
        baseline_old = sum(1 for item in old_results if _is_target_node(item.node) and OLD_VALUE in item.node.text)
        tee(f"  旧索引: {old_loaded.index_id}  节点数: {len(old_loaded.nodes)}")
        tee(f"  Top-{settings.candidate_k} §6 命中「{OLD_VALUE}」的节点: {baseline_old}")
        tee("  旧版本目录保留在 storage/ 下，仅供本报告对照")
    else:
        tee(f"  §6 已无「{OLD_VALUE}」，判定为幂等重跑，跳过基线对照")

    section(tee, "[B] 改写资料副本")
    _, after_text, replaced = rewrite_section(target, OLD_VALUE, NEW_VALUE)
    if has_old and replaced == 0:
        raise RagError(
            "RAG-E05", "§6 内未找到「15 分钟」，资料可能已被手工修改",
            "step3.run", "检查 data/artifacts/login-flow.md 的 §6 内容",
        )
    after_section = _section_text(after_text, SECTION_KEYWORD)
    tee(f"  替换处数: {replaced}")
    tee(f"  sha256  : {_sha(before_text)} -> {_sha(after_text)}")
    tee("  改写前:")
    for line in before_section.splitlines():
        tee(f"    {line}")
    tee("  改写后:")
    for line in after_section.splitlines():
        tee(f"    {line}")

    section(tee, "[C] 全量重建双路索引")
    try:
        corpus = build_corpus(settings)
        index_id, index_dir = build_index(settings, embed_model, corpus)
    except Exception:
        # 重建失败时把语料写回更新前版本，避免“资料已改、索引仍旧”的悬空状态。
        target.write_bytes(before_bytes)
        tee(f"  重建失败，已回滚资料副本: {target}")
        raise
    tee(f"  新索引  : {index_id}  节点数: {len(corpus.nodes)}")
    tee(f"  向量索引: {index_dir / 'vector'}")
    tee(f"  BM25索引: {index_dir / 'bm25'}  (tokenizer=jieba)")
    tee(f"  语料指纹: {corpus.sha256}")

    section(tee, "[D] 旧分块退出校验")
    new_loaded = load_index(settings, embed_model)
    target_nodes = [node for node in new_loaded.nodes if _is_target_node(node)]
    stale_in_index = sum(1 for node in target_nodes if OLD_VALUE in node.text)
    fresh_in_index = sum(1 for node in target_nodes if NEW_VALUE in node.text)

    new_results = build_hybrid_retriever(new_loaded.index, new_loaded.bm25, settings, llm).retrieve(question)
    merged = hit_map({"hybrid": new_results})
    new_rows = to_rows(new_results, "rrf", merged, settings.top_k)
    stale_in_top = sum(1 for item in new_results if _is_target_node(item.node) and OLD_VALUE in item.node.text)
    fresh_in_top = sum(1 for item in new_results if _is_target_node(item.node) and NEW_VALUE in item.node.text)

    checks = [
        _check(tee, "A2 新索引 §6 无旧值", stale_in_index == 0, f"§6 含「{OLD_VALUE}」节点={stale_in_index}"),
        _check(tee, "A3 新索引 §6 含新值", fresh_in_index >= 1, f"§6 含「{NEW_VALUE}」节点={fresh_in_index}"),
        _check(tee, "A4 检索结果 §6 无旧值", stale_in_top == 0, f"Top-{settings.candidate_k} §6 命中旧值={stale_in_top}"),
        _check(tee, "A5 检索结果 §6 有新值", fresh_in_top >= 1, f"Top-{settings.candidate_k} §6 命中新值={fresh_in_top}"),
        _check(tee, "A6 节点数一致", len(new_loaded.nodes) == len(corpus.nodes), f"{len(new_loaded.nodes)} == {len(corpus.nodes)}"),
    ]
    if has_old:
        tee(f"  {'✅' if baseline_old >= 1 else '⚠'} A1 更新前可召回旧值  §6 baseline={baseline_old}")
    else:
        tee("  - A1 跳过（资料已是新值）")

    print_rows(tee, f"更新后 RRF 混合检索 Top-{settings.top_k}", new_rows, rrf_note(settings))

    result = answer(settings, question, new_rows)
    render_answer(tee, result, new_rows, "[E] 更新后重新回答")

    cited_rows = [new_rows[index - 1] for index in result.cited]
    answer_fresh = _norm(NEW_VALUE) in _norm(result.text)
    cited_fresh = bool(cited_rows) and any(
        _is_target_node(row.node) and _norm(NEW_VALUE) in _norm(row.node.text) for row in cited_rows
    )
    cited_stale = any(_is_target_node(row.node) and _norm(OLD_VALUE) in _norm(row.node.text) for row in cited_rows)

    checks.append(_check(tee, "A7 回答使用新值", answer_fresh, f"回答含「{NEW_VALUE}」"))
    checks.append(_check(tee, "A8 引用指向新分块", cited_fresh and not cited_stale, f"有效引用={result.cited}"))
    if _norm(OLD_VALUE) in _norm(result.text):
        tee(f"  ⚠ 回答中仍出现「{OLD_VALUE}」，若为对比表述可忽略")

    tee("")
    if not all(checks):
        raise RagError(
            "RAG-E07", "更新后校验未全部通过",
            "step3.run", "查看上方 ❌ 项，确认资料改写与重建是否完成",
        )
    tee("全部断言通过：旧分块已退出当前检索，回答与引用均基于新资料。")
    tee(f"证据文件: {REPORT}")


def run_restore(tee: Tee) -> None:
    settings = load_settings("embed")
    target = settings.data_dir / TARGET_REL
    baseline = _baseline_path(settings)

    heading(tee, "第 3 步 --restore：恢复 15 分钟基线并重建双路索引")
    tee(f"目标资料 : {target}")
    tee(f"基线快照 : {baseline}")
    if not target.is_file():
        raise RagError("RAG-E03", f"目标资料不存在: {target}", "step3.run_restore", "确认 rag/data 资料副本完整")
    if not baseline.is_file():
        raise RagError(
            "RAG-E03", f"未找到基线快照: {baseline}",
            "step3.run_restore", "先正常运行 step3 生成快照，或从 login_demo 恢复 15 分钟副本",
        )
    baseline_section = _section_text(baseline.read_text(encoding="utf-8"), SECTION_KEYWORD)
    if OLD_VALUE not in baseline_section:
        raise RagError(
            "RAG-E03", "基线快照不含 §6 的 15 分钟",
            "step3.run_restore", "重新运行默认 step3 生成基线，或从 login_demo 恢复 15 分钟副本",
        )

    before_bytes = target.read_bytes()
    before = before_bytes.decode("utf-8")
    _restore_baseline(baseline, target)
    after = target.read_text(encoding="utf-8")
    after_section = _section_text(after, SECTION_KEYWORD)
    tee(f"  sha256  : {_sha(before)} -> {_sha(after)}")
    tee(f"  恢复后 §6 含「{OLD_VALUE}」: {OLD_VALUE in after_section}")

    section(tee, "[R] 重建双路索引")
    embed_model = get_embed_model(settings)
    try:
        corpus = build_corpus(settings)
        index_id, index_dir = build_index(settings, embed_model, corpus)
    except Exception:
        target.write_bytes(before_bytes)
        tee(f"  重建失败，已回滚资料副本: {target}")
        raise
    tee(f"  新索引  : {index_id}  节点数: {len(corpus.nodes)}")
    tee(f"  向量索引: {index_dir / 'vector'}")
    tee(f"  BM25索引: {index_dir / 'bm25'}  (tokenizer=jieba)")
    tee(f"  语料指纹: {corpus.sha256}")

    loaded = load_index(settings, embed_model)
    target_nodes = [node for node in loaded.nodes if _is_target_node(node)]
    stale_in_index = sum(1 for node in target_nodes if OLD_VALUE in node.text)
    fresh_in_index = sum(1 for node in target_nodes if NEW_VALUE in node.text)
    checks = [
        _check(tee, "R1 资料 §6 恢复为旧值", OLD_VALUE in after_section, f"§6 含「{OLD_VALUE}」"),
        _check(tee, "R2 索引 §6 含旧值", stale_in_index >= 1, f"§6 含「{OLD_VALUE}」节点={stale_in_index}"),
        _check(tee, "R3 索引 §6 无新值", fresh_in_index == 0, f"§6 含「{NEW_VALUE}」节点={fresh_in_index}"),
    ]
    if not all(checks):
        raise RagError("RAG-E07", "恢复后校验未通过", "step3.run_restore", "检查基线快照与资料副本")
    tee("恢复完成：资料与双路索引均已回到 15 分钟，可重跑 step2 复现截图。")
    tee(f"证据文件: {REPORT_RESTORE}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="资料更新与索引重建验证")
    parser.add_argument("--restore", action="store_true", help="恢复 15 分钟基线并重建双路索引")
    return parser.parse_args()


def main() -> int:
    ensure_utf8_stdio()
    args = _parse_args()
    report = REPORT_RESTORE if args.restore else REPORT
    with Tee(report) as tee:
        try:
            if args.restore:
                run_restore(tee)
            else:
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
