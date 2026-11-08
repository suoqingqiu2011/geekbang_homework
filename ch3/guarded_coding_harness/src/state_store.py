"""业务状态：预算、台账、待审批、写入/测试意图、证据、阻断与终止。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Iterable

from contracts import (
    Approval,
    BudgetLimits,
    Evidence,
    ReconcileVerdict,
    RunStatus,
    Subject,
    UnknownResult,
    WriteState,
    canonical_json,
    now_iso,
    sha256_text,
    ulid,
)

OPEN_STATES = {WriteState.INTENT.value, WriteState.UNCERTAIN.value}


def _blank(limits: BudgetLimits) -> dict[str, Any]:
    return {
        "version": 1,
        "run_id": None,
        "task": "",
        "plan": {"items": [], "revision": 0, "updated_at": None},
        "status": RunStatus.CREATED.value,
        "stop_reason": None,
        "session": None,
        "step": 0,
        "step_offset": 0,
        "fingerprint": None,
        "started_at": now_iso(),
        "budget": {
            "limits": limits.to_dict(),
            "used": {"steps": 0, "tool_calls": 0, "writes": 0, "test_runs": 0, "seconds": 0.0},
        },
        "ledger": {},
        "pending": {},
        "approvals": {},
        "writes": {},
        "tests": {},
        "evidence": [],
        "artifacts": {},
        "reconcile": {},
        "blocked": None,
        "termination": {"unconfirmed": False, "pid": None, "at": None},
    }


def _merge_defaults(data: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for key, value in defaults.items():
        if key not in data:
            data[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(data.get(key), dict):
            _merge_defaults(data[key], value)
    return data


class StateStore:
    def __init__(self, path: str | os.PathLike[str], limits: BudgetLimits | None = None) -> None:
        self.path = Path(path)
        self.data = _blank(limits or BudgetLimits())
        self.load()

    # -- 持久化 -------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = {}
            if isinstance(loaded, dict):
                self.data = _merge_defaults(loaded, _blank(BudgetLimits.from_dict(
                    loaded.get("budget", {}).get("limits", {})
                )))
        return self.data

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.data = copy.deepcopy(snapshot)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp-{ulid()}")
        tmp.write_text(canonical_json(self.data), encoding="utf-8")
        os.replace(tmp, self.path)

    def digest(self) -> str:
        return sha256_text(canonical_json(self.data))

    # -- 基本字段 -----------------------------------------------------------
    @property
    def run_id(self) -> str | None:
        return self.data.get("run_id")

    @property
    def status(self) -> str:
        return self.data.get("status", RunStatus.CREATED.value)

    def set_status(self, status: RunStatus | str, *, stop_reason: str | None = None) -> None:
        self.data["status"] = status.value if isinstance(status, RunStatus) else str(status)
        if stop_reason is not None:
            self.data["stop_reason"] = stop_reason
        elif status in (RunStatus.RUNNING, RunStatus.CREATED):
            self.data["stop_reason"] = None

    def reset_state(self, task: str, limits: BudgetLimits) -> None:
        self.data = _blank(limits)
        self.data["run_id"] = ulid()
        self.data["task"] = task

    def assert_ready_for_session(self) -> None:
        open_writes = self.open_writes()
        open_tests = self.open_tests()
        if open_writes:
            raise UnknownResult(
                "存在未决写入意图，禁止开启新 session；请先对账",
                subject=Subject.WRITE,
            )
        if open_tests:
            raise UnknownResult(
                "存在未销记的测试意图，禁止开启新 session；请先核对",
                subject=Subject.TEST_RUN,
            )

    def begin_session(self) -> str:
        self.assert_ready_for_session()
        session = ulid()
        self.data["session"] = session
        self.data["status"] = RunStatus.RUNNING.value
        self.data["stop_reason"] = None
        return session

    @property
    def session(self) -> str | None:
        return self.data.get("session")

    # -- 计划 ---------------------------------------------------------------
    def plan(self) -> dict[str, Any]:
        return self.data["plan"]

    def set_plan(self, items: Iterable[str]) -> dict[str, Any]:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        plan = self.data["plan"]
        plan["items"] = cleaned
        plan["revision"] = int(plan.get("revision", 0)) + 1
        plan["updated_at"] = now_iso()
        return plan

    # -- 预算 ---------------------------------------------------------------
    BUDGET_KEYS = {
        "steps": "max_steps",
        "tool_calls": "max_tool_calls",
        "writes": "max_writes",
        "test_runs": "max_test_runs",
        "seconds": "max_seconds",
    }

    def _limit_for(self, key: str) -> float:
        limits = self.data["budget"]["limits"]
        return float(limits.get(self.BUDGET_KEYS.get(key, key), 0))

    def budget_used(self, key: str) -> float:
        return float(self.data["budget"]["used"].get(key, 0.0))

    def budget_limit(self, key: str) -> float:
        return self._limit_for(key)

    def budget_ok(self, key: str) -> bool:
        return self.budget_used(key) <= self._limit_for(key)

    def budget_can_spend(self, key: str) -> bool:
        return self.budget_used(key) < self._limit_for(key)

    def charge(self, key: str, amount: float = 1.0) -> None:
        used = self.data["budget"]["used"]
        used[key] = float(used.get(key, 0.0)) + float(amount)

    def exhausted(self) -> bool:
        for key in ("steps", "tool_calls", "writes", "test_runs", "seconds"):
            if not self.budget_can_spend(key):
                return True
        return False

    # -- 台账 ---------------------------------------------------------------
    def ledger_entry(self, signature: str) -> dict[str, Any]:
        return self.data["ledger"].setdefault(
            signature,
            {"count": 0, "tool_call_ids": [], "applied": False, "result": None},
        )

    def ledger_count(self, signature: str) -> int:
        return int(self.ledger_entry(signature).get("count", 0))

    def ledger_applied(self, signature: str) -> bool:
        return bool(self.ledger_entry(signature).get("applied", False))

    def ledger_result(self, signature: str) -> Any:
        return self.ledger_entry(signature).get("result")

    def ledger_record(self, signature: str, tool_call_id: str, *, applied: bool = False, result: Any = None) -> None:
        entry = self.ledger_entry(signature)
        entry["count"] = int(entry.get("count", 0)) + 1
        if tool_call_id and tool_call_id not in entry["tool_call_ids"]:
            entry["tool_call_ids"].append(tool_call_id)
        if applied:
            entry["applied"] = True
            entry["result"] = result

    def mark_applied(self, signature: str, result: Any) -> None:
        entry = self.ledger_entry(signature)
        entry["applied"] = True
        entry["result"] = result

    def release(self, signature: str) -> None:
        entry = self.ledger_entry(signature)
        entry["applied"] = False
        entry["result"] = None

    # -- 待审批 -------------------------------------------------------------
    def record_pending(
        self,
        tool_call_id: str,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        signature: str,
        pre_sha256: str | None,
        fingerprint_at_request: str,
        step: int,
        target: str = "",
    ) -> dict[str, Any]:
        record = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "signature": signature,
            "pre_sha256": pre_sha256,
            "fingerprint_at_request": fingerprint_at_request,
            "step": step,
            "target": target,
            "at": now_iso(),
        }
        self.data["pending"][tool_call_id] = record
        return record

    def pending_items(self) -> list[dict[str, Any]]:
        return list(self.data["pending"].values())

    def get_pending(self, tool_call_id: str) -> dict[str, Any] | None:
        return self.data["pending"].get(tool_call_id)

    def pop_pending(self, tool_call_id: str) -> dict[str, Any] | None:
        return self.data["pending"].pop(tool_call_id, None)

    def pop_pending_by_signature(self, signature: str) -> list[dict[str, Any]]:
        removed = [item for item in self.data["pending"].values() if item.get("signature") == signature]
        for item in removed:
            self.data["pending"].pop(item["tool_call_id"], None)
        return removed

    # -- 审批 ---------------------------------------------------------------
    def record_approval(self, approval: Approval) -> None:
        self.data["approvals"][approval.tool_call_id] = approval.to_dict()

    def approvals_valid_count(self) -> int:
        return sum(1 for item in self.data["approvals"].values() if not item.get("consumed") and not item.get("rejected"))

    def find_approval(self, signature: str, fingerprint: str, step: int) -> dict[str, Any] | None:
        for item in self.data["approvals"].values():
            if item.get("rejected") or item.get("consumed"):
                continue
            if item.get("signature") != signature:
                continue
            if item.get("fingerprint") != fingerprint:
                continue
            if step - int(item.get("granted_step", 0)) > int(item.get("ttl_steps", 3)):
                continue
            return item
        return None

    def consume_approval(self, tool_call_id: str) -> None:
        item = self.data["approvals"].get(tool_call_id)
        if item:
            item["consumed"] = True

    def reject_approval(self, tool_call_id: str, *, actor: str, reason: str) -> dict[str, Any] | None:
        item = self.data["approvals"].get(tool_call_id)
        if not item:
            return None
        item["rejected"] = True
        item["actor"] = actor
        item["reason"] = reason
        item["at"] = now_iso()
        return item

    # -- 写入 ---------------------------------------------------------------
    def write_intent(
        self,
        signature: str,
        *,
        tool_call_id: str,
        target: str,
        pre_sha256: str | None,
        session: str,
        step: int,
    ) -> dict[str, Any]:
        record = {
            "signature": signature,
            "tool_call_id": tool_call_id,
            "target": target,
            "state": WriteState.INTENT.value,
            "pre_sha256": pre_sha256,
            "post_sha256": None,
            "result": None,
            "session": session,
            "step": step,
            "at": now_iso(),
        }
        self.data["writes"][signature] = record
        return record

    def write_result(self, signature: str, *, post_sha256: str | None, result: dict[str, Any]) -> None:
        record = self.data["writes"].setdefault(
            signature,
            {
                "signature": signature,
                "tool_call_id": "",
                "target": result.get("target", ""),
                "state": WriteState.INTENT.value,
                "pre_sha256": None,
                "post_sha256": None,
                "result": None,
                "session": self.session,
                "step": int(self.data.get("step", 0)),
                "at": now_iso(),
            },
        )
        record["state"] = WriteState.DONE.value
        record["post_sha256"] = post_sha256
        record["result"] = result
        record["at"] = now_iso()

    def set_write_state(self, signature: str, state: WriteState | str, **fields: Any) -> None:
        record = self.data["writes"].get(signature)
        if not record:
            return
        record["state"] = state.value if isinstance(state, WriteState) else str(state)
        record.update(fields)
        record["at"] = now_iso()

    def fail_intents(self, *, session: str | None = None, state: WriteState = WriteState.UNCERTAIN) -> list[str]:
        changed = []
        for sig, record in self.data["writes"].items():
            if record.get("state") != WriteState.INTENT.value:
                continue
            if session is not None and record.get("session") != session:
                continue
            record["state"] = state.value
            record["at"] = now_iso()
            changed.append(sig)
        return changed

    def writes(self) -> dict[str, dict[str, Any]]:
        return self.data["writes"]

    def open_writes(self, session: str | None = None) -> list[dict[str, Any]]:
        result = []
        for record in self.data["writes"].values():
            if record.get("state") not in OPEN_STATES:
                continue
            if session is not None and record.get("session") != session:
                continue
            result.append(record)
        return result

    # -- 测试运行 -----------------------------------------------------------
    def test_intent(
        self,
        signature: str,
        *,
        tool_call_id: str,
        scope: str,
        command: list[str],
        session: str,
        step: int,
    ) -> dict[str, Any]:
        record = {
            "signature": signature,
            "tool_call_id": tool_call_id,
            "scope": scope,
            "command": list(command),
            "state": WriteState.INTENT.value,
            "result": None,
            "session": session,
            "step": step,
            "pid": None,
            "at": now_iso(),
        }
        self.data["tests"][signature] = record
        return record

    def test_result(self, signature: str, *, result: dict[str, Any], pid: int | None = None) -> None:
        record = self.data["tests"].setdefault(
            signature,
            {
                "signature": signature,
                "tool_call_id": "",
                "scope": result.get("scope", ""),
                "command": list(result.get("argv", [])),
                "state": WriteState.INTENT.value,
                "result": None,
                "session": self.session,
                "step": int(self.data.get("step", 0)),
                "pid": None,
                "at": now_iso(),
            },
        )
        record["state"] = WriteState.DONE.value
        record["result"] = result
        record["pid"] = pid
        record["at"] = now_iso()

    def fail_test_intents(self, *, session: str | None = None) -> list[str]:
        changed = []
        for sig, record in self.data["tests"].items():
            if record.get("state") != WriteState.INTENT.value:
                continue
            if session is not None and record.get("session") != session:
                continue
            record["state"] = WriteState.UNCERTAIN.value
            record["at"] = now_iso()
            changed.append(sig)
        return changed

    def tests(self) -> dict[str, dict[str, Any]]:
        return self.data["tests"]

    def open_tests(self, session: str | None = None) -> list[dict[str, Any]]:
        result = []
        for record in self.data["tests"].values():
            if record.get("state") != WriteState.INTENT.value:
                continue
            if session is not None and record.get("session") != session:
                continue
            result.append(record)
        return result

    # -- 证据 ---------------------------------------------------------------
    def next_seq(self) -> int:
        return max([int(item.get("seq", 0)) for item in self.data["evidence"]] + [0]) + 1

    def append_evidence(
        self,
        *,
        scope: str,
        kind: str,
        payload: dict[str, Any],
        fingerprint: str,
        artifact_version: str | None = None,
    ) -> Evidence:
        evidence = Evidence(
            id=ulid(),
            scope=scope,
            seq=self.next_seq(),
            kind=kind,
            payload=dict(payload),
            fingerprint=fingerprint,
            artifact_version=artifact_version or fingerprint,
            at=now_iso(),
        )
        self.data["evidence"].append(evidence.to_dict())
        return evidence

    def evidence(self) -> list[dict[str, Any]]:
        return self.data["evidence"]

    def latest_evidence(self, scope: str) -> dict[str, Any] | None:
        items = [item for item in self.data["evidence"] if item.get("scope") == scope]
        if not items:
            return None
        return max(items, key=lambda item: int(item.get("seq", 0)))

    # -- 对账 ---------------------------------------------------------------
    def set_reconcile(self, signature: str, *, verdict: ReconcileVerdict | str, actor: str, note: str) -> None:
        self.data["reconcile"][signature] = {
            "verdict": verdict.value if isinstance(verdict, ReconcileVerdict) else str(verdict),
            "actor": actor,
            "note": note,
            "at": now_iso(),
        }

    def reconcile_records(self) -> dict[str, dict[str, Any]]:
        return self.data["reconcile"]

    # -- 阻断 / 终止 ---------------------------------------------------------
    def set_blocked(self, reason: str, *, stop_reason: str | None = None, signature: str = "") -> None:
        self.data["blocked"] = {
            "reason": reason,
            "stop_reason": stop_reason,
            "signature": signature,
            "at": now_iso(),
        }

    def clear_blocked(self) -> None:
        self.data["blocked"] = None

    @property
    def blocked(self) -> dict[str, Any] | None:
        return self.data.get("blocked")

    def set_termination(self, unconfirmed: bool, pid: int | None = None) -> None:
        self.data["termination"] = {
            "unconfirmed": bool(unconfirmed),
            "pid": pid,
            "at": now_iso(),
        }

    @property
    def termination(self) -> dict[str, Any]:
        return self.data["termination"]

    # -- 产物 ---------------------------------------------------------------
    def record_artifact(self, name: str, info: dict[str, Any]) -> None:
        self.data["artifacts"][name] = {**info, "at": now_iso()}

    def artifacts(self) -> dict[str, Any]:
        return self.data["artifacts"]

    # -- 兼容别名 -----------------------------------------------------------
    def ledger(self) -> dict[str, Any]:
        return self.data["ledger"]

    def pending(self) -> dict[str, Any]:
        return self.data["pending"]

    def approvals(self) -> dict[str, Any]:
        return self.data["approvals"]

    def iter_open_writes(self) -> Iterable[dict[str, Any]]:
        return self.open_writes()

    def iter_open_tests(self) -> Iterable[dict[str, Any]]:
        return self.open_tests()
