# Harness 运行记录（demo）

- run_id: `17940453828090001ebedd3b4`
- task: 修复 add 函数
- plan revision: 1
- 首次运行: status=waiting_approval, stop_reason=approval_required
- 恢复运行: status=completed, stop_reason=completed
- 证据: ['test:target']
- 预算: used={'seconds': 0.0, 'steps': 5.0, 'test_runs': 1.0, 'tool_calls': 5.0, 'writes': 1.0} / limits={'max_seconds': 120, 'max_steps': 8, 'max_test_runs': 3, 'max_tool_calls': 40, 'max_writes': 3}

## 计划

1. 复现 add 缺陷
2. 修复 src/app.py
3. 运行 target 测试
4. 提交

## Checkpoints（按 run 隔离）

- `checkpoints/17940453828090001ebedd3b4/step-000001-after_step.json`
- `checkpoints/17940453828090001ebedd3b4/step-000002-after_step.json`
- `checkpoints/17940453828090001ebedd3b4/step-000002-approval.json`
- `checkpoints/17940453828090001ebedd3b4/step-000002-halt-approval_required.json`
- `checkpoints/17940453828090001ebedd3b4/step-000003-after_step.json`
- `checkpoints/17940453828090001ebedd3b4/step-000004-after_step.json`
- `checkpoints/17940453828090001ebedd3b4/step-000005-after_step.json`
- `checkpoints/17940453828090001ebedd3b4/step-000005-run_end.json`

## 审计事件时间线

| seq | event | decision | rule | tool_call_id |
|---|---|---|---|---|
| 1 | run.start | start | run | None |
| 2 | gate.decision | allow | allowed | call-001 |
| 3 | gate.batch | batch | commit | None |
| 4 | plan.update | allow | update_plan | call-001 |
| 5 | gate.decision | require_approval | write_requires_approval | call-002 |
| 6 | approval.requested | require_approval | write_requires_approval | call-002 |
| 7 | gate.batch | batch | commit | None |
| 8 | run.halt | approval_required | ApprovalRequired | None |
| 9 | approval.granted | allow | approved | call-002 |
| 10 | resume.plan | resume | 可恢复 | None |
| 11 | run.start | resume | resume | None |
| 12 | gate.decision | allow | approval_granted | call-003 |
| 13 | gate.batch | batch | commit | None |
| 14 | write.intent | intent | write_file | call-003 |
| 15 | write.applied | allow | write_file | call-003 |
| 16 | gate.decision | allow | allowed | call-004 |
| 17 | gate.batch | batch | commit | None |
| 18 | test.intent | intent | run_test | call-004 |
| 19 | test.result | allow | run_test | call-004 |
| 20 | gate.decision | allow | allowed | call-005 |
| 21 | gate.batch | batch | commit | None |
| 22 | run.halt | completed | run_end | None |
