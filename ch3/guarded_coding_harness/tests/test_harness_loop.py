"""H2/H3、C1–C4 与取消的端到端回归。"""

from __future__ import annotations

import threading
import time

import pytest

from conftest import SAMPLE_SOURCE, SAMPLE_SOURCE_BROKEN, SAMPLE_SOURCE_PATCHED, call, make_harness, read
from contracts import StopReason, UnknownResult, WorkspaceChanged, call_signature
from harness import GuardedHarness
from sandbox import is_process_alive


def test_successful_run_uses_fresh_evidence_and_completes(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("run_test", scope="target"),
            call("final_answer", answer="已修复"),
        ],
        required_scopes=("target",),
        require_write_approval=False,
        max_steps=5,
    )
    result = harness.run("修复 add")
    assert result.status == "completed", result.message
    assert result.stop_reason == "completed"
    latest = harness.state.latest_evidence("target")
    assert latest is not None
    assert latest["payload"]["returncode"] == 0


def test_h2_stale_evidence_blocks_completion(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("run_test", scope="target"),
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_BROKEN),
            call("final_answer", answer="已修复"),
        ],
        required_scopes=("target",),
        require_write_approval=False,
        max_steps=4,
    )
    result = harness.run("修复后再次修改源码")
    assert result.status != "completed"
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_BROKEN
    ok, message = harness.checker.check()
    assert not ok
    assert "过期" in message


def test_h3_budget_stop_has_explicit_reason(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("read_file", path="src/app.py"),
            call("list_files", path="src"),
            call("inspect_workspace"),
        ],
        max_tool_calls=2,
        max_steps=5,
        repeat_limit=10,
    )
    result = harness.run("触发预算")
    assert result.status == "failed"
    assert result.stop_reason == StopReason.BUDGET_EXCEEDED.value


def test_c1_checkpoint_after_every_safe_step(workspace):
    harness, _ = make_harness(
        workspace,
        [call("read_file", path="src/app.py"), call("inspect_workspace")],
        max_steps=4,
    )
    harness.run("读文件")
    snapshots = harness.checkpoints.list_snapshots()
    assert snapshots
    payload = harness.checkpoints.latest()
    assert payload is not None
    snapshot = payload["snapshot"]
    for key in ("budget", "evidence", "pending", "writes", "tests", "step"):
        assert key in snapshot
    assert any(path.name.endswith("after_step.json") for path in snapshots)


def test_c2_unknown_write_requires_reconcile_then_release(workspace):
    signature = call_signature("apply_patch", {"path": "src/app.py", "old_text": "a", "new_text": "b"})
    harness1, _ = make_harness(workspace, [])
    harness1.state.reset_state("模拟崩溃", harness1.policy.limits())
    harness1.state.begin_session()
    target = workspace / "src" / "app.py"
    from contracts import sha256_file

    pre = sha256_file(target)
    target.write_text(SAMPLE_SOURCE_PATCHED, encoding="utf-8")
    harness1.state.write_intent(
        signature,
        tool_call_id="call-c2",
        target="src/app.py",
        pre_sha256=pre,
        session=harness1.state.session,
        step=1,
    )
    harness1.state.data["step"] = 1
    harness1.state.data["fingerprint"] = harness1.sandbox.fingerprint()
    harness1.state.save()
    harness1.checkpoints.save(harness1.state.snapshot(), step=1, boundary="simulated-crash")

    harness2 = GuardedHarness(workspace, harness1.model, policy=harness1.policy)
    with pytest.raises(UnknownResult):
        harness2.resume()
    assert harness2.state.status == "waiting_reconcile"
    assert harness2.state.open_writes()

    # 现场与执行前一致时，人工结论 not_executed 才能成立。
    target.write_text(SAMPLE_SOURCE, encoding="utf-8")
    outcome = harness2.reconcile({signature: "not_executed"}, actor="ops", note="已核对")
    assert signature in outcome["resolved"]
    assert harness2.state.ledger_applied(signature) is False
    assert not harness2.state.open_writes()


