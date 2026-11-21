---
type: "Memory"
title: "会话测试入口"
description: "测试文件：`tests/auth/test_session.py`"
generated:
  by: "process:pi-okf-memory"
  at: "2026-11-19T05:31:06.751Z"
id: "mem_06dbced0"
key: "session-test-entry"
project: "C:\\projet\\homework\\ch4\\pi-ltm"
projectId: "300a6590f1bc"
sources:
  -
    resource: "tests/auth/test_session.py"
tags: ["memory", "testing", "e2e", "login_demo", "unittest", "migrated"]
status: "stable"
revision: 5
---

# Content

- 测试文件：`tests/auth/test_session.py`
- 运行命令：`python -m unittest tests.auth.test_session -v`
- 已核对：入口位于 `tests/auth/`（`tests/auth/__init__.py` 为包标记），需在 `pi-ltm` 根目录运行；`find_plugin_root()` 会向上定位项目根，故位于子目录仍能解析路径
- 实测：2026-11-19 运行 `Ran 5 tests in 0.886s` → `OK`
- 前置条件：`login_demo` 在 `../login_demo`（`LOGIN_DEMO_ROOT` 可覆盖），端口 3001 空闲

# Citations

[1] tests/auth/test_session.py
