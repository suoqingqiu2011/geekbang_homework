"""S1–S4：路径、命令、超时与取消。"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from conftest import make_harness
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
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("top secret", encoding="utf-8")
    link = workspace / "artifacts" / "linked"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside_dir), str(link))
        else:
            os.symlink(outside_dir, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建链接逃逸场景")
    sandbox = Sandbox(workspace, writable=("artifacts", "src"))
    with pytest.raises(SandboxViolation):
        sandbox.resolve("artifacts/linked/secret.txt", mode="read")


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


def test_walk_based_read_tools_do_not_expose_sensitive_files(workspace):
    (workspace / ".env").write_text("DEEPSEEK_API_KEY=leak-me\n", encoding="utf-8")
    (workspace / "id_rsa").write_text("PRIVATE-KEY-MATERIAL\n", encoding="utf-8")
    harness, _ = make_harness(workspace, [])

    listing = harness.runtime.invoke("list_files", {"path": "."})
    assert ".env" not in listing
    assert "id_rsa" not in listing

    leaked = harness.runtime.invoke("search_code", {"pattern": "PRIVATE-KEY-MATERIAL", "path": "."})
    assert "PRIVATE-KEY-MATERIAL" not in leaked
    assert "leak-me" not in leaked

    normal = harness.runtime.invoke("search_code", {"pattern": "return a - b", "path": "src"})
    assert "src/app.py" in normal


def test_walk_based_read_tools_skip_link_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("ESCAPED-SECRET", encoding="utf-8")
    link = workspace / "artifacts" / "linked"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        else:
            os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建链接逃逸场景")

    harness, _ = make_harness(workspace, [])
    found = harness.runtime.invoke("search_code", {"pattern": "ESCAPED-SECRET", "path": "."})
    assert "ESCAPED-SECRET" not in found
    listing = harness.runtime.invoke("list_files", {"path": "artifacts"})
    assert "linked/secret.txt" not in listing


def test_workspace_fingerprint_excludes_runtime_artifacts(workspace):
    before = workspace_fingerprint(workspace)
    (workspace / "artifacts" / "note.txt").write_text("noise", encoding="utf-8")
    (workspace / "artifacts" / ".harness").mkdir(exist_ok=True)
    (workspace / "artifacts" / ".harness" / "x.json").write_text("{}", encoding="utf-8")
    assert workspace_fingerprint(workspace) == before
    (workspace / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert workspace_fingerprint(workspace) != before


def test_workspace_fingerprint_only_skips_root_artifacts(workspace):
    """深层同名目录不享受跳过：src/artifacts 的变化必须体现在指纹里。"""
    (workspace / "src" / "artifacts").mkdir(parents=True, exist_ok=True)
    nested = workspace / "src" / "artifacts" / "state.py"
    nested.write_text("value = 1\n", encoding="utf-8")
    before = workspace_fingerprint(workspace)
    nested.write_text("value = 2\n", encoding="utf-8")
    assert workspace_fingerprint(workspace) != before
    # 根级 artifacts 依然被跳过。
    stable = workspace_fingerprint(workspace)
    (workspace / "artifacts" / "trace.log").write_text("noise", encoding="utf-8")
    assert workspace_fingerprint(workspace) == stable
