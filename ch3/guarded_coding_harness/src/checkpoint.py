"""存档与恢复：三态分类、恢复优先级、原子快照。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import (
    ReconcileVerdict,
    StopReason,
    canonical_json,
    now_iso,
    sha256_text,
    ulid,
)


@dataclass
class ResumePlan:
    action: str
    reason: str
    stop_reason: str | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    fingerprint_changed: bool = False


def classify_write(record: dict[str, Any], current_sha: str | None) -> ReconcileVerdict:
    """EXECUTED 只认 DONE 或 post_sha；NOT_EXECUTED 只认 current == pre_sha。"""
    if record.get("state") == "done":
        return ReconcileVerdict.EXECUTED
    post = record.get("post_sha256")
    if post is not None and current_sha == post:
        return ReconcileVerdict.EXECUTED
    pre = record.get("pre_sha256")
    # IMP-02：只有现场与执行前完全一致（含新文件仍不存在）才可判为未执行。
    if current_sha == pre:
        return ReconcileVerdict.NOT_EXECUTED
    return ReconcileVerdict.UNCERTAIN


def classify_writes(records: Any, current_shas: dict[str, str | None]) -> dict[str, ReconcileVerdict]:
    if isinstance(records, dict):
        items = list(records.values())
    else:
        items = list(records)
    result: dict[str, ReconcileVerdict] = {}
    for record in items:
        target = record.get("target", "")
        result[record.get("signature", target)] = classify_write(record, current_shas.get(target))
    return result


def classify_batch(verdicts: Any) -> ReconcileVerdict:
    values = list(verdicts.values()) if isinstance(verdicts, dict) else list(verdicts)
    if not values:
        return ReconcileVerdict.NOT_EXECUTED
    if all(v == ReconcileVerdict.EXECUTED for v in values):
        return ReconcileVerdict.EXECUTED
    if all(v == ReconcileVerdict.NOT_EXECUTED for v in values):
        return ReconcileVerdict.NOT_EXECUTED
    return ReconcileVerdict.UNCERTAIN


def _find_valid_approval(snapshot: dict[str, Any], signature: str, fingerprint: str, step: int) -> dict[str, Any] | None:
    for item in (snapshot.get("approvals") or {}).values():
        if item.get("rejected") or item.get("consumed"):
            continue
        if signature and item.get("signature") != signature:
            continue
        if item.get("fingerprint") != fingerprint:
            continue
        if step - int(item.get("granted_step", 0)) > int(item.get("ttl_steps", 3)):
            continue
        return item
    return None


def _exhausted(snapshot: dict[str, Any]) -> bool:
    budget = snapshot.get("budget") or {}
    limits = budget.get("limits") or {}
    used = budget.get("used") or {}
    checks = (
        ("steps", "max_steps"),
        ("tool_calls", "max_tool_calls"),
        ("writes", "max_writes"),
        ("test_runs", "max_test_runs"),
        ("seconds", "max_seconds"),
    )
    for used_key, limit_key in checks:
        if float(used.get(used_key, 0)) >= float(limits.get(limit_key, 0)):
            return True
    return False


def plan_resume(
    snapshot: dict[str, Any],
    *,
    fingerprint: str,
    current_shas: dict[str, str | None] | None = None,
    step: int | None = None,
) -> ResumePlan:
    """按 SPEC 9.3 的固定优先级判定恢复动作。"""
    current_shas = current_shas or {}
    step = int(snapshot.get("step", 0)) if step is None else int(step)
    open_writes = [
        record for record in (snapshot.get("writes") or {}).values()
        if record.get("state") in {"intent", "uncertain"}
    ]
    open_tests = [
        record for record in (snapshot.get("tests") or {}).values()
        if record.get("state") == "intent"
    ]

    status = snapshot.get("status")
    if status in {"completed", "cancelled"}:
        return ResumePlan(
            "refuse",
            f"任务已是终态：{status}",
            StopReason.COMPLETED.value if status == "completed" else StopReason.CANCELLED.value,
        )

    termination = snapshot.get("termination") or {}
    if termination.get("unconfirmed"):
        return ResumePlan(
            "refuse",
            "上次进程退出未确认，禁止在同一工作区继续",
            StopReason.UNKNOWN_RESULT.value,
        )

    if open_tests:
        return ResumePlan(
            "refuse",
            "存在未销记的测试意图，结果不明",
            StopReason.UNKNOWN_RESULT.value,
            tests=open_tests,
        )

    stored_fp = snapshot.get("fingerprint")
    if stored_fp and stored_fp != fingerprint:
        return ResumePlan(
            "refuse",
            "工作区指纹与存档不一致，旧证据与旧审批失效",
            StopReason.WORKSPACE_CHANGED.value,
            writes=open_writes,
            tests=open_tests,
            fingerprint_changed=True,
        )

    blocked = snapshot.get("blocked")
    if blocked:
        allowed = False
        if blocked.get("stop_reason") == StopReason.APPROVAL_REQUIRED.value:
            allowed = _find_valid_approval(
                snapshot, blocked.get("signature", ""), fingerprint, step
            ) is not None
        if not allowed:
            return ResumePlan(
                "refuse",
                f"存在阻断标记：{blocked.get('reason', '')}",
                blocked.get("stop_reason") or StopReason.POLICY_DENIED.value,
                writes=open_writes,
                tests=open_tests,
            )

    if open_writes:
        verdicts = classify_writes(open_writes, current_shas)
        return ResumePlan(
            "reconcile",
            "存在结果不明的写入，需人工三态对账",
            StopReason.UNKNOWN_RESULT.value,
            writes=open_writes,
            tests=open_tests,
        )

    if _exhausted(snapshot):
        return ResumePlan(
            "refuse",
            "预算已耗尽",
            StopReason.BUDGET_EXCEEDED.value,
            writes=open_writes,
            tests=open_tests,
        )

    return ResumePlan("resume", "可恢复", None, writes=[], tests=open_tests)


class Checkpoint:
    def __init__(self, directory: str | os.PathLike[str], *, run_id: str | None = None) -> None:
        self.root = Path(directory)
        self.run_id = run_id
        self.directory = self.root / run_id if run_id else self.root
        self.directory.mkdir(parents=True, exist_ok=True)

    def bind(self, run_id: str | None) -> "Checkpoint":
        """按 run 隔离快照目录，避免不同 run 互相读到对方的最新快照。"""
        if run_id == self.run_id:
            return self
        return Checkpoint(self.root, run_id=run_id)

    def _path_for(self, step: int, boundary: str) -> Path:
        safe = boundary.replace(os.sep, "_").replace("/", "_")
        return self.directory / f"step-{int(step):06d}-{safe}.json"

    def save(self, snapshot: dict[str, Any], *, step: int, boundary: str) -> Path:
        payload = {
            "version": 1,
            "saved_at": now_iso(),
            "boundary": boundary,
            "step": int(step),
            "checksum": sha256_text(canonical_json(snapshot)),
            "snapshot": snapshot,
        }
        path = self._path_for(step, boundary)
        tmp = path.with_name(path.name + f".tmp-{ulid()}")
        tmp.write_text(canonical_json(payload), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def list_snapshots(self) -> list[Path]:
        return sorted(self.directory.glob("step-*.json"))

    def latest(self) -> dict[str, Any] | None:
        paths = self.list_snapshots()
        if not paths:
            return None

        def sort_key(path: Path) -> tuple[int, int]:
            try:
                step = int(path.name.split("-")[1])
            except (IndexError, ValueError):
                step = 0
            return step, path.stat().st_mtime_ns

        return self.load(max(paths, key=sort_key))

    def load(self, path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
        target = Path(path) if path else None
        if target is None:
            latest = self.latest()
            return latest
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        snapshot = payload.get("snapshot")
        checksum = payload.get("checksum")
        if isinstance(snapshot, dict) and checksum and checksum != sha256_text(canonical_json(snapshot)):
            return None
        return payload
