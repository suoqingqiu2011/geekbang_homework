# 修复 F-1 ~ F-5：交付证据与运行时一致性

## 背景

第二轮复核在 `c:\projet\homework\ch3\guarded_coding_harness` 上发现 F-1 ~ F-5 五项未达标：

- **F-1** `pytest -q` 实际输出未落盘，T1 提交证据缺失。
- **F-2** `artifacts/harness-demo/` 的 state 无 `plan` 字段、checkpoints 为扁平结构，与新实现不一致。
- **F-3** S1 缺少 Gate 级"越界路径不进入 handler"的用例。
- **F-4** A2 用例伪造 `audit` 中的 `sandbox.command` 事件；SPEC §12.1 的 `step.error` 事件未实现。
- **F-5** `resume()` 未清除 `_pause_requested` / `_cancel_requested` / sandbox 取消事件，暂停后 resume 会立即再次停机；`test_pause_is_resumable_but_cancel_is_not` 名不副实。

## 变更方案

### F-5 `src/harness.py`：`GuardedHarness.resume()`

在"正常恢复"分支（`plan.action == resume` 通过之后、`_execute` 之前）加入：

```python
self._pause_requested = False
self._cancel_requested = False
self.sandbox.reset_cancel()
self._last_stop = None
```

仅在此分支执行；`refuse` / `reconcile` 分支保持原样。`run()` 已清 `_pause_requested`，不改动。

### F-4 `src/harness.py`：`GuardedAgent.process_tool_calls` 与 `step.error`

1. `DENY` 分支在抛异常前追加审计：

```python
harness.audit.append(
    "step.error",
    harness.run_id,
    tool_call_id=request.tool_call_id,
    actor="harness",
    decision="deny",
    rule=verdict.rule or (verdict.error.code if verdict.error else "denied"),
    detail={
        "tool": request.tool_name,
        "step": harness.current_step,
        "error": verdict.error.to_dict() if verdict.error else {"code": "denied"},
    },
)
```

2. `yield from super().process_tool_calls(...)` 外层包 `try/except Exception`，记录后原样抛出：

```python
try:
    yield from super().process_tool_calls(chat_message, memory_step)
except Exception as exc:
    harness.audit.append(
        "step.error",
        harness.run_id,
        tool_call_id=requests[0].tool_call_id if requests else None,
        actor="harness",
        decision="error",
        rule=type(exc).__name__,
        detail={"message": str(exc), "step": harness.current_step},
    )
    raise
```

需从 `smolagents.utils` 追加导入 `AgentError`（若按异常类型区分记录，可保留单一 `except Exception`）。

### F-4 `tests/test_audit.py`

- 重写 `test_a2_run_reconstruction_and_hash_chain`：用 `make_harness` 跑一次真实 run（write → waiting_approval → approve → resume → run_test → final_answer），断言：
  - `audit.by_run(run_id)` 包含 `run.start`、`gate.decision`、`gate.batch`、`approval.granted`、`write.intent`、`write.applied`、`test.intent`、`test.result`、`run.halt`；
  - `seq` 从 1 连续递增；`audit.verify()` 通过；
  - `audit.trace(pending_tool_call_id)` 包含 `gate.decision` 与 `approval.granted`；
  - `write.applied` / `test.result` 的 `tool_call_id` 可溯源到对应调用。
- 新增 `test_step_error_is_audited_when_gate_denies`：越界写入被 Gate 拒绝后，审计中存在 `step.error`（`decision=deny`、`rule=sandbox`），且哈希链校验通过。
- 删除伪造的 `sandbox.command` 事件（实现不产生该事件）。

### F-3 `tests/test_gate.py`

新增 `test_s1_path_escape_is_denied_before_handler`：

- 脚本：`call("write_file", path="artifacts/../../.env", content="SECRET=1")`；
- 用 `spy_handler` 监视 `harness.runtime._do_write_file`，断言调用次数为 0；
- 断言 `tmp_path/.env` 未被创建；
- 断言审计中存在 `gate.decision` 且 `decision=deny`、`rule=sandbox`；
- 断言不存在 `write.intent` 事件；`result.status != "completed"`。

### F-5 测试：`tests/test_harness_loop.py`

- 将 `test_pause_is_resumable_but_cancel_is_not` 拆为：
  1. `test_pause_is_resumable`：`write_file` 脚本 → run 停在 `waiting_approval` → `harness.pause()`（`status=paused`）→ `harness.approve(pending_id)` → 追加 `write_file`/`run_test`/`final_answer` 脚本 → `harness.resume()` → `completed`，文件已修复；断言 `harness._pause_requested is False`、`harness.sandbox.cancel_requested is False`。
  2. `test_cancelled_run_cannot_be_revived`：保留原"取消后 `reset_cancel()` 抛 `PolicyDenied`"断言。

### F-1 归档 pytest 输出

在 `c:\projet\homework\ch3\guarded_coding_harness` 下运行：

```powershell
python -m pytest -q 2>&1 | Tee-Object -FilePath ..\artifacts\pytest-q.txt
```

要求退出码为 0、输出含完整用例统计；文件作为提交证据保留。

### F-2 重生成 demo

新增 `guarded_coding_harness/scripts/demo_run.py`（唯一新增源文件）：

- 复用 `tests/conftest.py` 的 `ScriptedModel`、`call`、`SAMPLE_SOURCE(_PATCHED)`、`SAMPLE_TEST`；
- 在 `artifacts/harness-demo/workspace` 重建演示工作区（`src/app.py`、`tests/test_target.py`）；
- harness 目录指向 `artifacts/harness-demo`，脚本序列：`update_plan` → `write_file` → `run_test` → `final_answer`；
- 先 run 到 `waiting_approval`，`approve()` 后 `resume()` 至 `completed`；
- 运行后生成 `artifacts/harness-demo/run-summary.md`（run_id、plan、审批、步骤预算、证据、checkpoint 列表、审计事件时间线）。

重跑前清理旧的 `harness-demo` 运行产物（audit.jsonl、state.json、checkpoints、logs），保留目录本身。

## 验证

1. `python -m pytest -q` 全绿，输出归档到 `artifacts/pytest-q.txt`。
2. 全仓检查：`state.json` 含非空 `plan`；`checkpoints/<run_id>/` 存在且包含 `halt-approval_required` / `approval` / `run_end` 快照。
3. `audit.jsonl` 中 `step.error` 与 `resume.plan` 均可在真实用例中复现；`audit.verify()` 通过。
4. 暂停后 `resume()` 能继续并完成，且未触发 `paused` 二次停机。

## 影响范围

- 修改：`src/harness.py`、`tests/test_audit.py`、`tests/test_gate.py`、`tests/test_harness_loop.py`
- 新增：`scripts/demo_run.py`
- 生成：`artifacts/pytest-q.txt`、`artifacts/harness-demo/*`
- 不改动：`docs/q3.md`、`docs/SPEC.md`、其他 `src/` 模块（F-6 ~ F-11 不在本次范围）