def test_c3_fresh_instance_resume_keeps_counts_and_skips_duplicates(workspace):
    script = [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)]
    harness1, _ = make_harness(workspace, script, max_steps=5, repeat_limit=3)
    first = harness1.run("修复 add")
    assert first.status == "waiting_approval"
    pending = harness1.state.pending_items()[0]
    used_tool_calls = harness1.state.budget_used("tool_calls")
    assert used_tool_calls >= 1

    harness2 = GuardedHarness(workspace, harness1.model, policy=harness1.policy)
    harness2.approve(pending["tool_call_id"], actor="reviewer", reason="确认")
    harness1.model.script.extend(
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("run_test", scope="target"),
            call("final_answer", answer="已修复"),
        ]
    )
    second = harness2.resume()

    assert second.status == "completed", second.message
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert harness2.state.budget_used("tool_calls") > used_tool_calls
    assert harness2.current_step > first.step


def test_c4_workspace_change_invalidates_old_state_and_stays_blocked(workspace):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        max_steps=3,
    )
    result = harness.run("修复 add")
    assert result.status == "waiting_approval"

    (workspace / "src" / "app.py").write_text(SAMPLE_SOURCE + "\n# external edit\n", encoding="utf-8")

    with pytest.raises(WorkspaceChanged):
        harness.resume()
    assert harness.state.status == "waiting_reconcile"
    with pytest.raises(WorkspaceChanged):
        harness.resume()


def test_s4_cancel_through_harness_kills_process(workspace):
    harness, _ = make_harness(
        workspace,
        [call("run_test", scope="slow"), call("inspect_workspace")],
        suites={"target": ["tests/test_target.py"], "slow": ["tests/test_slow.py"]},
        max_steps=4,
        command_timeout=30,
        max_seconds=60,
    )
    timer = threading.Timer(0.8, harness.cancel)
    timer.start()
    started = time.monotonic()
    result = harness.run("运行慢测试")
    elapsed = time.monotonic() - started
    timer.cancel()

    assert result.status == "cancelled"
    assert result.stop_reason == StopReason.CANCELLED.value
    assert elapsed < 10
    assert harness.sandbox.last_pid is not None
    assert not is_process_alive(harness.sandbox.last_pid)


def test_pause_is_resumable(workspace):
    harness, model = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        max_steps=6,
    )
    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    pending = harness.state.pending_items()[0]

    harness.pause()
    assert harness.state.status == "paused"
    assert harness._pause_requested is True

    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认")
    model.script.extend(
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("run_test", scope="target"),
            call("final_answer", answer="已修复"),
        ]
    )
    second = harness.resume()

    assert second.status == "completed", second.message
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert harness._pause_requested is False
    assert harness.sandbox.cancel_requested is False


def test_cancelled_run_cannot_be_revived(workspace):
    harness, _ = make_harness(workspace, [], max_steps=4)
    harness.pause()
    assert harness.state.status == "paused"
    harness.state.set_status("cancelled", stop_reason="cancelled")
    from contracts import PolicyDenied

    with pytest.raises(PolicyDenied):
        harness.reset_cancel()


def test_cancelled_instance_can_start_new_run(workspace):
    harness, model = make_harness(workspace, [], max_steps=3)
    harness.cancel()
    assert harness.state.status == "cancelled"

    model.script.extend([call("read_file", path="src/app.py")])
    result = harness.run("取消后重新开始")

    assert harness._cancel_requested is False
    assert harness.sandbox.cancel_requested is False
    assert result.stop_reason != "cancelled"


def test_h1_self_reported_evidence_cannot_complete(workspace):
    harness, _ = make_harness(
        workspace,
        [
            call("record_evidence", scope="target", summary="我已修复，无需测试", ok=True),
            call("final_answer", answer="已修复"),
        ],
        max_steps=4,
    )
    result = harness.run("修复 add")
    assert result.status != "completed"
    assert result.stop_reason in {"max_steps", "budget_exceeded"}
    assert harness.state.evidence()
