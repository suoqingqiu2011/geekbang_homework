"""formatting：报告写入失败时降级为屏幕输出，不中断主流程。"""

from __future__ import annotations

from pathlib import Path

from rag_core.formatting import Tee


class _BrokenHandle:
    def write(self, text: str) -> int:
        raise OSError("disk full")

    def flush(self) -> None:
        raise OSError("disk full")

    def close(self) -> None:
        pass


def test_tee_degrades_to_stdout_when_write_fails(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: _BrokenHandle())
    tee = Tee(tmp_path / "report.md")

    tee("第一行")
    tee("第二行")
    captured = capsys.readouterr()

    assert "第一行" in captured.out and "第二行" in captured.out
    assert captured.err.count("报告写入失败") == 1
    tee.close()
