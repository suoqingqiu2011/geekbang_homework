"""T2：用真实模型（DeepSeek OpenAI 兼容接口）跑通一次受控修复任务。

用法：
    set DEEPSEEK_API_KEY=...            # 必需
    python scripts/real_model_run.py    # 产物写入 artifacts/real-model-demo

可选环境变量：
    HARNESS_REAL_MODEL   模型 id（默认 deepseek-v4-pro）
    HARNESS_REAL_BASE    API base（默认 https://api.deepseek.com/v1）
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(ROOT / "smolagents" / "src"))
sys.path.insert(0, str(PROJECT / "tests"))

from smolagents.models import REMOVE_PARAMETER, OpenAIModel  # noqa: E402

from conftest import SAMPLE_SOURCE, SAMPLE_TEST  # noqa: E402
from harness import GuardedHarness, HarnessPolicy  # noqa: E402

DEMO_DIR = ROOT / "artifacts" / "real-model-demo"
WORKSPACE = DEMO_DIR / "workspace"
RUN_ARTIFACTS = ("audit.jsonl", "state.json", "checkpoints", "logs", "run-summary.md")

TASK = (
    "任务：修复 src/app.py 中的 add 函数（当前返回 a - b，正确行为是 a + b）。"
    "请严格按以下步骤执行，不要调用 inspect_workspace，不要用相同参数重复调用同一工具：\n"
    "1. read_file(path=\"src/app.py\") 读取当前内容；\n"
    "2. 先 write_file(path=\"src/app.py\", content=<把 return a - b 改为 return a + b 后的完整文件内容>)；\n"
    "3. 写入成功后再 run_test(scope=\"target\")；\n"
    "4. 若测试结果 ok=true，调用 final_answer(answer=\"已修复 add\")；否则按输出修正后重新 write_file。"
    "禁止修改 tests/ 目录，禁止在写入前运行测试。"
)


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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def drive(harness: GuardedHarness, task: str) -> dict:
    """跑一步、审批、再跑，直到终态；返回阶段记录。"""
    phases: list[dict] = []
    try:
        result = harness.run(task)
    except Exception as exc:  # 生成失败也要留下阶段记录
        return {"result": None, "error": f"{type(exc).__name__}: {exc}", "phases": phases}
    for _ in range(20):
        phases.append(
            {
                "status": result.status,
                "stop_reason": result.stop_reason,
                "step": result.step,
                "pending": [item.get("tool_call_id") for item in harness.state.pending_items()],
            }
        )
        try:
            if result.status == "waiting_approval":
                for item in harness.state.pending_items():
                    harness.approve(item["tool_call_id"], actor="human", reason="真实模型 demo 自动审批")
                result = harness.resume()
            elif result.status == "paused":
                result = harness.resume()
            elif result.status == "waiting_reconcile":
                harness.reconcile()
                result = harness.resume()
            else:
                break
        except Exception as exc:
            phases.append({"status": "exception", "error": f"{type(exc).__name__}: {exc}"})
            return {"result": result, "error": f"{type(exc).__name__}: {exc}", "phases": phases}
    return {"result": result, "phases": phases}


def last_log() -> tuple[str, list[str]]:
    logs = sorted((DEMO_DIR / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return "", []
    text = logs[-1].read_text(encoding="utf-8", errors="replace")
    return logs[-1].name, text.splitlines()[-30:]


def render_summary(harness: GuardedHarness, info: dict, final_text: str, initial_text: str) -> str:
    state = json.loads((DEMO_DIR / "state.json").read_text(encoding="utf-8"))
    result = info.get("result")
    status = result.status if result else state.get("status")
    stop_reason = result.stop_reason if result else state.get("stop_reason")
    step = result.step if result else state.get("step")
    tool_calls = result.tool_calls if result else (state.get("budget") or {}).get("used", {}).get("tool_calls")
    audit = harness.audit.by_run(harness.run_id)
    evidence = state.get("evidence") or []
    budget = state.get("budget") or {}
    log_name, log_tail = last_log()
    diff = list(
        difflib.unified_diff(
            initial_text.splitlines(),
            final_text.splitlines(),
            fromfile="a/src/app.py",
            tofile="b/src/app.py",
            lineterm="",
        )
    )
    lines = [
        "# 真实模型运行记录（T2）",
        "",
        f"- run_id: `{state.get('run_id')}`",
        f"- model: `{os.environ.get('HARNESS_REAL_MODEL', 'deepseek-v4-pro')}`",
        f"- task: {state.get('task')}",
        f"- 终态: status={status}, stop_reason={stop_reason}, step={step}, tool_calls={tool_calls}",
        *([f"- 异常: {info['error']}"] if info.get("error") else []),
        f"- 预算 used/limits: {budget.get('used')} / {budget.get('limits')}",
        f"- 最终文件 sha256: `{digest(final_text)}`",
        "",
        "## 计划",
        "",
        *[f"{i}. {item}" for i, item in enumerate(state.get("plan", {}).get("items", []), start=1)],
        "",
        "## 阶段",
        "",
        *[f"- {item}" for item in info["phases"]],
        "",
        "## 证据",
        "",
        *[
            f"- kind={item.get('kind')}, scope={item.get('scope')}, "
            f"returncode={item.get('payload', {}).get('returncode')}, "
            f"fingerprint={item.get('fingerprint')}, payload={json.dumps(item.get('payload'), ensure_ascii=False)}"
            for item in evidence
        ],
        "",
        "## Diff",
        "",
        "```diff",
        *(diff or ["（模型未修改 src/app.py）"]),
        "```",
        "",
        "## 测试记录",
        "",
        f"- 日志文件: `{log_name}`",
        "```text",
        *log_tail,
        "```",
        "",
        "## 审计时间线",
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
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY，无法执行真实模型验证。")
        return 2
    model_id = os.environ.get("HARNESS_REAL_MODEL", "deepseek-v4-pro")
    api_base = os.environ.get("HARNESS_REAL_BASE", "https://api.deepseek.com/v1")

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    clean_old_run()
    build_workspace()
    initial_text = (WORKSPACE / "src" / "app.py").read_text(encoding="utf-8")

    # DeepSeek 思考模式不接受 tool_choice，移除该参数（工具仍可正常调用）。
    model = OpenAIModel(
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        temperature=0.0,
        tool_choice=REMOVE_PARAMETER,
    )
    # 审批→resume 的中断恢复已由 artifacts/harness-demo 覆盖；此处让真实模型连续执行，
    # 避免模型在审批停顿后改变动作顺序（先测后写）导致证据失效。
    policy = HarnessPolicy(
        required_scopes=("target",),
        max_steps=20,
        max_tool_calls=60,
        max_writes=8,
        max_test_runs=5,
        max_seconds=600,
        repeat_limit=4,
        require_write_approval=False,
        suites={"target": ["tests/test_target.py"]},
    )
    harness = GuardedHarness(WORKSPACE, model, policy=policy, harness_dir=str(DEMO_DIR))

    info = drive(harness, TASK)
    result = info["result"]
    final_text = (WORKSPACE / "src" / "app.py").read_text(encoding="utf-8")
    summary = render_summary(harness, info, final_text, initial_text)
    (DEMO_DIR / "run-summary.md").write_text(summary, encoding="utf-8")

    status = result.status if result else "failed"
    print(f"run_id={harness.run_id} status={status} stop_reason={result.stop_reason if result else ''}")
    print(f"summary={DEMO_DIR / 'run-summary.md'}")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
