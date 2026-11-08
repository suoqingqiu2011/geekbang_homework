"""C1–C4 的状态与恢复单元测试。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from checkpoint import Checkpoint, classify_batch, classify_write, plan_resume
from conftest import SAMPLE_SOURCE_PATCHED, call, child_env, make_harness, read
from contracts import BudgetLimits, ReconcileVerdict, StopReason, UnknownResult, WriteState
from harness import GuardedHarness
from state_store import StateStore


def test_classify_write_three_states():
    record = {"state": WriteState.INTENT.value, "pre_sha256": "aaa", "post_sha256": None}
    assert classify_write(record, "aaa") == ReconcileVerdict.NOT_EXECUTED
    assert classify_write(record, "bbb") == ReconcileVerdict.UNCERTAIN

    done = {"state": WriteState.DONE.value, "pre_sha256": "aaa", "post_sha256": "bbb"}
    assert classify_write(done, "bbb") == ReconcileVerdict.EXECUTED
    assert classify_write({"state": "intent", "pre_sha256": "aaa", "post_sha256": "bbb"}, "bbb") == ReconcileVerdict.EXECUTED

    new_file = {"state": "intent", "pre_sha256": None, "post_sha256": None}
    assert classify_write(new_file, None) == ReconcileVerdict.NOT_EXECUTED
    assert classify_write(new_file, "ccc") == ReconcileVerdict.UNCERTAIN


def test_classify_batch():
    assert classify_batch([ReconcileVerdict.EXECUTED, ReconcileVerdict.EXECUTED]) == ReconcileVerdict.EXECUTED
    assert classify_batch([]) == ReconcileVerdict.NOT_EXECUTED
    assert classify_batch([ReconcileVerdict.EXECUTED, ReconcileVerdict.NOT_EXECUTED]) == ReconcileVerdict.UNCERTAIN


def test_checkpoint_atomic_roundtrip_and_tamper(tmp_path):
    checkpoint = Checkpoint(tmp_path / "cp")
    snapshot = {"version": 1, "step": 3, "budget": {"used": {"steps": 3}}}
    path = checkpoint.save(snapshot, step=3, boundary="after_step")
    assert path.name == "step-000003-after_step.json"
    payload = checkpoint.load(path)
    assert payload["snapshot"] == snapshot
    assert checkpoint.latest()["snapshot"] == snapshot

    data = json.loads(path.read_text(encoding="utf-8"))
    data["snapshot"]["budget"]["used"]["steps"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert checkpoint.load(path) is None


def test_checkpoint_latest_uses_step_order(tmp_path):
    checkpoint = Checkpoint(tmp_path / "cp")
    checkpoint.save({"step": 1}, step=1, boundary="after_step")
    checkpoint.save({"step": 2}, step=2, boundary="halt")
    assert checkpoint.latest()["snapshot"]["step"] == 2


def _snapshot(**overrides):
    base = {
        "status": "waiting_approval",
        "stop_reason": "approval_required",
        "fingerprint": "fp:v1:aaa",
        "step": 2,
        "writes": {},
        "tests": {},
        "approvals": {},
        "blocked": None,
        "termination": {"unconfirmed": False},
        "budget": {"limits": {"max_steps": 5, "max_tool_calls": 5, "max_writes": 5, "max_test_runs": 5, "max_seconds": 60}, "used": {}},
    }
    base.update(overrides)
    return base


def test_plan_resume_priority_and_workspace_change():
    open_write = {"state": "intent", "signature": "sig:1", "target": "src/app.py"}
    plan = plan_resume(
        _snapshot(writes={"sig:1": open_write}, blocked={"reason": "wait", "stop_reason": "approval_required"}),
        fingerprint="fp:v1:bbb",
    )
    assert plan.action == "refuse"
    assert plan.stop_reason == StopReason.WORKSPACE_CHANGED.value
    assert plan.fingerprint_changed


def test_plan_resume_unknown_test_blocks_before_anything_else():
    plan = plan_resume(
        _snapshot(tests={"sig:t": {"state": "intent", "signature": "sig:t"}}),
        fingerprint="fp:v1:aaa",
    )
    assert plan.action == "refuse"
    assert plan.stop_reason == StopReason.UNKNOWN_RESULT.value


def test_plan_resume_open_write_requires_reconcile():
    plan = plan_resume(
        _snapshot(writes={"sig:1": {"state": "uncertain", "signature": "sig:1", "target": "src/app.py"}}),
        fingerprint="fp:v1:aaa",
    )
    assert plan.action == "reconcile"


def test_plan_resume_refuses_unconfirmed_termination():
    plan = plan_resume(_snapshot(termination={"unconfirmed": True, "pid": 10}), fingerprint="fp:v1:aaa")
    assert plan.action == "refuse"
    assert plan.stop_reason == StopReason.UNKNOWN_RESULT.value


def test_state_ledger_release_allows_retry(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.reset_state("t", BudgetLimits())
    signature = "sig:1"
    state.write_intent(signature, tool_call_id="c1", target="src/app.py", pre_sha256="aaa", session="s1", step=1)
    state.mark_applied(signature, {"ok": True})
    assert state.ledger_applied(signature)
    state.set_write_state(signature, WriteState.NOT_EXECUTED)
    state.release(signature)
    assert not state.ledger_applied(signature)


def test_begin_session_refuses_open_intent(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.write_intent("sig:1", tool_call_id="c1", target="src/app.py", pre_sha256=None, session="s1", step=1)
    with pytest.raises(UnknownResult):
        state.begin_session()


def test_abandon_tests_unblocks_resume(workspace):
    harness, _ = make_harness(workspace, [], max_steps=2)
    harness.state.reset_state("模拟崩溃", harness.policy.limits())
    harness.state.begin_session()
    harness.state.test_intent(
        "sig:test",
        tool_call_id="call-test",
        scope="target",
        command=["python", "-m", "pytest"],
        session=harness.state.session,
        step=1,
    )
    harness.state.data["step"] = 1
    harness.state.data["fingerprint"] = harness.sandbox.fingerprint()
    harness.state.save()
    harness.checkpoints.save(harness.state.snapshot(), step=1, boundary="simulated-crash")

    resumed = GuardedHarness(workspace, harness.model, policy=harness.policy)
    with pytest.raises(UnknownResult):
        resumed.resume()
    assert resumed.state.open_tests()

    outcome = resumed.abandon_tests(actor="ops", note="进程崩溃，测试结果不可判定")
    assert "sig:test" in outcome["abandoned"]
    assert resumed.state.open_tests() == []

    result = resumed.resume()
    assert result.status in {"failed", "completed"}
    assert result.stop_reason != StopReason.UNKNOWN_RESULT.value


CHILD_RESUME_SCRIPT = """
import json, sys
from pathlib import Path
from conftest import SAMPLE_SOURCE_PATCHED, ScriptedModel, call
from harness import GuardedHarness, HarnessPolicy

