"""step3 改写：只改目标小节，且不得改变行尾。"""

from __future__ import annotations

from pathlib import Path

from scripts.step3_update_verify import _restore_baseline, _snapshot_baseline, rewrite_section

DOC = (
    "# 登录流程\r\n"
    "\r\n"
    "## 5. 其他说明\r\n"
    "\r\n"
    "历史值 15 分钟，仅作对照。\r\n"
    "\r\n"
    "## 6. 会话有效期\r\n"
    "\r\n"
    "有效期为 **15 分钟**。\r\n"
    "超过 15 分钟后需要重新登录。\r\n"
)


def _target(tmp_path: Path) -> Path:
    path = tmp_path / "login-flow.md"
    path.write_bytes(DOC.encode("utf-8"))
    return path


def test_rewrite_only_touches_target_section(tmp_path: Path) -> None:
    path = _target(tmp_path)
    _, after, count = rewrite_section(path, "15 分钟", "20 分钟")

    assert count == 2
    assert "历史值 15 分钟" in after
    assert "有效期为 **20 分钟**" in after
    assert "超过 20 分钟后" in after
    assert path.read_bytes().decode("utf-8") == after


def test_rewrite_preserves_crlf(tmp_path: Path) -> None:
    path = _target(tmp_path)
    _, after, _ = rewrite_section(path, "15 分钟", "20 分钟")

    assert after.count("\r\n") == DOC.count("\r\n")
    assert path.read_bytes() == after.encode("utf-8")


def test_rewrite_is_idempotent(tmp_path: Path) -> None:
    path = _target(tmp_path)
    rewrite_section(path, "15 分钟", "20 分钟")
    _, after, count = rewrite_section(path, "15 分钟", "20 分钟")

    assert count == 0
    assert "历史值 15 分钟" in after


def test_snapshot_does_not_overwrite_baseline_with_new_value(tmp_path: Path) -> None:
    path = _target(tmp_path)
    baseline = tmp_path / ".baseline" / "login-flow.md"

    assert _snapshot_baseline(path, baseline) is True
    original = baseline.read_bytes()
    rewrite_section(path, "15 分钟", "20 分钟")

    assert _snapshot_baseline(path, baseline) is False
    assert baseline.read_bytes() == original


def test_restore_round_trip_preserves_bytes(tmp_path: Path) -> None:
    path = _target(tmp_path)
    baseline = tmp_path / ".baseline" / "login-flow.md"
    _snapshot_baseline(path, baseline)
    original = path.read_bytes()

    rewrite_section(path, "15 分钟", "20 分钟")
    assert "20 分钟" in path.read_text(encoding="utf-8")
    assert _restore_baseline(baseline, path) is True
    assert path.read_bytes() == original
