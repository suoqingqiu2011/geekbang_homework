# F-1 ~ F-5 修复任务清单

| 状态 | ID | 任务 | 文件 | 完成标准 |
|---|---|---|---|---|
| [x] | T5 | `resume()` 复位暂停/取消标志与 sandbox 取消事件 | `src/harness.py` | 正常恢复分支设置 `_pause_requested=False`、`_cancel_requested=False`、`sandbox.reset_cancel()`、`_last_stop=None` |
| [x] | T4a | Gate 拒绝时写 `step.error` | `src/harness.py` | DENY 分支抛异常前追加审计事件（含 tool_call_id / rule / error） |
| [x] | T4b | 工具执行异常写 `step.error` | `src/harness.py` | `yield from super()` 外层捕获异常，记录后原样抛出 |
| [x] | T4c | 重写 A2 用例 | `tests/test_audit.py` | 真实 run 的 `audit.by_run()` 断言事件链、seq、tool_call_id 溯源、`verify()` |
| [x] | T4d | 新增 step.error 用例 | `tests/test_audit.py` | 越界写入被拒后存在 `step.error`（`deny` / `sandbox`） |
| [x] | T3 | 新增 S1 Gate 级用例 | `tests/test_gate.py` | handler 未被调用、`.env` 未创建、`gate.decision` 为 deny/sandbox、无 `write.intent` |
| [x] | T5b | 真实 pause→resume 用例 | `tests/test_harness_loop.py` | pause 后 approve 再 resume 可 `completed`；取消不可复活用例保留 |
| [x] | T1 | 运行并归档 pytest 输出 | `artifacts/pytest-q.txt` | `pytest -q` 退出码 0，输出完整（52 passed, 1 skipped） |
| [x] | T2 | 重生成 demo 运行记录 | `scripts/demo_run.py`、`artifacts/harness-demo/*` | 含 plan、按 run 隔离快照、审批→resume→completed 的完整记录 |

## 执行结果

- `python -m pytest`（`pyproject.toml` 已配置 `addopts = "-q"`）：`52 passed, 1 skipped in 12.01s`，输出归档于 `artifacts/pytest-q.txt`。
- demo run_id：`1794038375741000182982f39`，首次停在 `waiting_approval`，审批后 `resume()` 至 `completed`；`state.json` 含 `plan`（4 条，revision=1）；快照位于 `checkpoints/<run_id>/`。
- 审计链：22 条事件，包含 `run.start` / `gate.decision` / `gate.batch` / `plan.update` / `approval.granted` / `resume.plan` / `write.intent` / `write.applied` / `test.intent` / `test.result` / `run.halt`，`audit.verify()` 通过。
