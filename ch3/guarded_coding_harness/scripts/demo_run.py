"""重生成 artifacts/harness-demo 的完整运行记录（审批 → resume → completed）。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(ROOT / "smolagents" / "src"))
sys.path.insert(0, str(PROJECT / "tests"))

from conftest import SAMPLE_SOURCE, SAMPLE_SOURCE_PATCHED, SAMPLE_TEST, ScriptedModel, call  # noqa: E402
from harness import GuardedHarness, HarnessPolicy  # noqa: E402

DEMO_DIR = ROOT / "artifacts" / "harness-demo"
WORKSPACE = DEMO_DIR / "workspace"
RUN_ARTIFACTS = ("audit.jsonl", "audit.jsonl.head", "state.json", "checkpoints", "logs", "run-summary.md")


def build_workspace() -> None:
    (WORKSPACE / "src").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "tests").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "artifacts").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "src" / "__init__.py").write_text("", encoding="utf-8")
    (WORKSPACE / "src" / "app.py").write_text(SAMPLE_SOURCE, encoding="utf-8")
    (WORKSPACE / "tests" / "test_target.py").write_text(SAMPLE_TEST, encoding="utf-8")


def clean_old_run() -> None:
    for name in RUN_ARTIFACTS:
        target = DEMO_DIR / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def render_summary(harness: GuardedHarness, first, second) -> str:
    state = json.loads((DEMO_DIR / "state.json").read_text(encoding="utf-8"))
    audit = harness.audit.by_run(harness.run_id)
    snapshots = harness.checkpoints.list_snapshots()
    evidence = state.get("evidence") or []
    budget = state.get("budget") or {}
    lines = [
        "# Harness 运行记录（demo）",
        "",
        f"- run_id: `{state.get('run_id')}`",
        f"- task: {state.get('task')}",
        f"- plan revision: {state.get('plan', {}).get('revision')}",
        f"- 首次运行: status={first.status}, stop_reason={first.stop_reason}",
        f"- 恢复运行: status={second.status}, stop_reason={second.stop_reason}",
        f"- 证据: {[item.get('kind') + ':' + str(item.get('scope')) for item in evidence]}",
        f"- 预算: used={budget.get('used')} / limits={budget.get('limits')}",
        "",
        "## 计划",
        "",
        *[f"{index}. {item}" for index, item in enumerate(state.get("plan", {}).get("items", []), start=1)],
        "",
        "## Checkpoints（按 run 隔离）",
        "",
        *[f"- `{path.relative_to(DEMO_DIR).as_posix()}`" for path in snapshots],
        "",
        "## 审计事件时间线",
        "",
        "| seq | event | decision | rule | tool_call_id |",
        "|---|---|---|---|---|",
        *[
            f"| {item['seq']} | {item['event']} | {item.get('decision')} | {item.get('rule')} | {item.get('tool_call_id')} |"
            for item in audit
        ],
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    clean_old_run()
    build_workspace()

    policy = HarnessPolicy(
        required_scopes=("target",),
        max_steps=8,
        max_writes=3,
        max_test_runs=3,
        max_seconds=120,
        suites={"target": ["tests/test_target.py"]},
    )
    model = ScriptedModel(
        [
            call(
                "update_plan",
                items=["复现 add 缺陷", "修复 src/app.py", "运行 target 测试", "提交"],
            ),
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
        ]
    )
    harness = GuardedHarness(WORKSPACE, model, policy=policy, harness_dir=str(DEMO_DIR))

    first = harness.run("修复 add 函数")
    if first.status != "waiting_approval":
        raise SystemExit(f"演示未停在审批：{first.status} {first.message}")
    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="human", reason="演示审批")

    model.script.extend(
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("run_test", scope="target"),
            call("final_answer", answer="已修复 add，并完成 target 测试"),
        ]
    )
    second = harness.resume()
    if second.status != "completed":
        raise SystemExit(f"演示恢复未完成：{second.status} {second.message}")

    (DEMO_DIR / "run-summary.md").write_text(render_summary(harness, first, second), encoding="utf-8")
    print(f"run_id={harness.run_id} status={second.status} step={second.step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
