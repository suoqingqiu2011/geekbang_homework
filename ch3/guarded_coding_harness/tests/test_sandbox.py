"""S1–S4：路径、命令、超时与取消。"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from sandbox import (
    CommandResult,
    Sandbox,
    SandboxViolation,
    is_process_alive,
    workspace_fingerprint,
)


def test_s1_dotdot_escape_and_sensitive_targets_are_rejected(workspace):
    sandbox = Sandbox(workspace, writable=("artifacts", "src"))
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts/../../.env", mode="read")
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts/../../../outside.txt", mode="read")
    with pytest.raises(SandboxViolation):
        sandbox.resolve(".ssh/id_rsa", mode="read")
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts/cert.pem", mode="read")


def test_s2_writable_zones_and_prefix_impostors(workspace):
    sandbox = Sandbox(workspace, writable=("artifacts", "src"))
    with pytest.raises(SandboxViolation):
        sandbox.resolve("tests/test_target.py", mode="write")
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts-evil/payload.txt", mode="write")
    target = sandbox.resolve("src/app.py", mode="write")
    assert target == (workspace / "src" / "app.py").resolve()


def test_s2_symlink_escape_is_rejected_when_supported(workspace, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = workspace / "artifacts" / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    sandbox = Sandbox(workspace, writable=("artifacts", "src"))
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts/link.txt", mode="read")


def test_s3_commands_outside_whitelist_and_metachars_are_rejected(workspace):
    sandbox = Sandbox(workspace)
    with pytest.raises(SandboxViolation):
        sandbox.check_command(["rm", "-rf", "src"])
    with pytest.raises(SandboxViolation):
        sandbox.check_command(["python", "-c", "import os; os.remove('x')"])
    with pytest.raises(SandboxViolation):
        sandbox.check_command(["git", "push"])
    sandbox.check_command(["git", "status"])
    sandbox.check_command([sys.executable, "-m", "pytest", "-q", "tests/test_target.py"])


def test_s3_out_of_zone_cwd_is_rejected(workspace):
    sandbox = Sandbox(workspace)
    with pytest.raises(SandboxViolation):
        sandbox.run(["pwd"], cwd=workspace.parent)


def test_s4_timeout_kills_process_tree(workspace):
    sandbox = Sandbox(workspace, timeout=1.0)
    result = sandbox.run([sys.executable, str(workspace / "artifacts" / "sleep.py")], timeout=1.0)
    assert isinstance(result, CommandResult)
    assert result.timed_out is True
    assert result.terminated is True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and is_process_alive(result.pid):
        time.sleep(0.1)
    assert not is_process_alive(result.pid)
    assert os.path.isfile(result.log_path)


def test_s4_cancel_kills_process_group(workspace):
    sandbox = Sandbox(workspace, timeout=10.0)
    timer = threading.Timer(0.8, sandbox.request_cancel)
    timer.start()
    result = sandbox.run([sys.executable, str(workspace / "artifacts" / "sleep.py")], timeout=10.0)
    timer.cancel()
    assert result.cancelled is True
    assert result.terminated is True
    assert not is_process_alive(result.pid)
    sandbox.reset_cancel()


def test_workspace_fingerprint_excludes_runtime_artifacts(workspace):
    before = workspace_fingerprint(workspace)
    (workspace / "artifacts" / "note.txt").write_text("noise", encoding="utf-8")
    (workspace / "artifacts" / ".harness").mkdir(exist_ok=True)
    (workspace / "artifacts" / ".harness" / "x.json").write_text("{}", encoding="utf-8")
    assert workspace_fingerprint(workspace) == before
    (workspace / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert workspace_fingerprint(workspace) != before
