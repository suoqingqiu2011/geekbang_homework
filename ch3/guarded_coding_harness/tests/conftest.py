"""共享 fixture：脚本化模型与临时工作区。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
)

SAMPLE_SOURCE = "def add(a, b):\n    return a - b\n"
SAMPLE_SOURCE_PATCHED = "def add(a, b):\n    return a + b\n"
SAMPLE_SOURCE_BROKEN = "def add(a, b):\n    return 0\n"

SAMPLE_TEST = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import add


def test_add():
    assert add(2, 3) == 5
"""

SLOW_TEST = """import time


def test_slow():
    time.sleep(30)
"""


@dataclass
class ScriptedCall:
    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""


def call(name: str, **arguments) -> ScriptedCall:
    return ScriptedCall(name=name, arguments=arguments)


class ScriptedModel(Model):
    """按脚本逐轮返回工具调用；脚本耗尽后返回纯文本消息。"""

    def __init__(self, script: list | None = None) -> None:
        super().__init__(model_id="scripted")
        self.script: list = list(script or [])
        self._counter = 0
        self.generate_count = 0

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        self.generate_count += 1
        if not self.script:
            return ChatMessage(role=MessageRole.ASSISTANT, content="继续。")
        item = self.script.pop(0)
        if item is None:
            return ChatMessage(role=MessageRole.ASSISTANT, content="继续。")
        if isinstance(item, str):
            return ChatMessage(role=MessageRole.ASSISTANT, content=item)
        calls = list(item) if isinstance(item, (list, tuple)) else [item]
        tool_calls = []
        for spec in calls:
            self._counter += 1
            tool_calls.append(
                ChatMessageToolCall(
                    function=ChatMessageToolCallFunction(
                        name=spec.name,
                        arguments=dict(spec.arguments),
                    ),
                    id=spec.id or f"call-{self._counter:03d}",
                    type="function",
                )
            )
        return ChatMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=tool_calls)

    def parse_tool_calls(self, message):
        # 纯文本消息保持无工具调用，交给 Harness 的空批次分支处理。
        return message


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "artifacts").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(SAMPLE_SOURCE, encoding="utf-8")
    for name in ("test_target.py", "test_boundary.py", "test_regression.py"):
        (root / "tests" / name).write_text(SAMPLE_TEST, encoding="utf-8")
    (root / "tests" / "test_slow.py").write_text(SLOW_TEST, encoding="utf-8")
    (root / "artifacts" / "sleep.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    return root


def make_harness(workspace: Path, script: list | None = None, **policy_kwargs):
    from harness import GuardedHarness, HarnessPolicy

    model = ScriptedModel(script)
    policy = HarnessPolicy(**policy_kwargs)
    return GuardedHarness(workspace, model, policy=policy), model


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def child_env(workspace: Path) -> dict[str, str]:
    here = Path(__file__).resolve()
    src = here.parents[1] / "src"
    smol = here.parents[2] / "smolagents" / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(src), str(smol), str(workspace), str(here.parent)])
    return env
