"""G1–G4：门禁与审批。"""

from __future__ import annotations

import pytest

from conftest import SAMPLE_SOURCE, SAMPLE_SOURCE_PATCHED, call, make_harness, read
from contracts import ActionRequest, Decision, call_signature


def spy_handler(monkeypatch, runtime, attribute):
    calls = []
    original = getattr(runtime, attribute)

    def wrapper(arguments, tool_call_id):
        calls.append(dict(arguments))
        return original(arguments, tool_call_id)

    monkeypatch.setattr(runtime, attribute, wrapper)
    return calls


def test_g1_missing_argument_never_calls_handler(workspace, monkeypatch):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="src/app.py")],
        max_steps=2,
        max_tool_calls=3,
    )
    calls = spy_handler(monkeypatch, harness.runtime, "_do_write_file")
    result = harness.run("缺少 content 的写入")
    assert calls == []
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE
    assert result.status != "completed"
    decisions = [item for item in harness.audit.records() if item["event"] == "gate.decision"]
    assert decisions and decisions[0]["decision"] == Decision.DENY.value
    assert decisions[0]["detail"]["error"]["code"] == "missing_argument"


def test_g1_wrong_type_never_calls_handler(workspace, monkeypatch):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=123)],
        max_steps=2,
        max_tool_calls=3,
    )
    calls = spy_handler(monkeypatch, harness.runtime, "_do_write_file")
    result = harness.run("类型错误的写入")
    assert calls == []
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE
    errors = [
        item for item in harness.audit.records()
        if item["event"] == "gate.decision" and item["decision"] == Decision.DENY.value
    ]
    assert errors and errors[0]["detail"]["error"]["code"] == "invalid_argument_type"
    assert result.status != "completed"


def test_g2_third_identical_call_is_blocked_before_handler(workspace, monkeypatch):
    script = [call("read_file", path="src/app.py") for _ in range(3)]
    harness, _ = make_harness(workspace, script, max_steps=3, repeat_limit=2)
    calls = spy_handler(monkeypatch, harness.runtime, "_do_read_file")
    harness.run("重复读取")
    assert len(calls) == 2
    verdicts = [item for item in harness.audit.records() if item["event"] == "gate.decision"]
    assert len(verdicts) == 3
    assert verdicts[-1]["decision"] == Decision.DENY.value
    assert verdicts[-1]["rule"] == "repeat_limit"


def test_g3_unapproved_write_keeps_file_and_waits(workspace, monkeypatch):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        max_steps=3,
    )
    calls = spy_handler(monkeypatch, harness.runtime, "_do_write_file")
    result = harness.run("修复 add")
    assert calls == []
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE
    assert result.status == "waiting_approval"
    assert result.stop_reason == "approval_required"
    assert len(result.pending) == 1
    assert result.pending[0]["signature"]


def test_g4_approved_write_executes_once_then_duplicate_denied(workspace, monkeypatch):
    script = [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)]
    harness, model = make_harness(workspace, script, max_steps=5, repeat_limit=3)
    calls = spy_handler(monkeypatch, harness.runtime, "_do_write_file")

    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    assert calls == []

    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认修改")

    model.script.extend(
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
        ]
    )
    second = harness.resume()

    assert len(calls) == 1
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
    assert harness.state.ledger_applied(pending["signature"]) is True
    assert second.step > first.step
    duplicate = [
        item for item in harness.audit.records()
        if item["event"] == "gate.decision" and item["rule"] == "duplicate_write"
    ]
    assert duplicate
    assert duplicate[0]["detail"]["error"]["detail"]["first_result"]


def test_gate_duplicate_applied_includes_first_receipt(workspace):
    harness, _ = make_harness(workspace, [])
    signature = call_signature("apply_patch", {"path": "src/app.py", "old_text": "a", "new_text": "b"})
    harness.state.mark_applied(signature, {"target": "src/app.py", "sha256": "abc"})
    request = ActionRequest(
        tool_call_id="call-x",
        tool_name="apply_patch",
        arguments={"path": "src/app.py", "old_text": "a", "new_text": "b"},
        step=1,
    )
    verdict = harness.gate.evaluate(request)
    assert verdict.decision == Decision.DENY
    assert verdict.rule == "duplicate_write"
    assert verdict.error.detail["first_result"]["sha256"] == "abc"


def test_gate_rejects_unconfigured_test_scope(workspace):
    harness, _ = make_harness(workspace, [])
    verdict = harness.gate.evaluate(
        ActionRequest(tool_call_id="c", tool_name="run_test", arguments={"scope": "nightly"}, step=1)
    )
    assert verdict.decision == Decision.DENY
    assert verdict.error.code in {"invalid_argument_value", "pre_check_failed"}


def test_s1_path_escape_is_denied_before_handler(workspace, monkeypatch):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="artifacts/../../.env", content="SECRET=1")],
        max_steps=3,
    )
    calls = spy_handler(monkeypatch, harness.runtime, "_do_write_file")
    result = harness.run("越界写入")

    assert calls == []
    assert not (workspace.parent / ".env").exists()
    assert result.status != "completed"
    decisions = [item for item in harness.audit.records() if item["event"] == "gate.decision"]
    assert decisions and decisions[0]["decision"] == Decision.DENY.value
    assert decisions[0]["rule"] == "sandbox"
    assert not [item for item in harness.audit.records() if item["event"] == "write.intent"]


def test_gate_rejects_forged_test_evidence_kind(workspace):
    harness, _ = make_harness(workspace, [])
    verdict = harness.gate.evaluate(
        ActionRequest(
            tool_call_id="c",
            tool_name="record_evidence",
            arguments={"scope": "target", "summary": "伪造", "kind": "test", "ok": True},
            step=1,
        )
    )
    assert verdict.decision == Decision.DENY
    assert verdict.error.code == "invalid_argument_value"
