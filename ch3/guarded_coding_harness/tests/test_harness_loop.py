"""H2/H3、C1–C4 与取消的端到端回归。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from conftest import (
    SAMPLE_SOURCE,
    SAMPLE_SOURCE_BROKEN,
    SAMPLE_SOURCE_PATCHED,
    ScriptedModel,
    call,
    make_harness,
    read,
)
from contracts import RunStatus, StopReason, UnknownResult, WorkspaceChanged, call_signature
from harness import GuardedHarness, HarnessPolicy
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
    outcome = harness2.reconcile({"call-c2": "not_executed"}, actor="ops", note="已核对")
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


def test_s4_cancel_through_harness_kills_process(workspace, monkeypatch):
    import sandbox

    killed: list[int] = []
    real_kill = sandbox.kill_tree

    def spy(pid, job=None):
        killed.append(pid)
        return real_kill(pid, job)

    monkeypatch.setattr(sandbox, "kill_tree", spy)
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
    assert killed and not is_process_alive(killed[0])
    # 会话结束后清空 last_pid，避免非运行态的 cancel() 用旧 PID 误杀无关进程。
    assert harness.sandbox.last_pid is None


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


def test_text_only_output_is_tolerated_and_audited(workspace):
    from smolagents.models import ChatMessage, MessageRole

    from harness import ensure_text_tolerant

    class StrictModel:
        def parse_tool_calls(self, message):
            raise ValueError("The model output does not contain any JSON blob.")

    plain = ChatMessage(role=MessageRole.ASSISTANT, content="已修复")
    tolerant = ensure_text_tolerant(StrictModel())
    assert tolerant.parse_tool_calls(plain).tool_calls is None

    harness, _ = make_harness(workspace, ["已修复"], max_steps=3)
    result = harness.run("纯文本")
    assert result.status != "completed"
    events = [item["event"] for item in harness.audit.records()]
    assert "model.text_only" in events


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


def test_partial_batch_abort_does_not_charge_or_burn_approval(workspace):
    harness, model = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        require_write_approval=True,
        max_steps=6,
    )
    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    first_pending = harness.state.pending_items()[0]["tool_call_id"]
    harness.approve(first_pending, actor="reviewer", reason="确认 A")
    assert harness.state.approvals_valid_count() == 1

    # 同批里第二条写入仍需审批：整批会被中止，第一条也不该执行。
    model.script.extend(
        [[
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("write_file", path="artifacts/report.txt", content="note"),
        ]]
    )
    second = harness.resume()
    assert second.status == "waiting_approval"
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE
    assert not (workspace / "artifacts" / "report.txt").exists()
    assert harness.state.budget_used("writes") == 0.0
    assert harness.state.approvals_valid_count() == 1
    assert len(harness.state.pending_items()) == 2

    # 补批后整批全部放行，先前审批仍然有效，两条写入都真正落盘。
    new_pending = next(p for p in harness.state.pending_items() if p["tool_call_id"] != first_pending)
    harness.approve(new_pending["tool_call_id"], actor="reviewer", reason="确认 B")
    model.script.extend(
        [[
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("write_file", path="artifacts/report.txt", content="note"),
        ]]
    )
    harness.resume()
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert read(workspace / "artifacts" / "report.txt") == "note"
    assert harness.state.budget_used("writes") == 2.0


def test_apply_patch_records_artifact(workspace):
    harness, _ = make_harness(
        workspace,
        [call("apply_patch", path="src/app.py", old_text="return a - b", new_text="return a + b")],
        require_write_approval=False,
        max_steps=2,
    )
    harness.run("修补 add")
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert "src/app.py" in harness.state.artifacts()


def test_model_error_returns_failed_result(workspace):
    class BoomModel(ScriptedModel):
        def generate(self, *args, **kwargs):
            raise RuntimeError("模型崩了")

    harness = GuardedHarness(workspace, BoomModel(), policy=HarnessPolicy(max_steps=2))
    result = harness.run("修复 add")
    assert result.status == "failed"
    assert result.stop_reason == "model_error"
    assert "模型执行出错" in result.message
    assert harness._running is False


def test_new_run_refuses_unresolved_write_intent(workspace):
    harness, _ = make_harness(workspace, [], max_steps=2)
    harness.state.write_intent(
        "sig:open",
        tool_call_id="call-open",
        target="src/app.py",
        pre_sha256=None,
        session="s1",
        step=1,
    )
    with pytest.raises(UnknownResult):
        harness.run("新任务")
    assert harness._running is False


def test_search_code_rejects_redos_patterns(workspace):
    harness, _ = make_harness(workspace, [])
    with pytest.raises(ValueError, match="嵌套量词"):
        harness.runtime._do_search_code({"pattern": "(a+)+$"}, "c1")
    with pytest.raises(ValueError, match="过长"):
        harness.runtime._do_search_code({"pattern": "x" * 201}, "c2")


def test_search_code_truncates_long_lines_before_match(workspace):
    harness, _ = make_harness(workspace, [])
    (workspace / "src" / "long.py").write_text("a" * 5000 + "NEEDLE\n", encoding="utf-8")
    payload = json.loads(harness.runtime._do_search_code({"pattern": "NEEDLE"}, "c"))
    # 行被截断到 2000 字符，因此行尾的 NEEDLE 不会被扫到；关键是不触发回溯爆炸。
    assert payload["matches"] == []


def test_read_file_rejects_oversized_file(workspace):
    harness, _ = make_harness(workspace, [])
    big = workspace / "artifacts" / "big.log"
    big.write_text("x" * 1_100_000, encoding="utf-8")
    with pytest.raises(ValueError, match="文件过大"):
        harness.runtime._do_read_file({"path": "artifacts/big.log"}, "c")


def test_reject_clears_blocked_and_allows_resume(workspace):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        max_steps=2,
    )
    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    pending = harness.state.pending_items()
    assert len(pending) == 1
    harness.reject(pending[0]["tool_call_id"], reason="不批准这次修改")
    assert harness.state.pending_items() == []
    assert harness.state.blocked is None
    # 修复前这里会抛 PolicyDenied；修复后应继续执行直到 MAX_STEPS。
    second = harness.resume()
    assert second.stop_reason == "max_steps"


def test_status_mapping_is_shared_by_run_and_resume_paths():
    assert GuardedHarness._status_for_stop(StopReason.BUDGET_EXCEEDED) == RunStatus.FAILED
    assert GuardedHarness._status_for_stop(StopReason.BUDGET_EXCEEDED.value) == RunStatus.FAILED
    assert GuardedHarness._status_for_stop(StopReason.APPROVAL_REQUIRED) == RunStatus.WAITING_APPROVAL
    assert GuardedHarness._status_for_stop(StopReason.APPROVAL_REQUIRED.value) == RunStatus.WAITING_APPROVAL

