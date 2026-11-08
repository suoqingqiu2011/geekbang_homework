"""契约层：公共枚举、异常、数据对象与纯函数。

本模块只依赖标准库，供其余模块导入而不引入循环。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class RiskClass(str, Enum):
    READ = "read"
    WRITE = "write"
    CONTROL = "control"


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_RECONCILE = "waiting_reconcile"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(str, Enum):
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    MAX_STEPS = "max_steps"
    BUDGET_EXCEEDED = "budget_exceeded"
    APPROVAL_REQUIRED = "approval_required"
    WORKSPACE_CHANGED = "workspace_changed"
    UNKNOWN_RESULT = "unknown_result"
    POLICY_DENIED = "policy_denied"
    MODEL_ERROR = "model_error"

    @property
    def resumable(self) -> bool:
        return self in {
            StopReason.PAUSED,
            StopReason.APPROVAL_REQUIRED,
            StopReason.WORKSPACE_CHANGED,
            StopReason.UNKNOWN_RESULT,
        }


class WriteState(str, Enum):
    INTENT = "intent"
    DONE = "done"
    NOT_EXECUTED = "not_executed"
    UNCERTAIN = "uncertain"


class ReconcileVerdict(str, Enum):
    EXECUTED = "executed"
    NOT_EXECUTED = "not_executed"
    UNCERTAIN = "uncertain"


class Subject(str, Enum):
    WRITE = "write"
    TEST_RUN = "test_run"
    PROCESS = "process"


# ---------------------------------------------------------------------------
# 异常契约
# ---------------------------------------------------------------------------


class HarnessError(Exception):
    """Harness 自身的错误基类。"""


class HarnessHalt(HarnessError):
    """穿透 Agent Loop 的停机信号（非 AgentError，不会被内核吞掉）。"""

    stop_reason: StopReason = StopReason.POLICY_DENIED

    def __init__(self, message: str = "", *, stop_reason: StopReason | None = None):
        super().__init__(message)
        if stop_reason is not None:
            self.stop_reason = stop_reason
        self.message = message


class ApprovalRequired(HarnessHalt):
    stop_reason = StopReason.APPROVAL_REQUIRED

    def __init__(self, message: str = "", *, tool_call_id: str = ""):
        super().__init__(message)
        self.tool_call_id = tool_call_id


class BudgetExceeded(HarnessHalt):
    stop_reason = StopReason.BUDGET_EXCEEDED


class WorkspaceChanged(HarnessHalt):
    stop_reason = StopReason.WORKSPACE_CHANGED


class UnknownResult(HarnessHalt):
    stop_reason = StopReason.UNKNOWN_RESULT

    def __init__(self, message: str = "", *, subject: Subject = Subject.WRITE):
        super().__init__(message)
        self.subject = subject


class PolicyDenied(HarnessHalt):
    stop_reason = StopReason.POLICY_DENIED


class HarnessCancelled(HarnessHalt):
    stop_reason = StopReason.CANCELLED


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------


@dataclass
class StructuredError:
    code: str
    message: str
    tool: str = ""
    tool_call_id: str = ""
    rule: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "tool": self.tool,
            "tool_call_id": self.tool_call_id,
            "rule": self.rule,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        return canonical_json(self.to_dict())


@dataclass
class ActionRequest:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    step: int = 0
    signature: str = ""
    risk: RiskClass = RiskClass.READ
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateVerdict:
    request: ActionRequest
    decision: Decision
    rule: str = ""
    error: StructuredError | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Approval:
    tool_call_id: str
    signature: str
    fingerprint: str
    actor: str = "human"
    reason: str = ""
    ttl_steps: int = 3
    granted_step: int = 0
    consumed: bool = False
    rejected: bool = False
    force: bool = False
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "signature": self.signature,
            "fingerprint": self.fingerprint,
            "actor": self.actor,
            "reason": self.reason,
            "ttl_steps": self.ttl_steps,
            "granted_step": self.granted_step,
            "consumed": self.consumed,
            "rejected": self.rejected,
            "force": self.force,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Approval":
        return cls(
            tool_call_id=data.get("tool_call_id", ""),
            signature=data.get("signature", ""),
            fingerprint=data.get("fingerprint", ""),
            actor=data.get("actor", "human"),
            reason=data.get("reason", ""),
            ttl_steps=int(data.get("ttl_steps", 3)),
            granted_step=int(data.get("granted_step", 0)),
            consumed=bool(data.get("consumed", False)),
            rejected=bool(data.get("rejected", False)),
            force=bool(data.get("force", False)),
            at=data.get("at", ""),
        )


ACCEPTANCE_EVIDENCE_KINDS = frozenset({"test", "test_run"})
AUXILIARY_EVIDENCE_KINDS = frozenset({"note", "finding", "check"})


@dataclass
class Evidence:
    id: str
    scope: str
    seq: int
    kind: str
    payload: dict[str, Any]
    fingerprint: str
    artifact_version: str
    at: str

    def is_passing(self) -> bool:
        # IMP-07：取消/超时/进程树被强杀都不得归因为断言失败，也不得算通过。
        if self.is_acceptance():
            if self.payload.get("cancelled") or self.payload.get("timed_out") or self.payload.get("terminated"):
                return False
            try:
                return int(self.payload.get("returncode", 1)) == 0
            except (TypeError, ValueError):
                return False
        return bool(self.payload.get("ok", False))

    def is_acceptance(self) -> bool:
        """只有真实测试运行才能作为完成验收证据；自报 note 不是交付物。"""
        return self.kind in ACCEPTANCE_EVIDENCE_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "seq": self.seq,
            "kind": self.kind,
            "payload": self.payload,
            "fingerprint": self.fingerprint,
            "artifact_version": self.artifact_version,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            id=data.get("id", ""),
            scope=data.get("scope", ""),
            seq=int(data.get("seq", 0)),
            kind=data.get("kind", ""),
            payload=dict(data.get("payload", {})),
            fingerprint=data.get("fingerprint", ""),
            artifact_version=data.get("artifact_version", ""),
            at=data.get("at", ""),
        )


@dataclass
class WriteRecord:
    signature: str
    tool_call_id: str
    target: str
    state: str = WriteState.INTENT.value
    pre_sha256: str | None = None
    post_sha256: str | None = None
    result: dict[str, Any] | None = None
    session: str = ""
    step: int = 0
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "tool_call_id": self.tool_call_id,
            "target": self.target,
            "state": self.state,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "result": self.result,
            "session": self.session,
            "step": self.step,
            "at": self.at,
        }


@dataclass
class TestRunRecord:
    signature: str
    tool_call_id: str
    scope: str
    command: list[str]
    state: str = WriteState.INTENT.value
    result: dict[str, Any] | None = None
    session: str = ""
    step: int = 0
    pid: int | None = None
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "tool_call_id": self.tool_call_id,
            "scope": self.scope,
            "command": self.command,
            "state": self.state,
            "result": self.result,
            "session": self.session,
            "step": self.step,
            "pid": self.pid,
            "at": self.at,
        }


@dataclass
class BudgetLimits:
    max_steps: int = 12
    max_tool_calls: int = 40
    max_writes: int = 10
    max_test_runs: int = 8
    max_seconds: float = 180.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_writes": self.max_writes,
            "max_test_runs": self.max_test_runs,
            "max_seconds": self.max_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetLimits":
        return cls(
            max_steps=int(data.get("max_steps", 12)),
            max_tool_calls=int(data.get("max_tool_calls", 40)),
            max_writes=int(data.get("max_writes", 10)),
            max_test_runs=int(data.get("max_test_runs", 8)),
            max_seconds=float(data.get("max_seconds", 180.0)),
        )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

_ULID_COUNTER = 0


def ulid() -> str:
    """生成时间有序的短标识（非严格 ULID，但单调且唯一）。"""
    global _ULID_COUNTER
    _ULID_COUNTER = (_ULID_COUNTER + 1) % 10000
    return f"{int(time.time() * 1000):013d}{_ULID_COUNTER:04d}{secrets.token_hex(4)}"


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str] | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with open(p, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_arguments(arguments: Any) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ValueError(f"工具参数必须是对象，实际为 {type(arguments).__name__}")
    return json.loads(canonical_json(arguments))


def call_signature(tool_name: str, arguments: Any) -> str:
    body = {"tool": tool_name, "arguments": normalize_arguments(arguments)}
    return "sig:" + sha256_text(canonical_json(body))


def _coerce(value: Any, enum_type: type[Enum]) -> Any:
    if isinstance(value, enum_type):
        return value
    return enum_type(value)


def error_payload(error: StructuredError) -> dict[str, Any]:
    return {"ok": False, "error": error.to_dict()}
