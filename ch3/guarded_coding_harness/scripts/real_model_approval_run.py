"""T3：真实模型 + 完整审批流程的端到端验证。

与 real_model_run.py 的区别：本脚本保持 require_write_approval=True，
因此会真实经历 run → waiting_approval → approve → resume 的完整链路
（含多次审批、暂停、对账等所有中间态）。

用法：
    python scripts/real_model_approval_run.py    # 产物写入 artifacts/real-model-approval-demo

脚本会自动加载同目录上一级的 .env（已存在的进程环境变量优先，不会被覆盖），
因此只要 .env 里写了 DEEPSEEK_API_KEY 即可直接运行，无需在终端里 export。

可选环境变量：
    HARNESS_REAL_MODEL   模型 id（默认 deepseek-v4-pro）
    HARNESS_REAL_BASE    API base（默认 https://api.deepseek.com/v1）
    HARNESS_MAX_ROUNDS   审批/恢复循环上限（默认 12）
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

DEMO_DIR = ROOT / "artifacts" / "real-model-approval-demo"
WORKSPACE = DEMO_DIR / "workspace"
RUN_ARTIFACTS = ("audit.jsonl", "audit.jsonl.head", "state.json", "checkpoints", "logs", "run-summary.md", "workspace")

ENV_CANDIDATES = (PROJECT / ".env", ROOT / ".env")


def load_env_file(paths: tuple[Path, ...]) -> Path | None:
    """加载第一个存在的 .env 文件；已存在的环境变量优先，不会被覆盖。

    支持 `KEY=VALUE`、`export KEY=VALUE`、单/双引号包裹的值与 # 注释行。
    """
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
        return path
    return None


TASK = (
    "任务：修复 src/app.py 中的 add 函数（当前返回 a - b，正确行为是 a + b）。"
    "请严格按以下步骤执行，不要调用 inspect_workspace，不要用相同参数重复调用同一工具：\n"
    "1. read_file(path=\"src/app.py\") 读取当前内容；\n"
    "2. write_file(path=\"src/app.py\", content=<把 return a - b 改为 return a + b 后的完整文件内容>)；\n"
    "3. run_test(scope=\"target\")；\n"
    "4. 若测试结果 ok=true，调用 final_answer(answer=\"已修复 add\")；否则按输出修正后重新 write_file。"
    "禁止修改 tests/ 目录，禁止在写入前运行测试。\n"
    "审批提示：若你收到「写操作需要人工审批」，说明该调用已被挂起；恢复后请原样重发"
    "同一条 write_file 调用（path 与 content 必须与之前完全一致），即可获得已批准的放行。"
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


def drive(harness: GuardedHarness, task: str, max_rounds: int = 12) -> dict:
    """跑一步、按终态人工介入（审批/对账/恢复），直到终态或轮次耗尽。

    返回阶段记录，便于在 run-summary.md 中复盘完整审批链路。
    """
    phases: list[dict] = []
    approvals: list[dict] = []
    approved_ids: set[str] = set()
    try:
        result = harness.run(task)
    except Exception as exc:  # 例如上次运行进程退出未确认
        return {"result": None, "error": f"{type(exc).__name__}: {exc}", "phases": phases, "approvals": approvals}

    for _ in range(max_rounds):
        pending = harness.state.pending_items()
        phases.append(
            {
                "status": result.status,
                "stop_reason": result.stop_reason,
                "step": result.step,
                "tool_calls": result.tool_calls,
                "writes": result.writes,
                "test_runs": result.test_runs,
                "pending": [item.get("tool_call_id") for item in pending],
                "message": result.message,
            }
        )
        try:
            if result.status == "waiting_approval":
                for item in pending:
                    # 已批准但尚未被 resume 消耗的项不要重复批准，否则会刷新 granted_step/TTL。
                    if item["tool_call_id"] in approved_ids:
                        continue
                    approval = harness.approve(
                        item["tool_call_id"],
                        actor="human",
                        reason="端到端脚本自动批准",
                    )
                    approved_ids.add(approval.tool_call_id)
                    approvals.append(
                        {
                            "tool_call_id": approval.tool_call_id,
                            "signature": approval.signature,
                            "target": item.get("target", ""),
                            "granted_step": approval.granted_step,
                            "ttl_steps": approval.ttl_steps,
                            "fingerprint": approval.fingerprint,
                        }
                    )
                result = harness.resume()
            elif result.status == "paused":
                result = harness.resume()
            elif result.status == "waiting_reconcile":
                outcome = harness.reconcile()
                phases.append({"reconcile": outcome})
                result = harness.resume()
            else:
                break
        except Exception as exc:
            phases.append({"status": "exception", "error": f"{type(exc).__name__}: {exc}"})
            return {"result": result, "error": f"{type(exc).__name__}: {exc}", "phases": phases, "approvals": approvals}
    return {"result": result, "phases": phases, "approvals": approvals}


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
    blocked = state.get("blocked") or {}
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
        "# 真实模型 + 完整审批流程运行记录（T3）",
        "",
        f"- run_id: `{state.get('run_id')}`",
        f"- model: `{os.environ.get('HARNESS_REAL_MODEL', 'deepseek-v4-pro')}`",
        f"- task: {state.get('task')}",
        f"- 终态: status={status}, stop_reason={stop_reason}, step={step}, tool_calls={tool_calls}",
        *([f"- 异常: {info['error']}"] if info.get("error") else []),
        *([f"- 阻断: {blocked}"] if blocked else []),
        f"- 预算 used/limits: {budget.get('used')} / {budget.get('limits')}",
        f"- 最终文件 sha256: `{digest(final_text)}`",
        "",
        "## 审批记录（人工 approve）",
        "",
        *(
            [
                "| tool_call_id | target | granted_step | ttl_steps | signature |",
                "|---|---|---|---|---|",
                *[
                    f"| {item['tool_call_id']} | {item.get('target')} | {item.get('granted_step')} | "
                    f"{item.get('ttl_steps')} | `{str(item.get('signature'))[:16]}...` |"
                    for item in info.get("approvals", [])
                ],
            ]
            if info.get("approvals")
            else ["（本次运行未触发审批）"]
        ),
        "",
        "## 阶段（run / approve / resume 链路）",
        "",
        *[f"- {item}" for item in info.get("phases", [])],
        "",
        "## 计划",
        "",
        *[f"{i}. {item}" for i, item in enumerate(state.get("plan", {}).get("items", []), start=1)],
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
    loaded = load_env_file(ENV_CANDIDATES)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY，无法执行真实模型 + 审批端到端验证。")
        print(f"请在 {PROJECT / '.env'} 中写入 DEEPSEEK_API_KEY=sk-xxxx，或通过环境变量提供。")
        return 2
    if loaded:
        print(f"已加载环境变量文件: {loaded}")

    model_id = os.environ.get("HARNESS_REAL_MODEL", "deepseek-v4-pro")
    api_base = os.environ.get("HARNESS_REAL_BASE", "https://api.deepseek.com/v1")
    max_rounds = int(os.environ.get("HARNESS_MAX_ROUNDS", "12"))

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
    # 关键差异：require_write_approval=True，写操作必须经人工审批。
    # approval_ttl_steps 放宽，允许模型在恢复后经过少量只读步骤再重发同一写入。
    policy = HarnessPolicy(
        required_scopes=("target",),
        max_steps=20,
        max_tool_calls=60,
        max_writes=8,
        max_test_runs=5,
        max_seconds=600,
        repeat_limit=4,
        approval_ttl_steps=8,
        require_write_approval=True,
        suites={"target": ["tests/test_target.py"]},
    )
    harness = GuardedHarness(WORKSPACE, model, policy=policy, harness_dir=str(DEMO_DIR))

    info = drive(harness, TASK, max_rounds=max_rounds)
    result = info["result"]
    final_text = (WORKSPACE / "src" / "app.py").read_text(encoding="utf-8")
    summary = render_summary(harness, info, final_text, initial_text)
    (DEMO_DIR / "run-summary.md").write_text(summary, encoding="utf-8")

    status = result.status if result else "failed"
    print(f"run_id={harness.run_id} status={status} stop_reason={result.stop_reason if result else ''}")
    print(f"approvals={len(info.get('approvals', []))} rounds={len(info.get('phases', []))}")
    print(f"summary={DEMO_DIR / 'run-summary.md'}")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
