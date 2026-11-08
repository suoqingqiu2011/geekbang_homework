"""装配层：策略、运行时、9 个工具、完成检查、受控 Agent 与 Harness API。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smolagents.agents import ToolCallingAgent
from smolagents.memory import ActionStep, ToolCall
from smolagents.monitoring import LogLevel
from smolagents.tools import Tool
from smolagents.utils import AgentMaxStepsError, AgentToolCallError

from audit import AuditLog
from checkpoint import Checkpoint, classify_write, plan_resume
from contracts import (
    AUXILIARY_EVIDENCE_KINDS,
    ActionRequest,
    Approval,
    BudgetExceeded,
    BudgetLimits,
    Decision,
    Evidence,
    GateVerdict,
    HarnessCancelled,
    HarnessError,
    HarnessHalt,
    PolicyDenied,
    ReconcileVerdict,
    RiskClass,
    RunStatus,
    StopReason,
    StructuredError,
    Subject,
    UnknownResult,
    WorkspaceChanged,
    ApprovalRequired,
    WriteState,
    call_signature,
    canonical_json,
    normalize_arguments,
    now_iso,
    sha256_file,
    ulid,
)
from gate import Gate, ToolSpec
from sandbox import Sandbox, SandboxViolation, is_process_alive, kill_tree
from state_store import StateStore

MAX_PATTERN_CHARS = 200
MAX_SEARCH_LINE_CHARS = 2000
MAX_SEARCH_FILES = 2000
SEARCH_DEADLINE_SECONDS = 5.0
MAX_READ_BYTES = 1_000_000
# 粗粒度识别嵌套量词（ReDoS 常见形态，如 (a+)+ / (a*)* / (a+){2,}）。
NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*(?:[+*]|\{[0-9]+(?:,[0-9]*)?\})")

DEFAULT_SUITES: dict[str, list[str]] = {
    "target": ["tests/test_target.py"],
    "boundary": ["tests/test_boundary.py"],
    "regression": ["tests/test_regression.py"],
}


@dataclass
class HarnessPolicy:
    required_scopes: tuple[str, ...] = ("target",)
    max_steps: int = 12
    max_tool_calls: int = 40
    max_writes: int = 10
    max_test_runs: int = 8
    max_seconds: float = 180.0
    repeat_limit: int = 2
    command_timeout: float = 30.0
    approval_ttl_steps: int = 3
    writable_zones: tuple[str, ...] = ("artifacts", "src")
    require_write_approval: bool = True
    suites: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_SUITES.items()})

    def limits(self) -> BudgetLimits:
        return BudgetLimits(
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_writes=self.max_writes,
            max_test_runs=self.max_test_runs,
            max_seconds=self.max_seconds,
        )

    def suite_command(self, scope: str) -> list[str]:
        if scope not in self.suites:
            raise ValueError(f"未知测试范围：{scope}；可用：{sorted(self.suites)}")
        targets = self.suites[scope]
        return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]


@dataclass
class HarnessResult:
    run_id: str
    status: str
    stop_reason: str | None
    output: Any = None
    step: int = 0
    tool_calls: int = 0
    writes: int = 0
    test_runs: int = 0
    fingerprint: str | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    blocked: dict[str, Any] | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "output": self.output,
            "step": self.step,
            "tool_calls": self.tool_calls,
            "writes": self.writes,
            "test_runs": self.test_runs,
            "fingerprint": self.fingerprint,
            "pending": self.pending,
            "blocked": self.blocked,
            "message": self.message,
        }


class CompletionChecker:
    def __init__(self, state: StateStore, policy: HarnessPolicy, fingerprint_fn: Any) -> None:
        self.state = state
        self.policy = policy
        self.fingerprint_fn = fingerprint_fn

    def issues(self) -> list[str]:
        current = self.fingerprint_fn()
        problems: list[str] = []
        for scope in self.policy.required_scopes:
            item = self.latest_acceptance(scope)
            if item is None:
                problems.append(
                    f"缺少 scope={scope} 的测试证据：自报记录不是交付物，必须运行对应测试套件"
                )
                continue
            evidence = Evidence.from_dict(item)
            if not evidence.is_passing():
                problems.append(f"scope={scope} 的最新测试证据未通过（{evidence.kind}）")
            if evidence.fingerprint != current:
                problems.append(f"scope={scope} 的测试证据已过期：工区已变化，需重跑当前版本")
        return problems

    def latest_acceptance(self, scope: str) -> dict[str, Any] | None:
        items = [
            item
            for item in self.state.evidence()
            if item.get("scope") == scope and Evidence.from_dict(item).is_acceptance()
        ]
        if not items:
            return None
        return max(items, key=lambda item: int(item.get("seq", 0)))

    def missing(self) -> list[str]:
        return self.issues()

    def check(self) -> tuple[bool, str]:
        problems = self.issues()
        if problems:
            return False, "；".join(problems)
        return True, "ok"

    def rejections(self) -> list[str]:
        return self.issues()


class ToolRuntime:
    def __init__(self, harness: "GuardedHarness") -> None:
        self.harness = harness
        self.sandbox = harness.sandbox
        self.state = harness.state
        self.audit = harness.audit
        self.policy = harness.policy
        self.workspace = Path(harness.workspace)
        self.current_step = 0
        self._calls: dict[str, str] = {}
        self._last_calls_by_tool: dict[str, str] = {}

    # -- 调用登记 -----------------------------------------------------------
    def register_calls(self, requests: list[ActionRequest]) -> None:
        for request in requests:
            self._calls[request.signature] = request.tool_call_id
            self._last_calls_by_tool[request.tool_name] = request.tool_call_id

    def call_id_for(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            signature = call_signature(tool_name, arguments)
        except ValueError:
            signature = ""
        return self._calls.get(signature) or self._last_calls_by_tool.get(tool_name, "")

    # -- 分发 ---------------------------------------------------------------
    def invoke(self, tool_name: str, arguments: dict[str, Any], *, tool_call_id: str = "", step: int = 0) -> str:
        handler = getattr(self, f"_do_{tool_name}", None)
        if handler is None:
            raise ValueError(f"未知工具：{tool_name}")
        self.current_step = int(step)
        return handler(arguments, tool_call_id or self.call_id_for(tool_name, arguments))

    # -- 工具实现 -----------------------------------------------------------
    def _do_list_files(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        path = arguments.get("path", ".")
        # 上限硬编码，避免模型用超大 max_entries 把遍历变成耗时攻击。
        max_entries = min(int(arguments.get("max_entries", 50)), 500)
        target = self.sandbox.resolve(path, mode="read")
        entries: list[str] = []
        if target.is_file():
            entries.append(self._rel(target))
        else:
            for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d not in {"__pycache__", ".git", ".pytest_cache", ".harness"})
                for name in sorted(filenames):
                    candidate = Path(dirpath) / name
                    if self._safe_read_path(candidate) is None:
                        continue
                    entries.append(self._rel(candidate))
                    if len(entries) >= max_entries:
                        return canonical_json({"entries": entries, "truncated": True})
        return canonical_json({"entries": entries, "truncated": False})

    def _do_search_code(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        pattern = arguments["pattern"]
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"正则表达式过长：{len(pattern)} 字符，上限 {MAX_PATTERN_CHARS}")
        if NESTED_QUANTIFIER.search(pattern):
            raise ValueError("正则表达式包含嵌套量词（可能触发灾难性回溯），请改写为等价非嵌套形式")
        path = arguments.get("path", ".")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正则表达式非法：{exc}") from exc
        target = self.sandbox.resolve(path, mode="read")
        matches: list[dict[str, Any]] = []
        files = [target] if target.is_file() else [
            Path(dirpath) / name
            for dirpath, dirnames, filenames in os.walk(target)
            for name in sorted(filenames)
            if not any(skip in Path(dirpath).parts for skip in ("__pycache__", ".git", ".pytest_cache", ".harness"))
        ]
        deadline = time.monotonic() + SEARCH_DEADLINE_SECONDS
        for index, file_path in enumerate(files[:MAX_SEARCH_FILES]):
            if index and time.monotonic() > deadline:
                return canonical_json({"matches": matches, "truncated": True, "reason": "deadline"})
            safe = self._safe_read_path(file_path)
            if safe is None:
                continue
            try:
                if safe.stat().st_size > MAX_READ_BYTES:
                    continue
                text = safe.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line[:MAX_SEARCH_LINE_CHARS]):
                    matches.append({"path": self._rel(safe), "line": number, "text": line[:200]})
                    if len(matches) >= 50:
                        return canonical_json({"matches": matches, "truncated": True})
        return canonical_json({"matches": matches, "truncated": len(files) > MAX_SEARCH_FILES})

    def _do_read_file(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        path = arguments["path"]
        start_line = max(1, int(arguments.get("start_line", 1)))
        max_lines = max(1, int(arguments.get("max_lines", 200)))
        target = self.sandbox.resolve(path, mode="read")
        if not target.is_file():
            raise ValueError(f"文件不存在：{path}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ValueError(
                f"文件过大：{size} 字节，上限 {MAX_READ_BYTES}；请改用 search_code 或分段读取更小的文件"
            )
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1: start_line - 1 + max_lines]
        return canonical_json(
            {
                "path": self._rel(target),
                "start_line": start_line,
                "total_lines": len(lines),
                "lines": selected,
            }
        )

    def _do_inspect_workspace(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        return canonical_json(
            {
                "fingerprint": self.sandbox.fingerprint(),
                "step": self.state.data.get("step", 0),
                "status": self.state.status,
                "plan": self.state.plan(),
                "budget": self.state.data["budget"],
                "writes": {
                    "total": len(self.state.writes()),
                    "open": len(self.state.open_writes()),
                },
                "tests": {
                    "total": len(self.state.tests()),
                    "open": len(self.state.open_tests()),
                },
                "evidence": [
                    {
                        "scope": item.get("scope"),
                        "seq": item.get("seq"),
                        "kind": item.get("kind"),
                        "passing": Evidence.from_dict(item).is_passing(),
                    }
                    for item in self.state.evidence()
                ],
                "pending": len(self.state.pending_items()),
                "approvals": self.state.approvals_valid_count(),
            }
        )

    def _do_write_file(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        path = arguments["path"]
        content = arguments["content"]
        target = self.sandbox.resolve(path, mode="write")
        signature = call_signature("write_file", {"path": path, "content": content})
        pre_sha = sha256_file(target)
        self.state.write_intent(
            signature,
            tool_call_id=tool_call_id,
            target=self._rel(target),
            pre_sha256=pre_sha,
            session=self.state.session or "",
            step=self.current_step,
        )
        self.audit.append(
            "write.intent",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="intent",
            rule="write_file",
            detail={"target": self._rel(target), "pre_sha256": pre_sha, "signature": signature},
        )
        self._atomic_write(target, content)
        post_sha = sha256_file(target)
        result = {"target": self._rel(target), "path": str(target), "sha256": post_sha, "bytes": len(content.encode("utf-8"))}
        self.state.write_result(signature, post_sha256=post_sha, result=result)
        self.state.mark_applied(signature, result)
        self.state.record_artifact(self._rel(target), {"sha256": post_sha})
        self.audit.append(
            "write.applied",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow",
            rule="write_file",
            detail={"target": self._rel(target), "pre_sha256": pre_sha, "post_sha256": post_sha},
        )
        return canonical_json({"ok": True, **result})

    def _do_apply_patch(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        path = arguments["path"]
        old_text = arguments["old_text"]
        new_text = arguments["new_text"]
        target = self.sandbox.resolve(path, mode="write")
        if not target.is_file():
            raise ValueError(f"目标文件不存在：{path}")
        original = target.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(old_text)
        if occurrences != 1:
            raise ValueError(f"old_text 必须唯一匹配，实际匹配 {occurrences} 次")
        signature = call_signature("apply_patch", {"path": path, "old_text": old_text, "new_text": new_text})
        pre_sha = sha256_file(target)
        self.state.write_intent(
            signature,
            tool_call_id=tool_call_id,
            target=self._rel(target),
            pre_sha256=pre_sha,
            session=self.state.session or "",
            step=self.current_step,
        )
        self.audit.append(
            "write.intent",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="intent",
            rule="apply_patch",
            detail={"target": self._rel(target), "pre_sha256": pre_sha, "signature": signature},
        )
        patched = original.replace(old_text, new_text, 1)
        self._atomic_write(target, patched)
        post_sha = sha256_file(target)
        result = {"target": self._rel(target), "path": str(target), "sha256": post_sha, "patch": True}
        self.state.write_result(signature, post_sha256=post_sha, result=result)
        self.state.mark_applied(signature, result)
        self.state.record_artifact(self._rel(target), {"sha256": post_sha})
        self.audit.append(
            "write.applied",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow",
            rule="apply_patch",
            detail={"target": self._rel(target), "pre_sha256": pre_sha, "post_sha256": post_sha},
        )
        return canonical_json({"ok": True, **result})

    def _do_run_test(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        scope = arguments["scope"]
        command = self.policy.suite_command(scope)
        signature = call_signature("run_test", {"scope": scope})
        self.state.test_intent(
            signature,
            tool_call_id=tool_call_id,
            scope=scope,
            command=command,
            session=self.state.session or "",
            step=self.current_step,
        )
        self.audit.append(
            "test.intent",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="intent",
            rule="run_test",
            detail={"scope": scope, "argv": command, "signature": signature},
        )
        result = self.sandbox.run(command, cwd=self.workspace, timeout=self.policy.command_timeout)
        # terminated（进程树被强杀）一律不算通过，与 Evidence.is_passing() 保持同一口径。
        ok = (
            result.returncode == 0
            and not result.timed_out
            and not result.cancelled
            and not result.terminated
        )
        payload = {
            "scope": scope,
            "argv": command,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "terminated": result.terminated,
            "duration": round(result.duration, 4),
            "log_path": result.log_path,
            "output_tail": result.output_tail,
        }
        self.state.test_result(signature, result=payload, pid=result.pid)
        fingerprint = self.sandbox.fingerprint()
        self.state.append_evidence(scope=scope, kind="test", payload=payload, fingerprint=fingerprint)
        self.audit.append(
            "test.result",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow" if ok else "error",
            rule="run_test",
            detail={**payload, "fingerprint": fingerprint},
        )
        return canonical_json({"ok": ok, **payload})

    def _do_record_evidence(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        scope = arguments["scope"]
        summary = arguments["summary"]
        kind = arguments.get("kind", "note")
        ok = bool(arguments.get("ok", True))
        fingerprint = self.sandbox.fingerprint()
        evidence = self.state.append_evidence(
            scope=scope,
            kind=kind,
            payload={"summary": summary, "ok": ok},
            fingerprint=fingerprint,
        )
        self.audit.append(
            "evidence.record",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow",
            rule="record_evidence",
            detail={
                "scope": scope,
                "kind": kind,
                "ok": ok,
                "seq": evidence.seq,
                "fingerprint": fingerprint,
                "step": self.current_step,
            },
        )
        return canonical_json({"ok": True, "evidence": evidence.to_dict()})

    def _do_update_plan(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        plan = self.state.set_plan(arguments["items"])
        self.audit.append(
            "plan.update",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow",
            rule="update_plan",
            detail={"revision": plan["revision"], "items": plan["items"], "step": self.current_step},
        )
        return canonical_json({"ok": True, "plan": plan})

    def _do_final_answer(self, arguments: dict[str, Any], tool_call_id: str) -> str:
        answer = str(arguments.get("answer", ""))
        self.audit.append(
            "final.answer",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor="model",
            decision="allow",
            rule="final_answer",
            detail={"answer": answer, "step": self.current_step},
        )
        return answer

    # -- 辅助 ---------------------------------------------------------------
    def _safe_read_path(self, path: Path) -> Path | None:
        """遍历发现的文件必须逐个复检：敏感名、后缀与链接逃逸一律跳过。"""
        try:
            return self.sandbox.resolve(self._rel(path), mode="read")
        except SandboxViolation:
            return None

    def _rel(self, path: Path | str) -> str:
        try:
            return os.path.relpath(str(path), str(self.workspace)).replace(os.sep, "/")
        except ValueError:
            return str(path)

    def _atomic_write(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp-{ulid()}")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


class _HarnessTool(Tool):
    output_type = "string"

    def __init__(self, runtime: ToolRuntime) -> None:
        self.runtime = runtime
        super().__init__()

    def _call(self, arguments: dict[str, Any]) -> str:
        return self.runtime.invoke(self.name, arguments)


class ListFilesTool(_HarnessTool):
    name = "list_files"
    description = "列出工作区内的文件（相对路径）。"
    inputs = {
        "path": {"type": "string", "description": "要列出的目录，默认工作区根目录", "nullable": True},
        "max_entries": {"type": "integer", "description": "最多返回条目数，默认 50", "nullable": True},
    }

    def forward(self, path: str = ".", max_entries: int = 50) -> str:
        return self._call({"path": path, "max_entries": max_entries})


class SearchCodeTool(_HarnessTool):
    name = "search_code"
    description = "在工作区文本文件中按正则搜索。"
    inputs = {
        "pattern": {"type": "string", "description": "正则表达式"},
        "path": {"type": "string", "description": "搜索起点，默认工作区根目录", "nullable": True},
    }

    def forward(self, pattern: str, path: str = ".") -> str:
        return self._call({"pattern": pattern, "path": path})


class ReadFileTool(_HarnessTool):
    name = "read_file"
    description = "读取工作区文件的行内容。"
    inputs = {
        "path": {"type": "string", "description": "相对工作区的文件路径"},
        "start_line": {"type": "integer", "description": "起始行号，从 1 开始", "nullable": True},
        "max_lines": {"type": "integer", "description": "最多读取行数，默认 200", "nullable": True},
    }

    def forward(self, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        return self._call({"path": path, "start_line": start_line, "max_lines": max_lines})


class InspectWorkspaceTool(_HarnessTool):
    name = "inspect_workspace"
    description = "查看工区指纹、预算、写入/测试意图、证据与审批摘要。"
    inputs: dict[str, Any] = {}

    def forward(self) -> str:
        return self._call({})


class WriteFileTool(_HarnessTool):
    name = "write_file"
    description = "写入文件（需要审批；原子替换）。"
    inputs = {
        "path": {"type": "string", "description": "相对工作区的文件路径"},
        "content": {"type": "string", "description": "完整文件内容"},
    }

    def forward(self, path: str, content: str) -> str:
        return self._call({"path": path, "content": content})


class ApplyPatchTool(_HarnessTool):
    name = "apply_patch"
    description = "替换文件中唯一匹配的一段文本（需要审批；原子替换）。"
    inputs = {
        "path": {"type": "string", "description": "相对工作区的文件路径"},
        "old_text": {"type": "string", "description": "要替换的原文片段，必须唯一匹配"},
        "new_text": {"type": "string", "description": "替换后的新文本"},
    }

    def forward(self, path: str, old_text: str, new_text: str) -> str:
        return self._call({"path": path, "old_text": old_text, "new_text": new_text})


class RunTestTool(_HarnessTool):
    name = "run_test"
    description = "运行配置内的测试套件（target/boundary/regression）。"
    inputs = {"scope": {"type": "string", "description": "测试范围：target/boundary/regression"}}

    def forward(self, scope: str) -> str:
        return self._call({"scope": scope})


class RecordEvidenceTool(_HarnessTool):
    name = "record_evidence"
    description = "记录一条带 scope 的辅助证据（kind 仅限 note/finding/check；验收证据只能由 run_test 产生）。"
    inputs = {
        "scope": {"type": "string", "description": "证据范围：target/boundary/regression"},
        "summary": {"type": "string", "description": "证据摘要"},
        "kind": {"type": "string", "description": "辅助证据类型：note/finding/check，默认 note", "nullable": True},
        "ok": {"type": "boolean", "description": "该证据是否通过，默认 true", "nullable": True},
    }

    def forward(self, scope: str, summary: str, kind: str = "note", ok: bool = True) -> str:
        return self._call({"scope": scope, "summary": summary, "kind": kind, "ok": ok})


class UpdatePlanTool(_HarnessTool):
    name = "update_plan"
    description = "提交或更新当前任务的执行计划（有序步骤列表），计划会写入状态与 Checkpoint。"
    inputs = {"items": {"type": "array", "description": "有序计划条目列表，每条为一个步骤"}}

    def forward(self, items: list) -> str:
        return self._call({"items": items})


class FinalAnswerTool(_HarnessTool):
    name = "final_answer"
    description = "提交最终答案；必须满足完成契约。"
    inputs = {"answer": {"type": "string", "description": "最终答案"}}

    def forward(self, answer: str) -> str:
        return self._call({"answer": answer})


def build_tools(runtime: ToolRuntime) -> list[Tool]:
    return [
        ListFilesTool(runtime),
        SearchCodeTool(runtime),
        ReadFileTool(runtime),
        InspectWorkspaceTool(runtime),
        WriteFileTool(runtime),
        ApplyPatchTool(runtime),
        RunTestTool(runtime),
        RecordEvidenceTool(runtime),
        UpdatePlanTool(runtime),
        FinalAnswerTool(runtime),
    ]


def _pre_read(request: ActionRequest, sandbox: Sandbox) -> None:
    sandbox.resolve(request.arguments.get("path", "."), mode="read")


def _pre_write(request: ActionRequest, sandbox: Sandbox) -> None:
    target = sandbox.resolve(request.arguments["path"], mode="write")
    request.context["target_path"] = str(target)
    request.context["target"] = os.path.relpath(str(target), str(sandbox.root)).replace(os.sep, "/")


def build_specs(policy: HarnessPolicy, sandbox: Sandbox) -> dict[str, ToolSpec]:
    allowed_scopes = set(policy.suites)

    def _pre_run_test(request: ActionRequest, box: Sandbox) -> None:
        scope = request.arguments["scope"]
        if scope not in allowed_scopes:
            raise ValueError(f"测试范围不在配置内：{scope}")
        box.check_command(policy.suite_command(scope))

    def _pre_evidence(request: ActionRequest, box: Sandbox) -> None:
        scope = request.arguments["scope"]
        if scope not in allowed_scopes | {"target", "boundary", "regression"}:
            raise ValueError(f"未知证据范围：{scope}")

    def _pre_plan(request: ActionRequest, box: Sandbox) -> None:
        items = request.arguments["items"]
        if not items:
            raise ValueError("计划不能为空")
        if len(items) > 12:
            raise ValueError("计划条目不能超过 12 条")
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("计划条目必须是非空字符串")
            if len(item) > 200:
                raise ValueError("单个计划条目不能超过 200 字符")

    return {
        "list_files": ToolSpec("list_files", RiskClass.READ, optional=("path", "max_entries"),
                               types={"path": str, "max_entries": int}, pre_check=_pre_read),
        "search_code": ToolSpec("search_code", RiskClass.READ, required=("pattern",), optional=("path",),
                                types={"pattern": str, "path": str}, pre_check=_pre_read),
        "read_file": ToolSpec("read_file", RiskClass.READ, required=("path",), optional=("start_line", "max_lines"),
                              types={"path": str, "start_line": int, "max_lines": int}, pre_check=_pre_read),
        "inspect_workspace": ToolSpec("inspect_workspace", RiskClass.READ),
        "write_file": ToolSpec("write_file", RiskClass.WRITE, required=("path", "content"),
                               types={"path": str, "content": str}, pre_check=_pre_write),
        "apply_patch": ToolSpec("apply_patch", RiskClass.WRITE, required=("path", "old_text", "new_text"),
                                types={"path": str, "old_text": str, "new_text": str}, pre_check=_pre_write),
        "run_test": ToolSpec("run_test", RiskClass.CONTROL, required=("scope",), types={"scope": str},
                             choices={"scope": allowed_scopes}, pre_check=_pre_run_test),
        "record_evidence": ToolSpec("record_evidence", RiskClass.READ, required=("scope", "summary"),
                                    optional=("kind", "ok"),
                                    types={"scope": str, "summary": str, "kind": str, "ok": bool},
                                    choices={"kind": set(AUXILIARY_EVIDENCE_KINDS)},
                                    pre_check=_pre_evidence),
        "update_plan": ToolSpec("update_plan", RiskClass.CONTROL, required=("items",), types={"items": list},
                                pre_check=_pre_plan),
        "final_answer": ToolSpec("final_answer", RiskClass.CONTROL, required=("answer",), types={"answer": str}),
    }


def ensure_text_tolerant(model: Any) -> Any:
    """让「模型只回纯文本」不再触发解析异常。

    smolagents 在 tool_calls 为空时会调用 model.parse_tool_calls，默认实现要求文本
    内含 JSON blob，否则抛 AgentParsingError 并在日志刷错误。真实模型（尤其带思考
    模式）常先输出一段纯文本，这不该中断 Run：Harness 在空批次分支给出明确反馈，
    完成态仍由 final_answer 契约把关。
    """
    if getattr(model, "_harness_text_tolerant", False):
        return model
    original = model.parse_tool_calls

    def parse_tool_calls(message: Any) -> Any:
        if getattr(message, "tool_calls", None):
            return original(message)
        return message

    model.parse_tool_calls = parse_tool_calls
    model._harness_text_tolerant = True
    return model


class GuardedToolCallingAgent(ToolCallingAgent):
    """唯一拦截缝：覆写 process_tool_calls，执行段继续复用 super()。"""

    def __init__(self, *, harness: "GuardedHarness", tools: list[Tool], model: Any, max_steps: int) -> None:
        self._harness = harness
        super().__init__(
            tools=tools,
            model=model,
            max_steps=max_steps,
            max_tool_threads=1,
            final_answer_checks=[harness._final_answer_check],
            step_callbacks=[harness._after_step],
            verbosity_level=LogLevel.ERROR,
        )

    def process_tool_calls(self, chat_message: Any, memory_step: ActionStep):
        harness = self._harness
        calls = list(chat_message.tool_calls or [])
        harness._pre_step()
        if not calls:
            text = str(getattr(chat_message, "content", "") or "").strip()
            memory_step.tool_calls = None
            memory_step.observations = (
                "未检测到工具调用：纯文本不会被接受为交付或完成。"
                "请继续调用工具；若任务确实已完成，请调用 final_answer(answer=...)。"
            )
            harness.audit.append(
                "model.text_only",
                harness.run_id,
                actor="model",
                decision="ignore",
                rule="no_tool_call",
                detail={"step": harness.current_step, "content": text[:500]},
            )
            return
        requests = [harness.build_request(call, harness.current_step) for call in calls]
        harness.runtime.register_calls(requests)
        verdicts = harness.gate.evaluate_batch(requests, harness.current_step)
        all_allow = all(verdict.decision == Decision.ALLOW for verdict in verdicts)
        # 只有整批都能执行时才扣写/测试预算、消耗审批；否则整批会在下面中止。
        harness.gate.commit_batch(
            requests, verdicts, harness.current_step, charge_side_effects=all_allow
        )

        for request, verdict in zip(requests, verdicts):
            if verdict.decision == Decision.REQUIRE_APPROVAL:
                memory_step.tool_calls = [ToolCall(name=c.function.name, arguments=c.function.arguments, id=c.id) for c in calls]
                message = verdict.error.message if verdict.error else "写操作需要人工审批"
                raise ApprovalRequired(message, tool_call_id=request.tool_call_id)
            if verdict.decision == Decision.DENY:
                memory_step.tool_calls = [ToolCall(name=c.function.name, arguments=c.function.arguments, id=c.id) for c in calls]
                harness.audit.append(
                    "step.error",
                    harness.run_id,
                    tool_call_id=request.tool_call_id,
                    actor="harness",
                    decision="deny",
                    rule=verdict.rule or (verdict.error.code if verdict.error else "denied"),
                    detail={
                        "tool": request.tool_name,
                        "step": harness.current_step,
                        "error": verdict.error.to_dict() if verdict.error else {"code": "denied"},
                    },
                )
                if verdict.error and verdict.error.code == "budget_exceeded":
                    raise BudgetExceeded(verdict.error.message)
                raise AgentToolCallError(
                    canonical_json(verdict.error.to_dict() if verdict.error else {"code": "denied"}),
                    self.logger,
                )

        try:
            yield from super().process_tool_calls(chat_message, memory_step)
        except Exception as exc:
            harness.audit.append(
                "step.error",
                harness.run_id,
                tool_call_id=requests[0].tool_call_id if requests else None,
                actor="harness",
                decision="error",
                rule=type(exc).__name__,
                detail={"message": str(exc), "step": harness.current_step},
            )
            raise


class GuardedHarness:
    def __init__(self, workspace: str | os.PathLike[str], model: Any, *, policy: HarnessPolicy | None = None,
                 sandbox: Sandbox | None = None, harness_dir: str | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy or HarnessPolicy()
        self.model = ensure_text_tolerant(model)
        base_dir = Path(harness_dir) if harness_dir else self.workspace / "artifacts" / ".harness"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.sandbox = sandbox or Sandbox(
            self.workspace,
            writable=self.policy.writable_zones,
            timeout=self.policy.command_timeout,
            log_dir=str(self.base_dir / "logs"),
        )
        self.state = StateStore(self.base_dir / "state.json", limits=self.policy.limits())
        self.audit = AuditLog(self.base_dir / "audit.jsonl")
        self._checkpoint_root = Checkpoint(self.base_dir / "checkpoints")
        self.runtime = ToolRuntime(self)
        self.checker = CompletionChecker(self.state, self.policy, self.sandbox.fingerprint)
        self.gate = Gate(build_specs(self.policy, self.sandbox), self.state, self.audit, self.policy, self.sandbox)

        self._agent: GuardedToolCallingAgent | None = None
        self._accepted = False
        self._cancel_requested = False
        self._pause_requested = False
        self._running = False
        self._run_started_at = 0.0
        self._last_stop: str | None = None

    @property
    def checkpoints(self) -> Checkpoint:
        """始终绑定当前 run，避免 reset/restore 后写错快照目录。"""
        return self._checkpoint_root.bind(self.state.run_id)

    # -- 基本属性 -----------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self.state.run_id or ""

    @property
    def current_step(self) -> int:
        return int(self.state.data.get("step", 0))

    def _ensure_agent(self) -> GuardedToolCallingAgent:
        if self._agent is None:
            self._agent = GuardedToolCallingAgent(
                harness=self,
                tools=build_tools(self.runtime),
                model=self.model,
                max_steps=self.policy.max_steps + 1,
            )
        return self._agent

    def build_request(self, chat_tool_call: Any, step: int) -> ActionRequest:
        name = chat_tool_call.function.name
        try:
            arguments = normalize_arguments(chat_tool_call.function.arguments)
        except ValueError:
            arguments = chat_tool_call.function.arguments if isinstance(chat_tool_call.function.arguments, dict) else {}
        return ActionRequest(
            tool_call_id=getattr(chat_tool_call, "id", "") or "",
            tool_name=name,
            arguments=arguments,
            step=step,
        )

    # -- 步骤边界 -----------------------------------------------------------
    def _pre_step(self) -> None:
        if self._cancel_requested:
            raise HarnessCancelled("用户已取消任务")
        if self._pause_requested:
            raise HarnessHalt("用户暂停", stop_reason=StopReason.PAUSED)
        if self._run_started_at:
            elapsed = time.monotonic() - self._run_started_at
            if elapsed > self.policy.max_seconds:
                self.state.charge("seconds", elapsed - self.state.budget_used("seconds"))
                raise BudgetExceeded(f"运行时长超过上限 {self.policy.max_seconds}s")
        if not self.state.budget_can_spend("steps"):
            raise HarnessHalt("达到最大步数", stop_reason=StopReason.MAX_STEPS)
        self.state.charge("steps", 1)
        self.state.data["step"] = int(self.state.data.get("step", 0)) + 1

    def _after_step(self, memory_step: Any, agent: Any = None) -> None:
        """每个安全步骤后保存快照；不抛异常，避免掩盖在途停机信号。"""
        try:
            self.state.data["fingerprint"] = self.sandbox.fingerprint()
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="after_step")
        except Exception as exc:
            # 持久化失败必须留痕，否则 C1 的快照承诺会静默失效。
            self.audit.append(
                "step.error",
                self.state.run_id,
                actor="harness",
                decision="error",
                rule=type(exc).__name__,
                detail={"phase": "checkpoint", "step": self.current_step, "message": str(exc)},
            )
        if sys.exc_info()[0] is None:
            if self._cancel_requested:
                raise HarnessCancelled("用户已取消任务")
            if self._pause_requested:
                raise HarnessHalt("用户暂停", stop_reason=StopReason.PAUSED)

    # -- 完成检查 -----------------------------------------------------------
    def _final_answer_check(self, final_answer: Any, memory: Any, agent: Any = None) -> bool:
        ok, message = self.checker.check()
        if not ok:
            raise AssertionError(f"完成契约未满足：{message}")
        self._accepted = True
        return True

    # -- 公共 API -----------------------------------------------------------
    def run(self, task: str, *, reset: bool = True) -> HarnessResult:
        self._accepted = False
        self._pause_requested = False
        self._cancel_requested = False
        self.sandbox.reset_cancel()
        self._last_stop = None
        self._run_started_at = time.monotonic()

        # IMP-04：进程退出未确认时禁止同工作区再次运行。
        if self.state.termination.get("unconfirmed"):
            raise UnknownResult(
                "上次运行有进程退出未确认，禁止在同一工作区再次运行",
                subject=Subject.PROCESS,
            )
        if reset:
            # 必须先检查再 reset：reset_state 会清空 writes/tests，否则未决意图被静默丢弃。
            self.state.assert_ready_for_session()

        self._running = True
        if reset:
            self.state.reset_state(task, self.policy.limits())
            self.state.begin_session()
            self.gate.clear_batch()
        else:
            self.state.set_status(RunStatus.RUNNING)
        self.state.data["fingerprint"] = self.sandbox.fingerprint()
        self.state.data["started_at"] = now_iso()
        self.state.save()
        self.audit.append(
            "run.start",
            self.state.run_id,
            actor="human",
            decision="start",
            rule="run",
            detail={"task": task, "session": self.state.session, "fingerprint": self.state.data["fingerprint"]},
        )

        try:
            return self._execute(task, reset=reset)
        finally:
            self._running = False
            # 会话结束后必须清空：否则非运行态的 cancel() 会拿旧 PID 杀无关进程。
            self.sandbox.last_pid = None

    def resume(self, *, decisions: dict[str, str] | None = None, task: str | None = None) -> HarnessResult:
        checkpoint = self.checkpoints.latest()
        if not checkpoint:
            raise HarnessError("没有可恢复的 Checkpoint")
        snapshot = checkpoint["snapshot"]
        self.state.restore(snapshot)
        current_fp = self.sandbox.fingerprint()
        current_shas = self._current_shas(self.state.open_writes())
        plan = plan_resume(
            snapshot,
            fingerprint=current_fp,
            current_shas=current_shas,
            step=int(snapshot.get("step", 0)),
        )
        self.audit.append(
            "resume.plan",
            self.state.run_id,
            actor="harness",
            decision=plan.action,
            rule=plan.reason,
            detail={"reason": plan.reason, "stop_reason": plan.stop_reason},
        )

        if plan.action == "refuse":
            self.state.set_status(
                RunStatus.WAITING_RECONCILE if plan.stop_reason in {"workspace_changed", "unknown_result"} else RunStatus.PAUSED,
                stop_reason=plan.stop_reason,
            )
            if plan.stop_reason == StopReason.WORKSPACE_CHANGED.value:
                self.state.set_blocked(plan.reason, stop_reason=plan.stop_reason)
            # 保留旧指纹基线，防止二次 resume 洗白
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=int(snapshot.get("step", 0)), boundary="resume-refused")
            raise self._exception_for(plan.stop_reason, plan.reason, plan.writes)

        if plan.action == "reconcile":
            if not decisions:
                self.state.set_status(RunStatus.WAITING_RECONCILE, stop_reason=StopReason.UNKNOWN_RESULT.value)
                self.state.save()
                self.checkpoints.save(self.state.snapshot(), step=int(snapshot.get("step", 0)), boundary="reconcile")
                raise UnknownResult(plan.reason, subject=Subject.WRITE)
            self.reconcile(decisions)
            plan = plan_resume(
                self.state.snapshot(),
                fingerprint=self.sandbox.fingerprint(),
                current_shas=self._current_shas(self.state.open_writes()),
                step=int(self.state.data.get("step", 0)),
            )
            if plan.action != "resume":
                self.state.set_status(self._status_for_stop(plan.stop_reason), stop_reason=plan.stop_reason)
                if plan.stop_reason == StopReason.WORKSPACE_CHANGED.value:
                    self.state.set_blocked(plan.reason, stop_reason=plan.stop_reason)
                self.state.save()
                self.checkpoints.save(self.state.snapshot(), step=int(self.state.data.get("step", 0)), boundary="reconcile")
                raise self._exception_for(plan.stop_reason, plan.reason, plan.writes)

        # 正常恢复：先清除上一轮遗留的停机信号，否则首个 _pre_step 会再次暂停/取消。
        self._pause_requested = False
        self._cancel_requested = False
        self.sandbox.reset_cancel()
        self._last_stop = None
        self.state.clear_blocked()
        self.state.set_status(RunStatus.RUNNING)
        self.state.data["fingerprint"] = current_fp
        self.state.save()
        self._accepted = False
        self._run_started_at = time.monotonic()
        self._running = True
        resume_task = task or snapshot.get("task", "")
        self.audit.append(
            "run.start",
            self.state.run_id,
            actor="human",
            decision="resume",
            rule="resume",
            detail={"task": resume_task, "session": self.state.session, "step": self.state.data.get("step")},
        )
        try:
            return self._execute(resume_task, reset=False)
        finally:
            self._running = False
            self.sandbox.last_pid = None

    def _execute(self, task: str, *, reset: bool) -> HarnessResult:
        agent = self._ensure_agent()
        try:
            output = agent.run(task, reset=reset, max_steps=self.policy.max_steps + 1)
        except HarnessHalt as halt:
            return self._on_halt(halt)
        except Exception as exc:
            self.state.set_status(RunStatus.FAILED, stop_reason=StopReason.MODEL_ERROR.value)
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="halt-model_error")
            self.audit.append(
                "run.halt",
                self.state.run_id,
                actor="harness",
                decision="error",
                rule=type(exc).__name__,
                detail={"message": str(exc)},
            )
            return self._result(output=None, message=f"模型执行出错：{exc}")

        if self._accepted:
            self.state.set_status(RunStatus.COMPLETED, stop_reason=StopReason.COMPLETED.value)
            stop_reason = StopReason.COMPLETED
        else:
            last = self._last_memory_error(agent)
            self.state.set_status(RunStatus.FAILED, stop_reason=StopReason.MAX_STEPS.value)
            stop_reason = StopReason.MAX_STEPS
            if not isinstance(last, AgentMaxStepsError) and self._last_stop is None:
                self._last_stop = StopReason.MAX_STEPS.value
        self.state.data["fingerprint"] = self.sandbox.fingerprint()
        self.state.save()
        self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="run_end")
        self.audit.append(
            "run.halt",
            self.state.run_id,
            actor="harness",
            decision="completed" if self._accepted else "max_steps",
            rule="run_end",
            detail={"stop_reason": self.state.data.get("stop_reason"), "step": self.current_step},
        )
        return self._result(output=output, message="完成" if self._accepted else "未满足完成契约")

    @staticmethod
    def _last_memory_error(agent: Any) -> Any:
        for step in reversed(agent.memory.steps):
            error = getattr(step, "error", None)
            if error is not None:
                return error
        return None

    @staticmethod
    def _status_for_stop(reason: Any) -> RunStatus:
        """停机原因 → 运行态的唯一映射，供 _on_halt 与 resume 共用。"""
        key = reason.value if isinstance(reason, StopReason) else str(reason)
        mapping = {
            StopReason.APPROVAL_REQUIRED.value: RunStatus.WAITING_APPROVAL,
            StopReason.WORKSPACE_CHANGED.value: RunStatus.WAITING_RECONCILE,
            StopReason.UNKNOWN_RESULT.value: RunStatus.WAITING_RECONCILE,
            StopReason.PAUSED.value: RunStatus.PAUSED,
            StopReason.CANCELLED.value: RunStatus.CANCELLED,
            StopReason.BUDGET_EXCEEDED.value: RunStatus.FAILED,
            StopReason.POLICY_DENIED.value: RunStatus.FAILED,
        }
        return mapping.get(key, RunStatus.FAILED)

    def _on_halt(self, halt: HarnessHalt) -> HarnessResult:
        reason = halt.stop_reason
        status = self._status_for_stop(reason)
        if reason == StopReason.CANCELLED:
            pid = self.sandbox.recent_pid
            if pid and is_process_alive(pid):
                self.state.set_termination(True, pid)
            else:
                self.state.set_termination(False, None)
        if reason == StopReason.WORKSPACE_CHANGED:
            self.state.set_blocked(halt.message, stop_reason=reason.value)
        self.state.set_status(status, stop_reason=reason.value)
        self.state.save()
        boundary = f"halt-{reason.value}"
        self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary=boundary)
        self.audit.append(
            "run.halt",
            self.state.run_id,
            actor="harness",
            decision=reason.value,
            rule=type(halt).__name__,
            detail={"message": halt.message, "step": self.current_step},
        )
        return self._result(output=None, message=halt.message)

    def _exception_for(self, stop_reason: str | None, message: str, writes: list[dict[str, Any]]) -> HarnessHalt:
        if stop_reason == StopReason.WORKSPACE_CHANGED.value:
            return WorkspaceChanged(message)
        if stop_reason == StopReason.UNKNOWN_RESULT.value:
            return UnknownResult(message, subject=Subject.TEST_RUN if not writes else Subject.WRITE)
        if stop_reason == StopReason.BUDGET_EXCEEDED.value:
            return BudgetExceeded(message)
        if stop_reason == StopReason.APPROVAL_REQUIRED.value:
            return ApprovalRequired(message)
        if stop_reason == StopReason.CANCELLED.value:
            return HarnessCancelled(message)
        if stop_reason == StopReason.COMPLETED.value:
            return PolicyDenied("任务已完成，不能继续")
        return PolicyDenied(message)

    # -- 审批 ---------------------------------------------------------------
    def approve(self, tool_call_id: str, *, actor: str = "human", reason: str = "",
                ttl_steps: int | None = None, force: bool = False) -> Approval:
        pending = self.state.get_pending(tool_call_id)
        if not pending:
            raise HarnessError(f"没有待审批的调用：{tool_call_id}")
        fingerprint = self.sandbox.fingerprint()
        requested_fp = pending.get("fingerprint_at_request", "")
        if fingerprint != requested_fp:
            self.audit.append(
                "approval.refused",
                self.state.run_id,
                tool_call_id=tool_call_id,
                actor=actor,
                decision="refuse",
                rule="fingerprint_changed",
                detail={"requested": requested_fp, "current": fingerprint},
            )
            if not force:
                raise WorkspaceChanged("审批请求后工作区已变化，旧批准不可用")
        approval = Approval(
            tool_call_id=tool_call_id,
            signature=pending.get("signature", ""),
            fingerprint=fingerprint if force else requested_fp,
            actor=actor,
            reason=reason,
            ttl_steps=int(ttl_steps if ttl_steps is not None else self.policy.approval_ttl_steps),
            granted_step=int(self.state.data.get("step", 0)),
            force=force,
        )
        self.state.record_approval(approval)
        if force:
            self.state.clear_blocked()
            self.state.data["fingerprint"] = fingerprint
        self.state.save()
        self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="approval")
        self.audit.append(
            "approval.granted",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor=actor,
            decision="allow",
            rule="force" if force else "approved",
            detail={"signature": approval.signature, "fingerprint": approval.fingerprint, "ttl_steps": approval.ttl_steps, "reason": reason},
        )
        return approval

    def reject(self, tool_call_id: str, *, actor: str = "human", reason: str = "") -> Approval:
        pending = self.state.get_pending(tool_call_id)
        if not pending:
            raise HarnessError(f"没有待审批的调用：{tool_call_id}")
        approval = Approval(
            tool_call_id=tool_call_id,
            signature=pending.get("signature", ""),
            fingerprint=pending.get("fingerprint_at_request", ""),
            actor=actor,
            reason=reason,
            ttl_steps=int(self.policy.approval_ttl_steps),
            granted_step=int(self.state.data.get("step", 0)),
            rejected=True,
        )
        self.state.record_approval(approval)
        self.state.pop_pending(tool_call_id)
        # 尚未处理的 pending 仍需要阻断；全部处理完才解除，否则 resume 会被旧 blocked 永久拒绝。
        if not self.state.pending_items():
            self.state.clear_blocked()
        self.state.set_status(RunStatus.WAITING_APPROVAL, stop_reason=StopReason.APPROVAL_REQUIRED.value)
        self.state.save()
        self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="approval-rejected")
        self.audit.append(
            "approval.rejected",
            self.state.run_id,
            tool_call_id=tool_call_id,
            actor=actor,
            decision="deny",
            rule="rejected",
            detail={"signature": approval.signature, "reason": reason},
        )
        return approval

    # -- 对账 ---------------------------------------------------------------
    def reconcile(self, decisions: dict[str, str] | None = None, *, actor: str = "human", note: str = "") -> dict[str, Any]:
        decisions = decisions or {}
        open_writes = self.state.open_writes()
        verdicts: dict[str, str] = {}
        resolved: list[str] = []
        unresolved: list[str] = []

        for record in open_writes:
            signature = record.get("signature", "")
            target = record.get("target", "")
            current_sha: str | None = None
            if target:
                try:
                    current_sha = sha256_file(self.sandbox.resolve(target, mode="read"))
                except SandboxViolation:
                    current_sha = None
            raw = decisions.get(signature)
            if raw is None:
                call_id = record.get("tool_call_id") or ""
                raw = decisions.get(call_id) if call_id else None
            if raw is None:
                verdict = classify_write(record, current_sha)
            else:
                try:
                    verdict = ReconcileVerdict(raw)
                except ValueError:
                    verdict = ReconcileVerdict.UNCERTAIN

            pre = record.get("pre_sha256")
            post = record.get("post_sha256")
            # IMP-06：人工结论必须与现场一致，否则降级为 UNCERTAIN。
            if verdict == ReconcileVerdict.EXECUTED:
                if post is None and current_sha == pre:
                    verdict = ReconcileVerdict.UNCERTAIN
                elif post is not None and current_sha != post:
                    verdict = ReconcileVerdict.UNCERTAIN
            elif verdict == ReconcileVerdict.NOT_EXECUTED and current_sha != pre:
                verdict = ReconcileVerdict.UNCERTAIN

            verdicts[signature] = verdict.value
            self.state.set_reconcile(signature, verdict=verdict, actor=actor, note=note)
            if verdict == ReconcileVerdict.NOT_EXECUTED:
                self.state.set_write_state(signature, WriteState.NOT_EXECUTED, post_sha256=None)
                self.state.release(signature)
                resolved.append(signature)
            elif verdict == ReconcileVerdict.EXECUTED:
                self.state.set_write_state(signature, WriteState.DONE, post_sha256=current_sha)
                self.state.mark_applied(signature, record.get("result") or {"reconciled": True, "sha256": current_sha})
                resolved.append(signature)
            else:
                unresolved.append(signature)

            self.audit.append(
                "write.reconciled",
                self.state.run_id,
                tool_call_id=record.get("tool_call_id"),
                actor=actor,
                decision=verdict.value,
                rule="reconcile",
                detail={"signature": signature, "current_sha256": current_sha, "pre_sha256": pre, "post_sha256": post, "note": note},
            )

        if not unresolved:
            self.state.clear_blocked()
            self.state.set_status(RunStatus.RUNNING)
        else:
            self.state.set_status(RunStatus.WAITING_RECONCILE, stop_reason=StopReason.UNKNOWN_RESULT.value)
        self.state.save()
        self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="reconcile")
        return {
            "verdicts": verdicts,
            "resolved": resolved,
            "unresolved": unresolved,
            "actor": actor,
            "note": note,
        }

    def abandon_tests(self, *, actor: str = "human", note: str = "") -> dict[str, Any]:
        """人工销记无从判定的测试意图，解除 resume 的 unknown_result 阻断。"""
        changed = self.state.fail_test_intents(session=self.state.session)
        for signature in changed:
            record = self.state.tests().get(signature) or {}
            self.audit.append(
                "test.abandoned",
                self.state.run_id,
                tool_call_id=record.get("tool_call_id") or None,
                actor=actor,
                decision="uncertain",
                rule="abandon_tests",
                detail={"signature": signature, "scope": record.get("scope", ""), "note": note},
            )
        if changed:
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="abandon-tests")
        return {"abandoned": changed, "actor": actor, "note": note}

    # -- 暂停 / 取消 ---------------------------------------------------------
    def pause(self) -> None:
        self._pause_requested = True
        if not self._running:
            self.state.set_status(RunStatus.PAUSED, stop_reason=StopReason.PAUSED.value)
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="pause")
            self.audit.append(
                "run.halt",
                self.state.run_id,
                actor="human",
                decision="paused",
                rule="pause",
                detail={"step": self.current_step},
            )

    def cancel(self) -> None:
        self._cancel_requested = True
        self.sandbox.request_cancel()
        self.audit.append(
            "run.cancel_requested",
            self.state.run_id,
            actor="human",
            decision="cancel",
            rule="cancel",
            detail={"step": self.current_step},
        )
        if not self._running:
            pid = self.sandbox.last_pid
            if pid and is_process_alive(pid):
                kill_tree(pid)
            confirmed = not (pid and is_process_alive(pid))
            self.state.set_termination(not confirmed, pid if not confirmed else None)
            self.state.set_status(RunStatus.CANCELLED, stop_reason=StopReason.CANCELLED.value)
            self.state.save()
            self.checkpoints.save(self.state.snapshot(), step=self.current_step, boundary="halt-cancelled")

    def reset_cancel(self) -> None:
        if self.state.status == RunStatus.CANCELLED.value:
            raise PolicyDenied("已取消的任务不可复活；请开启全新 run")
        self._cancel_requested = False
        self._pause_requested = False
        self.sandbox.reset_cancel()

    # -- 辅助 ---------------------------------------------------------------
    def _current_shas(self, records: list[dict[str, Any]]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for record in records:
            target = record.get("target", "")
            if not target:
                continue
            try:
                result[target] = sha256_file(self.sandbox.resolve(target, mode="read"))
            except SandboxViolation:
                result[target] = None
        return result

    def _result(self, *, output: Any, message: str) -> HarnessResult:
        return HarnessResult(
            run_id=self.state.run_id or "",
            status=self.state.status,
            stop_reason=self.state.data.get("stop_reason"),
            output=output,
            step=self.current_step,
            tool_calls=int(self.state.budget_used("tool_calls")),
            writes=int(self.state.budget_used("writes")),
            test_runs=int(self.state.budget_used("test_runs")),
            fingerprint=self.state.data.get("fingerprint"),
            pending=self.state.pending_items(),
            blocked=self.state.blocked,
            message=message,
        )

    def result_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.state.run_id,
            "status": self.state.status,
            "stop_reason": self.state.data.get("stop_reason"),
            "step": self.current_step,
            "budget": self.state.data["budget"],
            "evidence": self.state.evidence(),
            "blocked": self.state.blocked,
        }
