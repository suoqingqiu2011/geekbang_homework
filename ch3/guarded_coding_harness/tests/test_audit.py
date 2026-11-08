"""A1–A2：审计哈希链与溯源。"""

from __future__ import annotations

import json

import pytest

from audit import AuditAnchorError, AuditLog
from conftest import SAMPLE_SOURCE_PATCHED, call, make_harness


def test_a1_denied_call_is_traceable_with_rule_and_actor(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        "gate.decision",
        "run-1",
        tool_call_id="call-1",
        actor="harness",
        decision="deny",
        rule="repeat_limit",
        detail={"tool": "read_file"},
    )
    trace = log.trace("call-1")
    assert len(trace) == 1
    assert trace[0]["rule"] == "repeat_limit"
    assert trace[0]["actor"] == "harness"
    assert trace[0]["tool_call_id"] == "call-1"
    ok, message = log.verify()
    assert ok, message


def test_a2_run_reconstruction_and_hash_chain(workspace):
    write = call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)
    write.id = "call-a2-write"
    harness, model = make_harness(workspace, [write], max_steps=6)
    first = harness.run("A2 全链路")
    assert first.status == "waiting_approval"
    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认")

    replay = call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)
    replay.id = "call-a2-write-replay"
    test_call = call("run_test", scope="target")
    test_call.id = "call-a2-test"
    final = call("final_answer", answer="已修复")
    final.id = "call-a2-final"
    model.script.extend([replay, test_call, final])

    second = harness.resume()
    assert second.status == "completed", second.message

    records = harness.audit.by_run(harness.run_id)
    events = [item["event"] for item in records]
    for expected in (
        "run.start",
        "gate.decision",
        "gate.batch",
        "approval.granted",
        "write.intent",
        "write.applied",
        "test.intent",
        "test.result",
        "run.halt",
    ):
        assert expected in events, expected
    assert [item["seq"] for item in records] == list(range(1, len(records) + 1))

    traced = {item["event"] for item in harness.audit.trace(pending["tool_call_id"])}
    assert {"gate.decision", "approval.granted"} <= traced

    applied = next(item for item in records if item["event"] == "write.applied")
    assert applied["tool_call_id"] == "call-a2-write-replay"
    tested = next(item for item in records if item["event"] == "test.result")
    assert tested["tool_call_id"] == "call-a2-test"

    ok, message = harness.audit.verify()
    assert ok, message


def test_step_error_is_audited_when_gate_denies(workspace):
    harness, _ = make_harness(
        workspace,
        [call("write_file", path="artifacts/../../.env", content="SECRET=1")],
        max_steps=3,
    )
    result = harness.run("越界写入")
    assert result.status != "completed"
    errors = [item for item in harness.audit.records() if item["event"] == "step.error"]
    assert errors
    assert errors[0]["decision"] == "deny"
    assert errors[0]["rule"] == "sandbox"
    assert errors[0]["tool_call_id"] == "call-001"
    assert errors[0]["detail"]["error"]["code"] == "sandbox_violation"
    ok, message = harness.audit.verify()
    assert ok, message


def test_tampering_breaks_verification(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append("gate.decision", "run-1", tool_call_id="c1", decision="deny", rule="repeat_limit")
    log.append("gate.decision", "run-1", tool_call_id="c2", decision="allow", rule="allowed")

    lines = path.read_text(encoding="utf-8").splitlines()
    target_index = next(index for index, line in enumerate(lines) if "gate.decision" in line)
    body = json.loads(lines[target_index])
    body["decision"] = "allow"
    lines[target_index] = json.dumps(body, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fresh = AuditLog(path)
    ok, message = fresh.verify()
    assert not ok
    assert "哈希校验失败" in message


def test_truncated_or_emptied_audit_is_detected(tmp_path):
    """尾部截断/清空会留下与头锚点不一致的长度，verify 必须拒绝。"""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for index in range(3):
        log.append("gate.decision", "run-1", tool_call_id=f"c{index}", decision="deny", rule="budget")
    lines = path.read_text(encoding="utf-8").splitlines()

    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    ok, message = AuditLog(path).verify()
    assert not ok
    assert "截断" in message
    # 截断后也不允许静默续写，否则会洗掉篡改痕迹。
    with pytest.raises(AuditAnchorError):
        AuditLog(path).append("gate.decision", "run-1", tool_call_id="late")

    path.write_text("", encoding="utf-8")
    ok, message = AuditLog(path).verify()
    assert not ok
    assert "清空" in message
