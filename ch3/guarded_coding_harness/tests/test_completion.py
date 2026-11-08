"""H1–H2 的完成契约单元测试。"""

from __future__ import annotations

from conftest import call, make_harness
from contracts import Evidence


def test_completion_requires_current_evidence(workspace):
    harness, _ = make_harness(workspace, [])
    ok, message = harness.checker.check()
    assert not ok
    assert "target" in message

    harness.state.append_evidence(
        scope="target",
        kind="test",
        payload={"returncode": 0},
        fingerprint=harness.sandbox.fingerprint(),
    )
    assert harness.checker.check() == (True, "ok")


def test_completion_rejects_stale_evidence(workspace):
    harness, _ = make_harness(workspace, [])
    harness.state.append_evidence(
        scope="target",
        kind="test",
        payload={"returncode": 0},
        fingerprint="fp:v1:old",
    )
    ok, message = harness.checker.check()
    assert not ok
    assert "过期" in message


def test_completion_uses_latest_evidence_not_history(workspace):
    harness, _ = make_harness(workspace, [])
    fingerprint = harness.sandbox.fingerprint()
    harness.state.append_evidence(scope="target", kind="test", payload={"returncode": 0}, fingerprint=fingerprint)
    harness.state.append_evidence(scope="target", kind="test", payload={"returncode": 1}, fingerprint=fingerprint)
    ok, message = harness.checker.check()
    assert not ok
    assert "未通过" in message


def test_multi_scope_completion(workspace):
    harness, _ = make_harness(workspace, [], required_scopes=("target", "boundary"))
    fingerprint = harness.sandbox.fingerprint()
    harness.state.append_evidence(scope="target", kind="test", payload={"returncode": 0}, fingerprint=fingerprint)
    ok, _ = harness.checker.check()
    assert not ok

    harness.state.append_evidence(scope="boundary", kind="test", payload={"returncode": 0}, fingerprint=fingerprint)
    ok, message = harness.checker.check()
    assert ok, message


def test_cancelled_or_timed_out_evidence_never_passes(workspace):
    harness, _ = make_harness(workspace, [])
    fingerprint = harness.sandbox.fingerprint()
    harness.state.append_evidence(
        scope="target",
        kind="test",
        payload={"returncode": 0, "cancelled": True},
        fingerprint=fingerprint,
    )
    assert Evidence.from_dict(harness.state.latest_evidence("target")).is_passing() is False
    assert harness.checker.check()[0] is False


def test_terminated_evidence_never_passes(workspace):
    harness, _ = make_harness(workspace, [])
    fingerprint = harness.sandbox.fingerprint()
    harness.state.append_evidence(
        scope="target",
        kind="test",
        payload={"returncode": 0, "terminated": True},
        fingerprint=fingerprint,
    )
    assert Evidence.from_dict(harness.state.latest_evidence("target")).is_passing() is False
    assert harness.checker.check()[0] is False


def test_h1_final_answer_without_evidence_is_rejected(workspace):
    harness, _ = make_harness(workspace, [call("final_answer", answer="已修复")], max_steps=3)
    result = harness.run("修复 add")
    assert result.status != "completed"
    assert result.stop_reason in {"max_steps", "budget_exceeded"}
    assert harness.state.evidence() == []


def test_self_reported_note_never_satisfies_completion(workspace):
    harness, _ = make_harness(workspace, [])
    harness.state.append_evidence(
        scope="target",
        kind="note",
        payload={"summary": "已完成", "ok": True},
        fingerprint=harness.sandbox.fingerprint(),
    )
    ok, message = harness.checker.check()
    assert not ok
    assert "测试证据" in message


def test_note_does_not_mask_passing_test_evidence(workspace):
    harness, _ = make_harness(workspace, [])
    fingerprint = harness.sandbox.fingerprint()
    harness.state.append_evidence(scope="target", kind="test", payload={"returncode": 0}, fingerprint=fingerprint)
    harness.state.append_evidence(
        scope="target", kind="note", payload={"summary": "备注", "ok": True}, fingerprint=fingerprint
    )
    assert harness.checker.check() == (True, "ok")