workspace = Path(sys.argv[1])
script = [
    call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
    call("write_file", path="artifacts/report.txt", content="note"),
    call("run_test", scope="target"),
    call("final_answer", answer="已修复"),
]
harness = GuardedHarness(workspace, ScriptedModel(script), policy=HarnessPolicy(max_steps=8))
result = harness.resume()
print("RESULT:" + json.dumps(result.to_dict(), ensure_ascii=False))
"""


def _audit_events(workspace, run_id):
    path = workspace / "artifacts" / ".harness" / "audit.jsonl"
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("run_id") == run_id:
            events.append(event)
    return events


def test_checkpoint_is_scoped_per_run(workspace):
    harness, _ = make_harness(workspace, [call("inspect_workspace")], max_steps=2)
    harness.run("第一次")
    first_run = harness.run_id
    first_dir = harness.checkpoints.directory
    assert first_dir.name == first_run
    assert first_dir.parent == harness.base_dir / "checkpoints"

    second, _ = make_harness(workspace, [call("inspect_workspace")], max_steps=2)
    second.run("第二次")
    assert second.run_id != first_run
    assert second.checkpoints.directory != first_dir
    assert second.checkpoints.directory.name == second.run_id
    assert second.checkpoints.latest()["snapshot"]["run_id"] == second.run_id
    assert first_dir.is_dir()


def test_c1_checkpoint_carries_plan_evidence_budget_and_pending(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("update_plan", items=["定位 add 缺陷", "修复 src/app.py", "运行 target 测试"]),
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
        ],
        max_steps=4,
    )
    result = harness.run("修复 add")
    assert result.status == "waiting_approval"
    snapshot = harness.checkpoints.latest()["snapshot"]
    assert snapshot["run_id"] == harness.run_id
    assert snapshot["step"] >= 1
    assert snapshot["plan"]["items"] == ["定位 add 缺陷", "修复 src/app.py", "运行 target 测试"]
    assert snapshot["plan"]["revision"] == 1
    assert snapshot["budget"]["used"]["tool_calls"] >= 1
    assert snapshot["pending"]
    assert "evidence" in snapshot


def test_c3_resume_in_new_process_skips_completed_writes(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("write_file", path="artifacts/report.txt", content="note"),
        ],
        max_steps=8,
    )
    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    harness.approve(harness.state.pending_items()[0]["tool_call_id"], actor="reviewer", reason="确认 A")

    second = harness.resume()
    assert second.status == "waiting_approval"
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    harness.approve(harness.state.pending_items()[0]["tool_call_id"], actor="reviewer", reason="确认 B")

    run_id = harness.run_id
    step_before = harness.current_step
    calls_before = harness.state.budget_used("tool_calls")
    applied_before = sum(1 for e in _audit_events(workspace, run_id) if e.get("event") == "write.applied")

    child = subprocess.run(
        [sys.executable, "-c", CHILD_RESUME_SCRIPT, str(workspace)],
        cwd=str(workspace),
        env=child_env(workspace),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert child.returncode == 0, child.stderr
    line = [item for item in child.stdout.splitlines() if item.startswith("RESULT:")][-1]
    result = json.loads(line[len("RESULT:"):])

    assert result["status"] == "completed", result
    assert result["run_id"] == run_id
    assert result["step"] > step_before
    assert result["tool_calls"] > calls_before
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert read(workspace / "artifacts" / "report.txt") == "note"

    events = _audit_events(workspace, run_id)
    assert sum(1 for e in events if e.get("event") == "write.applied") == applied_before + 1
    assert any(
        e.get("event") == "gate.decision"
        and e.get("decision") == "deny"
        and e.get("rule") in {"repeat_limit", "duplicate_write"}
        for e in events
    )
