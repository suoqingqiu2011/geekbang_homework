# Guarded Coding Harness

基于 [smolagents](https://github.com/huggingface/smolagents) `ToolCallingAgent` 的受控编码执行框架。它不改写内核，只覆写 `process_tool_calls` 一处拦截缝，在工具真正执行前完成参数校验、权限与路径约束、风险分级、写审批、重复调用拦截、预算控制，并把每一步的决策、写入、测试证据、Checkpoint 和审计链落盘。

完成契约只认真实测试证据（`kind` 为 `test` / `test_run`，且 `returncode == 0`、指纹与当前工区一致）；`note`、`finding`、`check` 等自报记录不能作为交付物。

---

## 1. 环境要求

| 项目 | 要求 | 本项目验证环境 |
|---|---|---|
| 操作系统 | Windows / Linux / macOS | Windows（PowerShell 5.1、Git Bash） |
| Python | >= 3.10 | 3.14.7 |
| pytest | >= 8 | 9.2.0 |
| smolagents | 本地源码或 PyPI 包 | 同级目录 `../smolagents`（1.27.0.dev0） |
| 虚拟环境 | **必须使用独立的 venv / conda / uv 环境** | 项目根目录 `.venv/`（已被 `.gitignore` 忽略）；若本项目被当作 Harness workspace，则建在 `ch3/.venvs/guarded/` |

目录约定：`pyproject.toml` 中 `pythonpath = ["src", "../smolagents/src"]` 要求 smolagents 源码位于与本项目同级的 `../smolagents`。本项目的完整目录结构见第 8 节。

**为什么必须隔离**：

1. `pip install -e ../smolagents` 会带入 `transformers`、`huggingface-hub`、`requests` 等传递依赖，装进全局环境容易与其他项目冲突。
2. `HarnessPolicy.suite_command()` 用 `sys.executable` 启动子进程执行 pytest；隔离环境保证主进程、pytest 与被测仓库用同一解释器和同一套依赖。
3. 避免 PyPI 版 smolagents 与 `../smolagents/src` 本地源码混用，排查「到底加载了哪一份」非常费时。
4. 项目要求 `>=3.10`，用 venv 固定解释器版本，不受机器上其他 Python 影响。

> 注意：如果本项目本身就是 `GuardedHarness` 的 workspace，虚拟环境不要放进被指纹覆盖的位置。`workspace_fingerprint()` 只跳过**根目录**的 `artifacts`，以及任意层级的 `.harness`、`.git`、`__pycache__`、`.pytest_cache`；`src/artifacts` 之类的深层同名目录仍计入指纹，`.venv` 会被整体计入指纹，导致指纹计算变慢、测试证据频繁「过期」。稳妥做法是把环境建在项目外，例如 `c:\projet\homework\ch3\.venvs\guarded`。

---

## 2. 安装依赖

所有命令都在 `guarded_coding_harness` 目录下执行。PowerShell 与 Git Bash 的差异仅在路径分隔符（`\` 与 `/`）以及激活脚本的位置，命令本身等效。

### 2.1 创建并激活虚拟环境

Git Bash 进入项目目录（`C:\` 在 Git Bash 中写作 `/c/`）：

```bash
cd /c/projet/homework/ch3/guarded_coding_harness
```

PowerShell：

```powershell
# PowerShell
cd C:\projet\homework\ch3\guarded_coding_harness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Git Bash（Windows 上激活脚本在 `Scripts/` 下，不是 Linux 的 `bin/`）：

```bash
# Git Bash
python -m venv .venv
source ./.venv/Scripts/activate
```

如果 PowerShell 报「禁止运行脚本」：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

也可以**不激活**，直接调用环境内的解释器，效果相同且不受 shell 状态影响：

```bash
# Git Bash
./.venv/Scripts/python.exe -m pip install --upgrade pip
```

```powershell
# PowerShell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

### 2.2 安装依赖

后续所有 `pip` 与 `pytest` 都通过 `python -m ...` 调用，避免解析到全局环境的可执行文件。

#### 方式 A：使用本地 smolagents 源码（推荐，与测试配置一致）

pytest 会通过 `pyproject.toml` 的 `pythonpath` 直接引用 `../smolagents/src`，无需 `pip install smolagents`，但需要保证 smolagents 的运行依赖已安装：

```powershell
# PowerShell
python -m pip install --upgrade pip
python -m pip install -e ..\smolagents
python -m pip install "pytest>=8"
```

```bash
# Git Bash
python -m pip install --upgrade pip
python -m pip install -e ../smolagents
python -m pip install "pytest>=8"
```

#### 方式 B：把本项目安装为可编辑包

```powershell
# PowerShell
python -m pip install -e .
python -m pip install "pytest>=8"
```

```bash
# Git Bash
python -m pip install -e .
python -m pip install "pytest>=8"
```

`pyproject.toml` 中 `dependencies = ["smolagents"]` 会从 PyPI 安装；运行测试时仍优先使用 `../smolagents/src`。也可以只装测试依赖：

```bash
python -m pip install -e ".[test]"
```

### 2.3 验证安装

```powershell
# PowerShell
python --version
python -c "import sys; print(sys.executable)"
python -c "import pytest, smolagents; print(pytest.__version__); print(smolagents.__file__)"
python -m pytest --version
python -m pytest --collect-only
```

```bash
# Git Bash（命令与 PowerShell 完全相同）
python --version
python -c "import sys; print(sys.executable)"
python -c "import pytest, smolagents; print(pytest.__version__); print(smolagents.__file__)"
python -m pytest --version
python -m pytest --collect-only
```

期望结果：

- `sys.executable` 指向 `.../guarded_coding_harness/.venv/Scripts/python.exe`；
- `smolagents.__file__` 指向 `.../ch3/smolagents/src/smolagents/__init__.py`（本地 editable 源码，而非 `site-packages`）；
- `pytest --collect-only` 显示 `74 tests collected`。

---

## 3. 快速开始

### 3.1 离线演示（无需 API Key）

```powershell
# PowerShell
python scripts\demo_run.py
```

```bash
# Git Bash
python scripts/demo_run.py
```

预期输出类似：

```text
run_id=... status=completed step=...
```

该脚本会在 `ch3/artifacts/harness-demo/` 生成一份完整运行记录：

- `state.json`：计划、预算、台账、证据、待审批项
- `audit.jsonl`：`run.start` → `gate.decision` → `approval.requested` → `approval.granted` → `write.intent` → `write.applied` → `test.intent` → `test.result` → `run.halt`
- `checkpoints/<run_id>/step-XXXXXX-<boundary>.json`：每步快照（含 `halt-approval_required`、`run_end`）
- `logs/*.log`：测试进程输出
- `run-summary.md`：计划、快照清单、审计时间线

### 3.2 真实模型演示（可选，需要 DeepSeek API Key）

```powershell
# PowerShell：$env: 前缀设置当前会话的环境变量
$env:DEEPSEEK_API_KEY = "sk-..."
python scripts\real_model_run.py
```

```bash
# Git Bash：export 把变量导出给子进程
export DEEPSEEK_API_KEY="sk-..."
python scripts/real_model_run.py
```

也可以基于仓库内模板创建本地 `.env`（`.env` 已被 `.gitignore` 忽略，不会进入版本库）：

```powershell
# PowerShell
Copy-Item .env.example .env
```

```bash
# Git Bash
cp .env.example .env
```

`real_model_run.py` 与 3.3 的 `real_model_approval_run.py` 都会自动加载 `guarded_coding_harness/.env`（若不存在则回退到 `ch3/.env`），且**已存在的进程环境变量优先**。因此把 Key 写入 `.env` 后即可直接运行，无需再 `export` 或 `source`；上面手动设置变量的方式仍然有效，可用来临时覆盖 `.env` 中的值。

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARNESS_REAL_MODEL` | `deepseek-v4-pro` | 模型 id |
| `HARNESS_REAL_BASE` | `https://api.deepseek.com/v1` | OpenAI 兼容 API base |

产物写入 `ch3/artifacts/real-model-demo/`。脚本会驱动真实的 read → write → test → final_answer 流程，并在 `run-summary.md` 中给出 Diff、测试日志尾部、证据指纹与审计时间线。

> 注意：脚本启动时会清理上一轮的 `audit.jsonl`、`audit.jsonl.head`、`state.json`、`checkpoints`、`logs`、`run-summary.md`，只保留最新一次运行。

### 3.3 真实模型 + 完整审批流程（可选，需要 DeepSeek API Key）

`real_model_approval_run.py` 与 3.2 的演示相反，保持 `require_write_approval=True`，因此会真实经历
`run → waiting_approval → approve → resume → completed` 的完整停机链路（含暂停、对账等中间态）。

```bash
# Git Bash 与 PowerShell 均可：脚本内部用 pathlib 拼接路径，不受分隔符影响
python scripts/real_model_approval_run.py
```

该脚本会自动加载 `guarded_coding_harness/.env`（若不存在则回退到 `ch3/.env`），**已存在的进程环境变量优先**。
因此只要 `.env` 中已写入 `DEEPSEEK_API_KEY`，就不必再执行 `export` 或 `source`。

预期输出：

```text
已加载环境变量文件: C:\projet\homework\ch3\guarded_coding_harness\.env
run_id=17941258937740001ba7aaf48 status=completed stop_reason=completed
approvals=1 rounds=2
summary=C:\projet\homework\ch3\artifacts\real-model-approval-demo\run-summary.md
```

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARNESS_REAL_MODEL` | `deepseek-v4-pro` | 模型 id |
| `HARNESS_REAL_BASE` | `https://api.deepseek.com/v1` | OpenAI 兼容 API base |
| `HARNESS_MAX_ROUNDS` | `12` | run / approve / resume 循环上限 |

行为说明：

- 审批由脚本自动完成（`harness.approve(..., actor="human")`），不会阻塞等待终端输入。
- 驱动循环按终态分流：`waiting_approval` → 只批准**尚未批准过**的待审批项，然后 `resume()`（跳过已批准但还没被 resume 消耗的项，避免重复批准刷新 `granted_step` / TTL）；`paused` → `resume()`；`waiting_reconcile` → `reconcile()` 后 `resume()`；其他终态则退出循环。
- 策略中 `approval_ttl_steps=8`，允许模型在恢复后先做少量只读步骤，再原样重发同一 `write_file` 取得放行。
- 退出码：`completed` 为 `0`，其他终态为 `1`，缺少 API Key 为 `2`。

产物写入 `ch3/artifacts/real-model-approval-demo/`，`run-summary.md` 包含审批记录表（`tool_call_id`、`target`、`granted_step`、`ttl_steps`、`signature`）、阶段链路、证据、Diff、测试日志与审计时间线。脚本启动时会清理上一轮的 `audit.jsonl`、`audit.jsonl.head`、`state.json`、`checkpoints`、`logs`、`run-summary.md` 与 `workspace`，只保留最新一次运行。

> 若 `.env` 缺失或其中 `DEEPSEEK_API_KEY` 为空，脚本会打印缺失提示并以 `2` 退出；此时可按 3.2 的方式临时 `export DEEPSEEK_API_KEY=...`，环境变量会优先生效。

#### 3.3.1 与 3.2（`real_model_run.py`）的差异

两个脚本共用同一套骨架：相同的 `.env` 自动加载、`OpenAIModel` 构造、`drive()` 停机驱动循环与 `run-summary.md` 渲染方式。差异集中在一个策略开关及其连带设计上。

核心差异：写操作是否需要审批

| 维度 | `real_model_run.py`（T2） | `real_model_approval_run.py`（T3） |
|---|---|---|
| `require_write_approval` | `False` | `True` |
| 写操作路径 | Gate 直接 `allow` → `write.intent` → `write.applied` | Gate 判 `require_approval` → 挂起 → `waiting_approval` 停机 |
| 是否经历审批停机 | 否，模型连续执行 | 是，真实走 `run → waiting_approval → approve → resume → completed` |
| `approval_ttl_steps` | 未设置 | `8`（放宽，允许恢复后先做只读步骤再重发写入） |

T2 关闭审批是有意为之：审批→resume 的中断恢复已由离线演示（`artifacts/harness-demo`）覆盖，真实模型场景下让模型连续执行，可避免它在审批停顿后改变动作顺序（例如先测后写）导致证据指纹失效。

连带的实现差异：

| 维度 | T2 | T3 |
|---|---|---|
| 产物目录 | `ch3/artifacts/real-model-demo/` | `ch3/artifacts/real-model-approval-demo/` |
| 启动清理范围 | 5 项（不含 `workspace`） | 6 项（含 `workspace`，完全重建） |
| TASK 提示词 | 4 步指令 | 额外附加「审批提示」：恢复后必须**原样重发**同一 `write_file`（`path` 与 `content` 完全一致）才能放行 |
| `drive()` 循环上限 | 固定 20 轮 | 参数化，默认 12，可用 `HARNESS_MAX_ROUNDS` 调整 |
| 审批记录 | `approve()` 返回值直接丢弃 | 收集到 `approvals` 列表（`signature`、`target`、`granted_step`、`ttl_steps`、`fingerprint`） |
| 阶段记录字段 | `status` / `stop_reason` / `step` / `pending` | 额外记录 `tool_calls` / `writes` / `test_runs` / `message`，`reconcile` 结果单独入列 |
| `run-summary.md` 章节 | 无审批章节 | 多出「审批记录」表格与「阻断」字段 |
| 终端输出 | `run_id` / `status` / `summary` | 额外打印 `approvals=N rounds=N` |

实测终态差异：

| 项目 | T2 | T3 |
|---|---|---|
| 终态 | `completed` | `completed` |
| 步数 / 工具调用 | `4 / 4` | `5 / 5` |
| 写入 / 测试次数 | `writes=1`、`test_runs=1` | `writes=1`、`test_runs=1` |
| 审批次数 | `0` | `1` |
| 驱动轮次 | `1`（无停机） | `2`（停机 1 次 + 恢复 1 次） |
| 审计事件数 | `15` | `22`（多出 `approval.requested`、`approval.granted`、`resume.plan`、`run.start(resume)` 等） |

两者的职责因此可以概括为：**T2 验证「真实模型 + 自动防护」**（Gate 参数校验与风险分级、Sandbox 路径约束、预算、审计哈希链、完成契约），**T3 在此之上验证「真实模型 + 人工审批闭环」**——审批绑定写入签名与工作区指纹，停机后必须重发同一写入才放行，其余防护同样生效。

---

## 4. 以编程方式使用

### 4.1 装配 Harness

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))
sys.path.insert(0, str(Path("../smolagents/src").resolve()))

from harness import GuardedHarness, HarnessPolicy
from smolagents.models import OpenAIModel

policy = HarnessPolicy(
    required_scopes=("target",),
    suites={"target": ["tests/test_target.py"]},
    writable_zones=("artifacts", "src"),
    require_write_approval=True,
    max_steps=12,
)

model = OpenAIModel(
    model_id="deepseek-v4-pro",
    api_base="https://api.deepseek.com/v1",
    api_key="sk-...",
    temperature=0.0,
)

harness = GuardedHarness("./workspace", model, policy=policy)
```

`GuardedHarness` 的构造参数：

| 参数 | 说明 |
|---|---|
| `workspace` | 被操作的工区根目录（会解析为绝对路径） |
| `model` | 任意 smolagents `Model` 实例 |
| `policy` | `HarnessPolicy`，缺省为默认策略 |
| `sandbox` | 可选，自定义 `Sandbox`；缺省按 `policy` 自动创建 |
| `harness_dir` | 运行时产物目录；缺省为 `<workspace>/artifacts/.harness` |

### 4.2 运行任务

```python
result = harness.run("修复 src/app.py 中的 add 函数")
print(result.status, result.stop_reason, result.step)
print(result.to_dict())
```

`result.status` 取值与处理方式：

| status | 含义 | 下一步 |
|---|---|---|
| `completed` | 完成契约已满足 | 结束 |
| `waiting_approval` | 写操作等待人工审批 | `approve()` / `reject()` 后 `resume()` |
| `paused` | 被 `pause()` 暂停 | `resume()` |
| `waiting_reconcile` | 存在结果未知的写入 | `reconcile()` 后 `resume()` |
| `failed` | 预算耗尽 / 步骤耗尽 / 模型错误 | 检查 `stop_reason` |
| `cancelled` | 被 `cancel()` 取消 | 需新建实例重新 `run()` |

`stop_reason` 取值：`completed`、`paused`、`cancelled`、`max_steps`、`budget_exceeded`、`approval_required`、`workspace_changed`、`unknown_result`、`policy_denied`、`model_error`。

`run()` 与 `resume()` 共用同一套 `stop_reason → status` 映射（`GuardedHarness._status_for_stop`）：恢复时如果再次因预算或审批失败，得到的状态与首次运行完全一致（`budget_exceeded` → `failed`、`approval_required` → `waiting_approval`），不会出现「首次失败、恢复后卡在 reconcile」的分叉。

预算与重复规则在**批次内**也会提前预占：同一批次里第二个同签名写入会被 `duplicate_write` 拒绝，`max_writes` / `max_test_runs` / `repeat_limit` 都把同批已评估请求计入；任一请求被拒时整批中止，不做部分执行（详见 4.7 末尾）。

### 4.3 审批与恢复

```python
first = harness.run("修复 add")
if first.status == "waiting_approval":
    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认修改")
    second = harness.resume()
```

- `approve()` 会把审批绑定到「调用签名 + 当前工区指纹 + 步骤」，工区变化后旧审批自动失效（`WorkspaceChanged`）。
- `reject()` 直接拒绝待审批项，并在所有待审批项都处理完后解除 `blocked`；随后可 `resume()` 让模型换方案继续（重复提交同一动作仍受 `repeat_limit` 约束）。
- `resume()` 先取当前 run 的最新 Checkpoint 并把快照恢复进 `StateStore`；`load()` 失败（checksum 不符或文件半截）等同于「没有可恢复快照」。随后按固定优先级执行 `plan_resume()`：终态 → 未确认进程 → 未销记测试意图 → 指纹变化 → `blocked`（仅 `approval_required` 可凭有效批准票放行）→ 未决写入（转 `reconcile`）→ 预算耗尽。命中即停，不会跳过前序检查。
- 判定为 `refuse` 时：`workspace_changed` / `unknown_result` 落到 `waiting_reconcile`，其余落到 `paused`；同时落一份 `*-resume-refused.json` 快照并抛出对应异常（`WorkspaceChanged` / `UnknownResult` / `BudgetExceeded` / `HarnessCancelled` / `PolicyDenied`）。`workspace_changed` 还会写回 `blocked`，防止二次 `resume()` 洗白。

### 4.4 对账未知写入

```python
if result.status == "waiting_reconcile":
    # decisions: {signature（优先）或 tool_call_id: "executed" | "not_executed" | "uncertain"}
    harness.reconcile()
    result = harness.resume()
```

`classify_write()` 依据写入前 sha256、写入后 sha256 与当前文件内容判定 `executed` / `not_executed` / `uncertain`；人工结论与现场不一致时会降级为 `uncertain`。

若 `resume()` 因「存在未销记的测试意图」返回 `unknown_result`（例如进程在 `run_test` 中途崩溃），可人工销记后再恢复：

```python
outcome = harness.abandon_tests(actor="ops", note="进程崩溃，测试结果不可判定")
# outcome["abandoned"] -> 被标记为 uncertain 的测试签名
result = harness.resume()
```

`abandon_tests()` 只把测试意图的 `intent` 改为 `uncertain` 并写 `test.abandoned` 审计事件，不会伪造测试结果；是否使用由人决定。未对账直接调用 `harness.run(...)` 会抛 `UnknownResult`，不会静默清空未决写入或测试意图。

### 4.5 暂停与取消

```python
harness.pause()    # 当前步结束后停在 paused，可 resume()
harness.cancel()   # 终止当前测试进程树，进入 cancelled
harness.reset_cancel()  # 仅在任务未进入终态时解除取消信号；已取消任务会抛 PolicyDenied，请开全新 run
```

取消只作用于**活跃命令**：`Sandbox.run()` 在子进程结束后立即清空 `last_pid`，另把该 PID 记入 `recent_pid` 供 `_on_halt` 事后确认 termination。因此非运行态调用 `harness.cancel()` 不会拿一个已结束的旧 PID 去杀进程（避免 PID 复用误伤无关进程）。

### 4.6 查看状态、审计与快照

```python
harness.state.data          # 完整状态：plan / budget / evidence / writes / tests / pending
harness.state.latest_evidence("target")
harness.audit.by_run(harness.run_id)
harness.audit.verify()      # -> (True, "ok")；哈希链 + audit.jsonl.head 锚点一起校验
harness.checkpoints.latest()         # 当前 run 最新快照（按 step, mtime）
harness.checkpoints.list_snapshots() # 当前 run 全部快照
harness.checker.check()     # -> (bool, message)
```

### 4.7 工具一览

| 工具 | 风险 | 参数 | 说明 |
|---|---|---|---|
| `list_files` | READ | `path="."`, `max_entries=50` | 列目录（`max_entries` 上限 500） |
| `search_code` | READ | `pattern`, `path="."` | 文本检索：pattern ≤ 200 字符、单行截断到 2000 字符、最多扫描 2000 个文件、总时长 5s，拒绝嵌套量词 |
| `read_file` | READ | `path`, `start_line=1`, `max_lines=200` | 读文件（单文件上限 1 MB） |
| `inspect_workspace` | READ | 无 | 工区摘要 |
| `write_file` | WRITE | `path`, `content` | 原子写，需审批（可配置） |
| `apply_patch` | WRITE | `path`, `old_text`, `new_text` | 精确替换，需审批 |
| `run_test` | CONTROL | `scope` | 运行 `policy.suites` 中配置的套件 |
| `record_evidence` | READ | `scope`, `summary`, `kind="note"`, `ok=True` | 只能记录辅助证据，不能完成交付 |
| `update_plan` | CONTROL | `items: list[str]` | 更新计划（1–12 条） |
| `final_answer` | CONTROL | `answer` | 完成契约校验不通过会被拒绝 |

所有 READ/WRITE 路径都经过 `Sandbox.resolve()`：拒绝 `..` 逃逸、软链接逃逸、敏感目录（`.ssh`、`.aws`、`.gnupg`、`.docker`、`.kube`）、敏感文件名与后缀（`id_rsa`、`.pem`、`.key`、`.env*` 等）。`search_code` 与 `list_files` 对遍历发现的每个文件也逐个复检，敏感文件与链接逃逸不会进入模型上下文。

同一批次的请求在 `commit` 前先做**批次内预占**：重复签名、写入次数、测试次数与工具调用预算都会把同批已评估的请求计入。因此「一张审批票 + 同批两条相同写入」不会执行两次，`max_writes=1` 也不会被同批两条写入突破——任一请求被拒即整批中止，不做部分执行。

---

## 5. 运行时产物

默认目录：`<workspace>/artifacts/.harness/`。演示脚本通过 `harness_dir` 重定向到 `ch3/artifacts/<demo>/`。

| 文件/目录 | 说明 |
|---|---|
| `state.json` | 计划、预算、台账、证据、待审批、未决写入/测试 |
| `audit.jsonl` | append-only 审计链，每条含 `seq`、`prev_hash`、`hash` |
| `audit.jsonl.head` | 审计头锚点（`seq` + 末条 `hash`），用于发现尾部截断或清空 |
| `checkpoints/<run_id>/step-XXXXXX-<boundary>.json` | 按 run 隔离的原子快照 |
| `logs/<ulid>.log` | 命令/测试进程输出 |

校验审计链：

```python
ok, message = harness.audit.verify()
assert ok, message
```

`verify()` 同时校验哈希链与 `audit.jsonl.head` 锚点：删除尾部记录、清空日志、或日志与锚点长度不一致都会失败；截断后继续 `append()` 会抛 `AuditAnchorError`，不会静默重写锚点。重置运行产物时必须同时删除 `audit.jsonl` 与 `audit.jsonl.head`。

> §5.2 中的三套记录均由当前代码重新生成，`audit.jsonl.head` 随每次 `append()` 同步更新，`verify()` 已验证通过；删除尾部记录、清空日志或锚点不一致都会使校验失败。

### 5.1 为什么 `ch3/artifacts` 不放在本项目内

`ch3/artifacts` 存放的是**作业交付证据**（`pytest-q.txt`、三套 demo 的 `state.json` / `audit.jsonl` / `checkpoints` / `logs` / `run-summary.md`），不是 `guarded_coding_harness` 的源码或运行时依赖。保持与项目同级的原因：

| 维度 | 放在 `ch3/artifacts`（现状） | 放到 `guarded_coding_harness/artifacts` |
|---|---|---|
| 证据与实现分离 | 源码树只含 7 个模块、6 个测试与 3 个脚本 | 审计日志、工区副本与源码混在同一棵树 |
| 打包边界 | 安装/打 sdist 时天然不包含证据 | 需额外配置排除，否则有把审计记录与工区副本带入发行包的风险 |
| pytest 收集 | `testpaths = ["tests"]` 只指向项目测试 | 显式运行 `python -m pytest .` 或 IDE 自动发现时，工区内的 `workspace/tests/test_target.py` 可能被误收集 |
| 生命周期 | demo 可整目录清理重建，不影响源码 | `clean_old_run()` 删除的是项目内目录，误删风险更高 |
| 可重复生成 | 每次运行生成新 `run_id`，旧证据可整体归档 | 需在 `.gitignore` 与提交物之间反复权衡 |

三个脚本中的 `ROOT = Path(__file__).resolve().parents[2]` 与 `DEMO_DIR = ROOT / "artifacts" / ...` 正是按此约定实现的。Harness 自身的默认运行时目录仍是工区内的 `<workspace>/artifacts/.harness/`，与交付证据目录互不影响。

### 5.2 完整运行记录 + 审计记录 + checkpoint恢复记录

以下三条记录取自仓库内已落盘的产物，可分别用 `python scripts/demo_run.py`、`python scripts/real_model_run.py` 与 `python scripts/real_model_approval_run.py` 复现。每次运行的 `run_id` 与哈希不同，但事件结构、证据契约与完成判定一致。

#### 记录 A：离线演示（`ScriptedModel`，确定性，含审批停机）

产物目录：`ch3/artifacts/harness-demo/`

| 项目 | 值 |
|---|---|
| run_id | `17941258738370001993a4977` |
| session | `179412587383700023f94bcf4` |
| task | `修复 add 函数` |
| 驱动模型 | `ScriptedModel`（按脚本返回工具调用，不访问网络） |
| 首次运行 | `status=waiting_approval`，`stop_reason=approval_required`（step=2） |
| 恢复后终态 | `status=completed`，`stop_reason=completed`（step=5） |
| 预算 used / limits | `steps 5/8`、`tool_calls 5/40`、`writes 1/3`、`test_runs 1/3`、`seconds 0/120` |
| 工区指纹 | 开始 `fp:v1:08c7d0ee…`；写入后 `fp:v1:b2ef38b7…` |

计划（`state.json → plan`，revision=1）：

```text
1. 复现 add 缺陷
2. 修复 src/app.py
3. 运行 target 测试
4. 提交
```

执行序列：

| step | tool_call_id | 工具 | 关键参数 | Gate 判定 | 结果 |
|---|---|---|---|---|---|
| 1 | `call-001` | `update_plan` | 4 条计划 | `allow` / `allowed` | `plan.update`，revision=1 |
| 2 | `call-002` | `write_file` | `path=src/app.py` | `require_approval` / `write_requires_approval` | `run.halt`，status=`waiting_approval` |
| — | `call-002` | 人工审批 | `actor=human`、`reason=演示审批`、`ttl_steps=3` | `approval.granted` / `approved` | 批准票绑定「签名 + 指纹 + step=2」 |
| 3 | `call-003` | `write_file` | 与 `call-002` 同签名重发 | `allow` / `approval_granted` | `write.intent` → `write.applied` |
| 4 | `call-004` | `run_test` | `scope=target` | `allow` / `allowed` | `test.result`，`returncode=0` |
| 5 | `call-005` | `final_answer` | `answer=已修复 add，并完成 target 测试` | `allow` / `allowed` | `run.halt` / `completed` |

写入前后哈希：`pre_sha256=828092436119…`，`post_sha256=680df8de3e5c…`。批准票的 TTL 为 3 步，`call-003` 在 step 3 重新发起，未超期；台账中该签名 `count=2`、`applied=true`，说明「第一次被拦、第二次放行一次」，没有重复执行。

工区变化：

```diff
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```

证据（唯一一条，满足完成契约：`kind=test`、`returncode=0`、指纹等于当前工区）：

```json
{
  "seq": 1,
  "scope": "target",
  "kind": "test",
  "returncode": 0,
  "duration": 0.5077,
  "fingerprint": "fp:v1:b2ef38b713687dda0699a3547e599dc4f6945490570859742887b84b6704de9",
  "argv": ["C:\\projet\\homework\\ch3\\guarded_coding_harness\\.venv\\Scripts\\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_target.py"]
}
```

测试日志（`logs/179412587388700349b0f6a96.log`）：

```text
.                                                                        [100%]
1 passed in 0.00s
```

Checkpoint（按 run 隔离，共 8 个）：

```text
checkpoints/17941258738370001993a4977/
├── step-000001-after_step.json              status=running
├── step-000002-after_step.json              status=running
├── step-000002-approval.json                status=waiting_approval
├── step-000002-halt-approval_required.json  status=waiting_approval
├── step-000003-after_step.json              status=running
├── step-000004-after_step.json              status=running
├── step-000005-after_step.json              status=running
└── step-000005-run_end.json                 status=completed
```

审计时间线（23 条，append-only + 哈希链）：

| seq | event | decision | rule | tool_call_id |
|---|---|---|---|---|
| 1 | `run.start` | `start` | `run` | — |
| 2 | `gate.decision` | `allow` | `allowed` | `call-001` |
| 3 | `gate.batch` | `batch` | `commit` | — |
| 4 | `plan.update` | `allow` | `update_plan` | `call-001` |
| 5 | `gate.decision` | `require_approval` | `write_requires_approval` | `call-002` |
| 6 | `approval.requested` | `require_approval` | `write_requires_approval` | `call-002` |
| 7 | `gate.batch` | `batch` | `commit` | — |
| 8 | `run.halt` | `approval_required` | `ApprovalRequired` | — |
| 9 | `approval.granted` | `allow` | `approved` | `call-002` |
| 10 | `resume.plan` | `resume` | `可恢复` | — |
| 11 | `run.start` | `resume` | `resume` | — |
| 12 | `gate.decision` | `allow` | `approval_granted` | `call-003` |
| 13 | `gate.batch` | `batch` | `commit` | — |
| 14 | `write.intent` | `intent` | `write_file` | `call-003` |
| 15 | `write.applied` | `allow` | `write_file` | `call-003` |
| 16 | `gate.decision` | `allow` | `allowed` | `call-004` |
| 17 | `gate.batch` | `batch` | `commit` | — |
| 18 | `test.intent` | `intent` | `run_test` | `call-004` |
| 19 | `test.result` | `allow` | `run_test` | `call-004` |
| 20 | `gate.decision` | `allow` | `allowed` | `call-005` |
| 21 | `gate.batch` | `batch` | `commit` | — |
| 22 | `final.answer` | `allow` | `final_answer` | `call-005` |
| 23 | `run.halt` | `completed` | `run_end` | — |

终态关键字段：`blocked=null`、`pending={}`、`approvals["call-002"].consumed=true`、`writes[...].state=done`、`tests[...].state=done`、`termination.unconfirmed=false`。

校验结论：

```python
assert result.status == "completed"
assert harness.checker.check() == (True, "ok")
assert harness.audit.verify() == (True, "ok")
assert harness.state.latest_evidence("target")["fingerprint"] == harness.sandbox.fingerprint()
```

#### 记录 B：真实模型（`deepseek-v4-pro`）

产物目录：`ch3/artifacts/real-model-demo/`

| 项目 | 值 |
|---|---|
| run_id | `179412588001600012b9feff2` |
| session | `1794125880016000298d2f44e` |
| model | `deepseek-v4-pro`（OpenAI 兼容接口，`temperature=0`） |
| task | 修复 `src/app.py` 中的 `add`（`a - b` → `a + b`），按 read → write → test → final_answer 执行 |
| 终态 | `status=completed`，`stop_reason=completed`，`step=4`，`tool_calls=4` |
| 预算 used / limits | `steps 4/20`、`tool_calls 4/60`、`writes 1/8`、`test_runs 1/5`、`seconds 0/600` |
| 计划 | `plan.items=[]`、`revision=0`（模型未调用 `update_plan`） |
| 工区指纹 | 开始 `fp:v1:08c7d0ee…`；写入后 `fp:v1:637d4f22…` |

该脚本使用 `require_write_approval=False`，因此**没有** `approval.requested`、`gate.decision=require_approval` 与 `run.halt=approval_required`；写操作由 Gate 直接 `allow`，但仍走 `write.intent` → `write.applied` 的落盘流程。

执行序列：

| step | tool_call_id | 工具 | 关键参数 | Gate 判定 | 结果 |
|---|---|---|---|---|---|
| 1 | `call_00_51jHZsPP9ttdJrJ5a9e15200` | `read_file` | `path=src/app.py` | `allow` / `allowed` | 读取错误实现 |
| 2 | `call_00_dKomDzAEoYfjqxUy376Z4946` | `write_file` | `path=src/app.py` | `allow` / `allowed` | `write.intent` → `write.applied`，`post_sha256=1098a821…` |
| 3 | `call_00_ET_NdBBSZsdHXGrw5z4ZOaO2913` | `run_test` | `scope=target` | `allow` / `allowed` | `test.result`，`returncode=0`，耗时 0.4571s |
| 4 | `call_00_2mnNWprySknF0Oyf41A31297` | `final_answer` | `answer=已修复 add` | `allow` / `allowed` | `run.halt` / `completed` |

工区变化：

```diff
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```

证据（唯一一条）：

```json
{
  "seq": 1,
  "scope": "target",
  "kind": "test",
  "returncode": 0,
  "duration": 0.4571,
  "fingerprint": "fp:v1:637d4f2242dcf639e9ec260ae76ca3aab20da1fbd72dfb000d78f7ddc0630896",
  "argv": ["C:\\projet\\homework\\ch3\\guarded_coding_harness\\.venv\\Scripts\\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_target.py"]
}
```

测试日志（`logs/17941258843490019b9d3059e.log`）：

```text
.                                                                        [100%]
1 passed in 0.00s
```

Checkpoint（共 5 个）：

```text
checkpoints/179412588001600012b9feff2/
├── step-000001-after_step.json  status=running
├── step-000002-after_step.json  status=running
├── step-000003-after_step.json  status=running
├── step-000004-after_step.json  status=running
└── step-000004-run_end.json     status=completed
```

审计时间线（15 条）：

| seq | event | decision | rule | tool_call_id |
|---|---|---|---|---|
| 1 | `run.start` | `start` | `run` | — |
| 2 | `gate.decision` | `allow` | `allowed` | `call_00_51jHZsPP9ttdJrJ5a9e15200` |
| 3 | `gate.batch` | `batch` | `commit` | — |
| 4 | `gate.decision` | `allow` | `allowed` | `call_00_dKomDzAEoYfjqxUy376Z4946` |
| 5 | `gate.batch` | `batch` | `commit` | — |
| 6 | `write.intent` | `intent` | `write_file` | `call_00_dKomDzAEoYfjqxUy376Z4946` |
| 7 | `write.applied` | `allow` | `write_file` | `call_00_dKomDzAEoYfjqxUy376Z4946` |
| 8 | `gate.decision` | `allow` | `allowed` | `call_00_ET_NdBBSZsdHXGrw5z4ZOaO2913` |
| 9 | `gate.batch` | `batch` | `commit` | — |
| 10 | `test.intent` | `intent` | `run_test` | `call_00_ET_NdBBSZsdHXGrw5z4ZOaO2913` |
| 11 | `test.result` | `allow` | `run_test` | `call_00_ET_NdBBSZsdHXGrw5z4ZOaO2913` |
| 12 | `gate.decision` | `allow` | `allowed` | `call_00_2mnNWprySknF0Oyf41A31297` |
| 13 | `gate.batch` | `batch` | `commit` | — |
| 14 | `final.answer` | `allow` | `final_answer` | `call_00_2mnNWprySknF0Oyf41A31297` |
| 15 | `run.halt` | `completed` | `run_end` | — |

#### 记录 C：真实模型 + 完整审批流程（`deepseek-v4-pro`，`require_write_approval=True`）

产物目录：`ch3/artifacts/real-model-approval-demo/`

| 项目 | 值 |
|---|---|
| run_id | `17941258937740001ba7aaf48` |
| session | `179412589377400022a883d42` |
| model | `deepseek-v4-pro`（OpenAI 兼容接口，`temperature=0`） |
| task | 修复 `src/app.py` 中的 `add`（`a - b` → `a + b`），按 read → write（审批）→ test → final_answer 执行 |
| 首次运行 | `status=waiting_approval`，`stop_reason=approval_required`（step=2，`writes=0`） |
| 恢复后终态 | `status=completed`，`stop_reason=completed`，`step=5`，`tool_calls=5` |
| 预算 used / limits | `steps 5/20`、`tool_calls 5/60`、`writes 1/8`、`test_runs 1/5`、`seconds 0/600` |
| 计划 | `plan.items=[]`、`revision=0`（模型未调用 `update_plan`） |
| 工区指纹 | 开始 `fp:v1:08c7d0ee…`；写入后 `fp:v1:b2ef38b7…` |
| 审批 | 1 次，`granted_step=2`、`ttl_steps=8`、`signature=sig:fc3bd1fc…`、`consumed=true` |

该脚本使用 `require_write_approval=True`，因此完整经历 `run → waiting_approval → approve → resume → completed`。批准票绑定「写入签名 + 工作区指纹 + 授予步号」，恢复后模型必须**原样重发**同一 `write_file`（`path` 与 `content` 完全一致）才会命中 `approval_granted`，其余防护（Sandbox、预算、审计、完成契约）全部生效。

执行序列：

| step | tool_call_id | 工具 | 关键参数 | Gate 判定 | 结果 |
|---|---|---|---|---|---|
| 1 | `call_00_v2CNDfut59UX4eRyHCJ18121` | `read_file` | `path=src/app.py` | `allow` / `allowed` | 读取错误实现 |
| 2 | `call_00_pfERiyPxtHRVORLV7gRA4000` | `write_file` | `path=src/app.py` | `require_approval` / `write_requires_approval` | `approval.requested` → `run.halt`，status=`waiting_approval` |
| — | `call_00_pfERiyPxtHRVORLV7gRA4000` | 人工审批 | `actor=human`、`reason=端到端脚本自动批准`、`ttl_steps=8` | `approval.granted` / `approved` | 批准票绑定「签名 + 指纹 + step=2」 |
| 3 | `call_00_Gf7c7j99rofX43ntyXnZ1191` | `write_file` | 与上一条同签名重发 | `allow` / `approval_granted` | `write.intent` → `write.applied`，`post_sha256=680df8de…` |
| 4 | `call_00_uLDa6ogeZ7EfxiZVzrUw5442` | `run_test` | `scope=target` | `allow` / `allowed` | `test.result`，`returncode=0`，耗时 0.5083s |
| 5 | `call_00_ET_k63lFXKELRnJHGY2dTP82475` | `final_answer` | `answer=已修复 add` | `allow` / `allowed` | `run.halt` / `completed` |

写入前后哈希：`pre_sha256=828092436119…`，`post_sha256=680df8de3e5c…`，台账中该签名 `state=done`，说明「第一次被拦、恢复后第二次放行一次」，未重复执行。

工区变化：

```diff
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```

证据（唯一一条，满足完成契约：`kind=test`、`returncode=0`、指纹等于当前工区）：

```json
{
  "seq": 1,
  "scope": "target",
  "kind": "test",
  "returncode": 0,
  "duration": 0.5083,
  "fingerprint": "fp:v1:b2ef38b713687ddaa0699a3547e599dc4f6945490570859742887b84b6704de9",
  "argv": ["C:\\projet\\homework\\ch3\\guarded_coding_harness\\.venv\\Scripts\\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_target.py"]
}
```

测试日志（`logs/1794125903544003381611364.log`）：

```text
.                                                                        [100%]
1 passed in 0.01s
```

Checkpoint（共 8 个，含停机与恢复边界）：

```text
checkpoints/17941258937740001ba7aaf48/
├── step-000001-after_step.json              status=running
├── step-000002-after_step.json              status=running
├── step-000002-approval.json                status=waiting_approval
├── step-000002-halt-approval_required.json  status=waiting_approval
├── step-000003-after_step.json              status=running
├── step-000004-after_step.json              status=running
├── step-000005-after_step.json              status=running
└── step-000005-run_end.json                 status=completed
```

审计时间线（22 条）：

| seq | event | decision | rule | tool_call_id |
|---|---|---|---|---|
| 1 | `run.start` | `start` | `run` | — |
| 2 | `gate.decision` | `allow` | `allowed` | `call_00_v2CNDfut59UX4eRyHCJ18121` |
| 3 | `gate.batch` | `batch` | `commit` | — |
| 4 | `gate.decision` | `require_approval` | `write_requires_approval` | `call_00_pfERiyPxtHRVORLV7gRA4000` |
| 5 | `approval.requested` | `require_approval` | `write_requires_approval` | `call_00_pfERiyPxtHRVORLV7gRA4000` |
| 6 | `gate.batch` | `batch` | `commit` | — |
| 7 | `run.halt` | `approval_required` | `ApprovalRequired` | — |
| 8 | `approval.granted` | `allow` | `approved` | `call_00_pfERiyPxtHRVORLV7gRA4000` |
| 9 | `resume.plan` | `resume` | `可恢复` | — |
| 10 | `run.start` | `resume` | `resume` | — |
| 11 | `gate.decision` | `allow` | `approval_granted` | `call_00_Gf7c7j99rofX43ntyXnZ1191` |
| 12 | `gate.batch` | `batch` | `commit` | — |
| 13 | `write.intent` | `intent` | `write_file` | `call_00_Gf7c7j99rofX43ntyXnZ1191` |
| 14 | `write.applied` | `allow` | `write_file` | `call_00_Gf7c7j99rofX43ntyXnZ1191` |
| 15 | `gate.decision` | `allow` | `allowed` | `call_00_uLDa6ogeZ7EfxiZVzrUw5442` |
| 16 | `gate.batch` | `batch` | `commit` | — |
| 17 | `test.intent` | `intent` | `run_test` | `call_00_uLDa6ogeZ7EfxiZVzrUw5442` |
| 18 | `test.result` | `allow` | `run_test` | `call_00_uLDa6ogeZ7EfxiZVzrUw5442` |
| 19 | `gate.decision` | `allow` | `allowed` | `call_00_ET_k63lFXKELRnJHGY2dTP82475` |
| 20 | `gate.batch` | `batch` | `commit` | — |
| 21 | `final.answer` | `allow` | `final_answer` | `call_00_ET_k63lFXKELRnJHGY2dTP82475` |
| 22 | `run.halt` | `completed` | `run_end` | — |

终态关键字段：`blocked=null`、`pending={}`、`approvals["call_00_pfERiyPxtHRVORLV7gRA4000"].consumed=true`、`writes[...].state=done`、`tests[...].state=done`、`termination.unconfirmed=false`。

校验结论：

```python
assert result.status == "completed"
assert harness.checker.check() == (True, "ok")
assert harness.audit.verify() == (True, "ok")
assert harness.state.latest_evidence("target")["fingerprint"] == harness.sandbox.fingerprint()
```

#### 三条记录的差异与共同点

| 维度 | 记录 A（离线） | 记录 B（真实模型） | 记录 C（真实模型 + 审批） |
|---|---|---|---|
| 脚本 | `demo_run.py` | `real_model_run.py` | `real_model_approval_run.py` |
| 决策来源 | `ScriptedModel` 固定脚本 | `deepseek-v4-pro` 实时生成 | `deepseek-v4-pro` 实时生成 |
| `require_write_approval` | `True` | `False` | `True` |
| 写审批链路 | 触发 `waiting_approval`，人工批准后同签名重发 | 策略关闭审批，直接 `allow` | 触发 `waiting_approval`，脚本自动批准后同签名重发 |
| 是否调用 `update_plan` | 是（revision=1） | 否（`plan.items=[]`） | 否（`plan.items=[]`） |
| 停机 / 恢复轮次 | 2 | 1 | 2 |
| 步数 / 工具调用 | 5 / 5 | 4 / 4 | 5 / 5 |
| 审计事件数 | 23 | 15 | 22 |
| 测试解释器 | 项目 `.venv\Scripts\python.exe` | 项目 `.venv\Scripts\python.exe` | 项目 `.venv\Scripts\python.exe` |
| 写入 `post_sha256` | `680df8de…`（34 字节，含结尾 CRLF） | `1098a821…`（32 字节，末尾无换行） | `680df8de…`（34 字节，含结尾 CRLF） |
| 测试耗时 | 0.5077s | 0.4571s | 0.5083s |

共同点：三者的唯一交付证据都是 `kind=test`、`scope=target`、`returncode=0`，且证据指纹等于当前工区指纹；`note`/`finding`/`check` 均未参与完成判定；`final_answer` 都经过 `CompletionChecker` 才被接受。

> 说明：`run-summary.md` 的「最终文件 sha256」由 `read_text()` 读回后计算，Windows 下 CRLF 被归一化为 LF；`state.json → writes[].post_sha256` 则是写盘后的原始字节哈希。记录 B：summary 为 `8f75a686…`（31 字节，LF），state 为 `1098a821…`（32 字节，磁盘原始，末尾无换行）。记录 C：summary 为 `ba1a531f…`（32 字节，LF），state 为 `680df8de…`（34 字节，含结尾 CRLF）。记录 A 的 state 同为 `680df8de…`（34 字节）。summary 与 state 的这种差异属预期，验收以 `state.json` 与实际文件 `Get-FileHash` 为准。

复现命令：

```bash
# 记录 A
python scripts/demo_run.py

# 记录 B（需 DEEPSEEK_API_KEY，见 3.2）
python scripts/real_model_run.py

# 记录 C（需 DEEPSEEK_API_KEY，见 3.3）
python scripts/real_model_approval_run.py
```

两个真实模型脚本都会自动加载 `.env`（`guarded_coding_harness/.env` 优先，缺失时回退到 `ch3/.env`），且已存在的进程环境变量优先，因此无需再 `source` 或 `export`；`demo_run.py` 使用 `ScriptedModel`，不需要任何 Key。

#### 记录 A 的审计记录（`ch3/artifacts/harness-demo/audit.jsonl`）

基本信息：

| 项目 | 值 |
|---|---|
| run_id | `17941258738370001993a4977` |
| 记录条数 | 23，`seq` 1–23 连续 |
| 时间跨度 | `08:17:53.839856Z` → `08:17:54.407916Z`（约 0.57s） |
| 首条哈希（链头） | `h:fd1ee59892dc6f703fb861a4ff8129af851a784fdfcd2f049233ed23eb0eaee1` |
| 末条哈希（链尾） | `h:fc60288644253204cc59bf03185ebfd07ac298263e067ffa1a933100d40a665b` |
| 头锚点 | `audit.jsonl.head` → `{"seq":23,"hash":"h:fc602886…"}`，续写与 `verify()` 均以此为准 |
| actor 分布 | human 3、harness 14、model 6 |
| 文件形态 | append-only JSONL，无删除、无改写 |

每条记录包含 `seq`、`ts`、`event`、`run_id`、`tool_call_id`、`actor`、`decision`、`rule`、`detail`、`prev_hash`、`hash`，其中：

```text
hash = "h:" + sha256(prev_hash + canonical_json(body))
```

`body` 为除 `hash` 外的全部字段，首条 `prev_hash = "genesis"`。任何字段被改动，从该条起后续哈希全部失配。

完整事件序列：

| seq | 时间 (UTC) | actor | event | decision | rule | tool_call_id | hash 前缀 |
|---|---|---|---|---|---|---|---|
| 1 | 08:17:53.839856 | human | `run.start` | `start` | `run` | — | `fd1ee59892dc…` |
| 2 | 08:17:53.849373 | harness | `gate.decision` | `allow` | `allowed` | `call-001` | `5e40d5fd4260…` |
| 3 | 08:17:53.850609 | harness | `gate.batch` | `batch` | `commit` | — | `9ca40a5f25c4…` |
| 4 | 08:17:53.851522 | model | `plan.update` | `allow` | `update_plan` | `call-001` | `1ef4d93d94e1…` |
| 5 | 08:17:53.855739 | harness | `gate.decision` | `require_approval` | `write_requires_approval` | `call-002` | `1e23c526cc1a…` |
| 6 | 08:17:53.856530 | harness | `approval.requested` | `require_approval` | `write_requires_approval` | `call-002` | `1ed61ea649d2…` |
| 7 | 08:17:53.857309 | harness | `gate.batch` | `batch` | `commit` | — | `d58653e23a75…` |
| 8 | 08:17:53.861829 | harness | `run.halt` | `approval_required` | `ApprovalRequired` | — | `37959bfedbaf…` |
| 9 | 08:17:53.864689 | human | `approval.granted` | `allow` | `approved` | `call-002` | `3375453deb63…` |
| 10 | 08:17:53.871719 | harness | `resume.plan` | `resume` | `可恢复` | — | `a7f6157dc374…` |
| 11 | 08:17:53.873667 | human | `run.start` | `resume` | `resume` | — | `d0fb23832e91…` |
| 12 | 08:17:53.877151 | harness | `gate.decision` | `allow` | `approval_granted` | `call-003` | `ef1bad2b50c1…` |
| 13 | 08:17:53.877965 | harness | `gate.batch` | `batch` | `commit` | — | `7ace7e34850a…` |
| 14 | 08:17:53.879235 | model | `write.intent` | `intent` | `write_file` | `call-003` | `894a99e9fd7d…` |
| 15 | 08:17:53.881122 | model | `write.applied` | `allow` | `write_file` | `call-003` | `3d87061c65fc…` |
| 16 | 08:17:53.884242 | harness | `gate.decision` | `allow` | `allowed` | `call-004` | `13caa273e8e6…` |
| 17 | 08:17:53.885163 | harness | `gate.batch` | `batch` | `commit` | — | `20c1d94ef617…` |
| 18 | 08:17:53.886132 | model | `test.intent` | `intent` | `run_test` | `call-004` | `c58296f29264…` |
| 19 | 08:17:54.397012 | model | `test.result` | `allow` | `run_test` | `call-004` | `344f923521c1…` |
| 20 | 08:17:54.400419 | harness | `gate.decision` | `allow` | `allowed` | `call-005` | `4de696c83562…` |
| 21 | 08:17:54.401321 | harness | `gate.batch` | `batch` | `commit` | — | `74201a8c185e…` |
| 22 | 08:17:54.402145 | model | `final.answer` | `allow` | `final_answer` | `call-005` | `e06b0ac9e392…` |
| 23 | 08:17:54.407916 | harness | `run.halt` | `completed` | `run_end` | — | `fc6028864425…` |

表中哈希为 `h:` 之后的前 12 位，完整值见源文件。

按 `tool_call_id` 溯源：

| tool_call_id | seq | 语义 |
|---|---|---|
| `call-001` | 2、4 | 计划提交与落盘 |
| `call-002` | 5、6、9 | 写入被拦 → 请求审批 → 人工批准（未执行写入） |
| `call-003` | 12、14、15 | 凭批准票放行 → 写入意图 → 写入完成 |
| `call-004` | 16、18、19 | 放行 → 测试意图 → 测试结果（`returncode=0`） |
| `call-005` | 20、22 | 放行 → 提交答案（`final.answer`） |

`call-002` 与 `call-003` 签名相同（`sig:fc3bd1fc…`），但审计能区分「被拦截的一次」与「凭批准票执行的一次」，不会混为一次。

关键安全不变量与对应证据：

| 不变量 | 审计证据 |
|---|---|
| 写入前必须先有门禁判定 | seq 12（`allow`）在 seq 14（`write.intent`）之前 |
| 未审批不落盘 | seq 5–8 之间没有 `write.intent`，文件未变 |
| 审批绑定工区指纹 | seq 6 与 seq 9 的 `fingerprint` 均为 `fp:v1:08c7d0ee…` |
| 写入前后可对账 | seq 14 `pre=828092436119…`；seq 15 `pre=828092436119…`、`post=680df8de3e5c…` |
| 测试结果带指纹 | seq 19 `fingerprint=fp:v1:b2ef38b7…`，与写入后工区一致 |
| 完成前必有测试证据 | seq 19（`test.result`）在 seq 23（`run.halt/completed`）之前 |
| 停机可恢复且留痕 | seq 8 `run.halt` → seq 10 `resume.plan` → seq 11 `run.start(rule=resume)` |

本 run 未出现（说明未被触发，而非缺失）的事件：`DENY`、`duplicate_write`、`repeat_limit`、`budget_exceeded`、`workspace_changed`、`unknown_result`。

复核脚本：

```python
import sys
sys.path.insert(0, r"c:\projet\homework\ch3\guarded_coding_harness\src")

from audit import AuditLog

log = AuditLog(r"c:\projet\homework\ch3\artifacts\harness-demo\audit.jsonl")
print(log.verify())                    # -> (True, 'ok')
print(len(log.records()))              # -> 23
print(log.trace("call-002"))           # -> 3 条
print(log.by_run("17941258738370001993a4977")[-1]["event"])   # -> run.halt
```

篡改检测：修改任意一条记录的 `decision` 后调用 `verify()`，会返回 `(False, "哈希校验失败：第 N 条")`；删除中间一条则先报 `seq 不连续`。相关用例见 `tests/test_audit.py`。

原始记录样例（链头、停机与审批）：

```json
{"actor":"human","decision":"start","detail":{"fingerprint":"fp:v1:08c7d0ee9e52bf444ce0f485fa76da4730ed447280a926a0bced5aa5e12d9a6f","session":"179412587383700023f94bcf4","task":"修复 add 函数"},"event":"run.start","hash":"h:fd1ee59892dc6f703fb861a4ff8129af851a784fdfcd2f049233ed23eb0eaee1","prev_hash":"genesis","rule":"run","run_id":"17941258738370001993a4977","seq":1,"tool_call_id":null,"ts":"2026-11-08T08:17:53.839856+00:00"}
{"actor":"harness","decision":"approval_required","detail":{"message":"写操作需要人工审批","step":2},"event":"run.halt","hash":"h:37959bfedbafcdfa7504b3db337a42cc1e54f8f3272ab7a6898c118fc6fadcba","prev_hash":"h:d58653e23a75be660f91f8d1ae603abda716477b838761b625b74566dda80bee","rule":"ApprovalRequired","run_id":"17941258738370001993a4977","seq":8,"tool_call_id":null,"ts":"2026-11-08T08:17:53.861829+00:00"}
{"actor":"human","decision":"allow","detail":{"fingerprint":"fp:v1:08c7d0ee9e52bf444ce0f485fa76da4730ed447280a926a0bced5aa5e12d9a6f","reason":"演示审批","signature":"sig:fc3bd1fca0d7daee26a4c6707407eda12b2ae95d638dcba1eddb91dc6ae75d48","ttl_steps":3},"event":"approval.granted","hash":"h:3375453deb63190f9657dd5843c783bbcc73c4e48b3ffb800c2b053da40e5f48","prev_hash":"h:37959bfedbafcdfa7504b3db337a42cc1e54f8f3272ab7a6898c118fc6fadcba","rule":"approved","run_id":"17941258738370001993a4977","seq":9,"tool_call_id":"call-002","ts":"2026-11-08T08:17:53.864689+00:00"}
{"actor":"harness","decision":"completed","detail":{"step":5,"stop_reason":"completed"},"event":"run.halt","hash":"h:fc60288644253204cc59bf03185ebfd07ac298263e067ffa1a933100d40a665b","prev_hash":"h:e06b0ac9e3923859e944e460b35b93d32f1bad37738f96e6fbe31e2f9f21de34","rule":"run_end","run_id":"17941258738370001993a4977","seq":23,"tool_call_id":null,"ts":"2026-11-08T08:17:54.407916+00:00"}
```

#### 记录 A 的 Checkpoint 恢复记录

数据来源：`ch3/artifacts/harness-demo/checkpoints/17941258738370001993a4977/`，对应「写入被拦 → 停机 → 批准 → 恢复」这一段。

快照清单（按 run 隔离，共 8 个）：

```text
checkpoints/17941258738370001993a4977/
├── step-000001-after_step.json              正常步进
├── step-000002-after_step.json              正常步进
├── step-000002-halt-approval_required.json  停机点（恢复起点）
├── step-000002-approval.json                批准票落盘（恢复依据）
├── step-000003-after_step.json              恢复后第一步
├── step-000004-after_step.json
├── step-000005-after_step.json
└── step-000005-run_end.json                 终态
```

同一 step 2 存在三个快照：步进、停机、审批是三个不同的 `boundary`，各自原子写入、互不覆盖。

停机快照 `step-000002-halt-approval_required.json` 的元信息：

| 字段 | 值 |
|---|---|
| `boundary` | `halt-approval_required` |
| `step` | `2` |
| `saved_at` | `2026-11-08T08:17:53.861101+00:00` |
| `checksum` | `b83e11e0ff1c8d43ffc7bd0c4fdc552bd0575178a5e9e99dfb2e3dcc5f7eddd4` |

快照中的关键状态：

| 字段 | 值 | 含义 |
|---|---|---|
| `status` | `waiting_approval` | 停在待审批 |
| `stop_reason` | `approval_required` | 停机原因 |
| `fingerprint` | `fp:v1:08c7d0ee…` | 工区指纹（未变化） |
| `blocked.signature` | `sig:fc3bd1fc…` | 阻断签名，恢复时要匹配 |
| `blocked.reason` | `等待人工审批` | 阻断说明 |
| `pending["call-002"].pre_sha256` | `828092436119…` | 审批绑定的文件版本 |
| `writes` | `{}` | **写入未发生** |
| `artifacts` | `{}` | 工区未变 |
| `tests` | `{}` | 无未决测试 |
| `termination.unconfirmed` | `false` | 进程退出已确认 |
| `budget.used` | `steps 2/8`、`tool_calls 2/40`、`writes 0/3`、`test_runs 0/3`、`seconds 0/120` | 预算未耗尽 |
| `ledger["sig:fc3bd1fc…"]` | `count=1`、`applied=false` | 只请求过一次，未执行 |

审批快照 `step-000002-approval.json`（`checksum=4a088e37490f5286…`，`saved_at=08:17:53.863966`）与停机快照的唯一实质差异，是 `approvals` 由 `{}` 变为：

```json
{
  "call-002": {
    "actor": "human",
    "at": "2026-11-08T08:17:53.863083+00:00",
    "consumed": false,
    "fingerprint": "fp:v1:08c7d0ee…",
    "force": false,
    "granted_step": 2,
    "reason": "演示审批",
    "rejected": false,
    "signature": "sig:fc3bd1fc…",
    "tool_call_id": "call-002",
    "ttl_steps": 3
  }
}
```

`blocked`、`pending`、`writes`、`budget` 均未变化——批准票是恢复时唯一新增的信息，它把「签名 + 工区指纹 + 授权步号 + TTL」绑定在一起。

恢复判定（`plan_resume(approval_snapshot, fingerprint="fp:v1:08c7d0ee…", step=2)`）按固定优先级走查（直接传 halt 快照会因 `blocked` 无有效批准返回 `refuse` / `approval_required`）：

| 序 | 检查项 | 本 run 实际值 | 是否拦截 |
|---|---|---|---|
| 1 | `status ∈ {completed, cancelled}` | `waiting_approval` | 否 |
| 2 | `termination.unconfirmed` | `false` | 否 |
| 3 | 存在 `tests[].state == "intent"` | `tests = {}` | 否 |
| 4 | `snapshot.fingerprint != 当前指纹` | 两者均为 `fp:v1:08c7d0ee…` | 否 |
| 5 | `blocked` 是否存在有效批准 | 见下表 | 否（通过） |
| 6 | 存在 `writes[].state ∈ {intent, uncertain}` | `writes = {}` | 否 |
| 7 | 预算是否耗尽 | `2/8`、`2/40`、`0/3`、`0/3`、`0/120` | 否 |
| 8 | 默认分支 | — | 返回 `resume` |

第 5 步的有效性过滤（`_find_valid_approval`）：

| 过滤条件 | 实际值 | 结果 |
|---|---|---|
| `rejected` | `false` | 保留 |
| `consumed` | `false` | 保留 |
| `signature == blocked.signature` | 均为 `sig:fc3bd1fc…` | 保留 |
| `fingerprint == 当前指纹` | 均为 `fp:v1:08c7d0ee…` | 保留 |
| `step - granted_step > ttl_steps` | `2 - 2 = 0 ≤ 3` | 保留 |

最终结果：

```python
ResumePlan(action="resume", reason="可恢复", stop_reason=None,
           writes=[], tests=[], fingerprint_changed=False)
```

与审计 seq 10 的 `resume.plan / decision=resume / rule=可恢复` 一致。TTL 语义为 `step - granted_step ≤ ttl_steps`；本 run 批准与恢复都在 step 2，`diff = 0`，余量充足。若批准后又空转 4 步才重发，`diff = 4 > 3`，票失效，恢复会被拒绝。

恢复时序：

| 时刻 (UTC) | 事件 | 来源 |
|---|---|---|
| 08:17:53.856526 | Gate 判定 `require_approval`，写入 `blocked` | 快照 `blocked.at` |
| 08:17:53.861101 | 落盘停机快照 | 快照 `saved_at` |
| 08:17:53.863083 | 人工 `approve("call-002")` | 快照 `approvals.call-002.at` |
| 08:17:53.863966 | 落盘审批快照 | 快照 `saved_at` |
| 08:17:53.864689 | 审计 seq 9 `approval.granted` | audit.jsonl |
| 08:17:53.871719 | 审计 seq 10 `resume.plan` | audit.jsonl |
| 08:17:53.873667 | 审计 seq 11 `run.start(rule=resume)`，`step=2` | audit.jsonl |
| 08:17:53.877151 | 审计 seq 12 `gate.decision / allow / approval_granted`（`call-003`） | audit.jsonl |
| 08:17:53.881122 | 审计 seq 15 `write.applied`，`post_sha256=680df8de…` | audit.jsonl |

快照落盘（`08:17:53.863966`）早于审计写入（`08:17:53.864689`）：先持久化恢复依据，再写审计。

恢复前后的状态变化：

| 字段 | 恢复前（halt 快照） | 恢复后（`state.json` 终态） |
|---|---|---|
| `status` | `waiting_approval` | `completed` |
| `stop_reason` | `approval_required` | `completed` |
| `step` | `2` | `5` |
| `blocked` | `{reason: 等待人工审批, signature: sig:fc3bd1fc…}` | `null` |
| `pending` | `{call-002: …}` | `{}` |
| `approvals["call-002"].consumed` | `false` | `true` |
| `writes` | `{}` | `{sig:fc3bd1fc…: {state: done, post_sha256: 680df8de…}}` |
| `tests` | `{}` | `{sig:96196e97…: {state: done, scope: target}}` |
| `evidence` | `[]` | 1 条（`kind=test`, `returncode=0`） |
| `budget.used.writes` | `0` | `1` |
| `budget.used.steps` | `2.0` | `5.0` |
| `ledger["sig:fc3bd1fc…"].count` | `1` | `2` |
| `ledger["sig:fc3bd1fc…"].applied` | `false` | `true` |

`ledger.count = 2` 记录「请求两次」，`applied = true` 记录「只执行一次」，两者共同证明「批准后必须重发，且重发只放行一次」。

快照完整性：`save()` 先写 `.tmp-<ulid>` 再 `os.replace` 原子替换，并写入 `checksum = sha256(canonical_json(snapshot))`；`load()` 重算 checksum，不一致则返回 `None`，不参与恢复。`latest()` 按 `(step, mtime_ns)` 取最大：停机后（run 尚未结束时）读到的是更新的审批快照；本 run 最终完成，故最新快照为 `step-000005-run_end.json`。

`plan_resume()` 只给出动作，`resume()` 按动作落地：

- `refuse` → 按 `stop_reason` 置 `waiting_reconcile`（`workspace_changed`、`unknown_result`）或 `paused`（其余），保存 `*-resume-refused.json` 并抛出对应异常；`workspace_changed` 额外写回 `blocked`，保留旧指纹基线，防止二次 `resume()` 洗白。
- `reconcile` → 未传 `decisions` 时置 `waiting_reconcile`、保存 `*-reconcile.json` 并抛 `UnknownResult`；传入 `decisions` 则先 `reconcile()`，再以更新后的状态重新 `plan_resume()`，仍未通过则再次落快照并抛错。
- `resume` → 清 `blocked`、撤销 `pause` / `cancel` 信号、`sandbox.reset_cancel()`、把 `state.fingerprint` 刷新为当前值，写 `run.start(rule=resume)` 后以 `reset=False` 继续执行（不重建会话）。

同一条 approval 快照，改动单一条件即可触发对应拒绝分支：

| 改动 | `plan_resume` 返回 |
|---|---|
| `status` → `completed` | `refuse` / `completed`，`任务已是终态` |
| `termination.unconfirmed = true` | `refuse` / `unknown_result`，`上次进程退出未确认` |
| `tests` 加一条 `state="intent"` | `refuse` / `unknown_result`，`存在未销记的测试意图` |
| 当前指纹换成别的值 | `refuse` / `workspace_changed`，`旧证据与旧审批失效` |
| 删除批准项 | `refuse` / `approval_required`，`存在阻断标记` |
| `blocked.stop_reason` 改为 `policy_denied` | `refuse` / `policy_denied`，`存在阻断标记`（非审批阻断不能靠批准票放行） |
| `rejected = true` | 同上（被 rejected 过滤） |
| `consumed = true` | 同上（已消费，不可复用） |
| `granted_step = -2`（`diff = 4`） | 同上（TTL 过期） |
| 批准 `fingerprint` 换成别的值 | 同上（指纹不匹配） |
| `writes` 加一条 `state="intent"` | `reconcile` / `unknown_result`，`需人工三态对账` |
| `budget.used.steps = 8` | `refuse` / `budget_exceeded`，`预算已耗尽` |

复核脚本：

```python
import sys
sys.path.insert(0, r"c:\projet\homework\ch3\guarded_coding_harness\src")

from checkpoint import Checkpoint, plan_resume

cp = Checkpoint(r"c:\projet\homework\ch3\artifacts\harness-demo\checkpoints",
                run_id="17941258738370001993a4977")
latest = cp.latest()
print(latest["boundary"], latest["step"])        # -> run_end 5

halt = cp.load(cp.directory / "step-000002-halt-approval_required.json")
print(plan_resume(halt["snapshot"],
                  fingerprint=halt["snapshot"]["fingerprint"],
                  step=2).action)                # -> refuse（尚无批准）

approval = cp.load(cp.directory / "step-000002-approval.json")
print(plan_resume(approval["snapshot"],
                  fingerprint=approval["snapshot"]["fingerprint"],
                  step=2).action)                # -> resume
```

对 approval 快照把 `fingerprint` 换成其他值再调用，返回 `refuse` / `workspace_changed`。相关用例见 `tests/test_state_checkpoint.py`（恢复优先级、快照篡改、三态分类、台账释放）。

这段恢复说明四件事：停机不丢状态（`pending`/`blocked`/`ledger`/`budget` 全部落盘）；批准不等于执行（`writes` 仍为空）；恢复有前置条件（终态、未确认进程、未销记测试、指纹、批准有效期、预算任一不满足都会被拦下）；快照可自证（`checksum` 使篡改或半截写入被拒绝加载）。

---

## 6. 测试使用指南

### 6.1 测试文件与覆盖范围

共 74 个用例（74 passed）。

| 文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `tests/test_gate.py` | 13 | G1 参数校验、G2 重复调用、G3 未审批写入、G4 审批后只执行一次、S1 Gate 级越界、伪造 `test` 证据、批次内同签名写入/预算预占/整批中止/repeat_limit |
| `tests/test_sandbox.py` | 11 | S1 路径逃逸/敏感目标、S2 写区与前缀仿冒/链接逃逸（Windows 用 junction，免管理员）、S3 命令白名单/元字符/CWD、S4 超时与取消杀进程树、指纹排除运行时产物、遍历型读取的敏感文件与链接逃逸过滤、仅跳过根级 `artifacts` |
| `tests/test_state_checkpoint.py` | 14 | 三态分类、快照原子往返与篡改、`plan_resume` 优先级、台账释放、`begin_session` 拒绝未决意图、`abandon_tests` 销记后恢复、按 run 隔离、新进程 resume |
| `tests/test_audit.py` | 5 | A1 被拒调用可追溯、A2 事件序列与哈希链、`step.error`、篡改检测、尾部截断/清空检测 |
| `tests/test_harness_loop.py` | 22 | 端到端完成、H2 过期证据、H3 预算停机、C1–C4、S4 取消、暂停恢复、取消不可复活、纯文本容忍、批次中止不扣费、未决意图阻止新 run、`apply_patch` 产物登记、模型异常返回 `failed`、ReDoS 防护、大文件读取上限、`reject` 后恢复、状态映射一致性 |
| `tests/test_completion.py` | 9 | H1/H2 完成契约：必须是最新、当前指纹、非自报证据；取消/超时/强杀一律不通过 |

### 6.2 运行命令

所有命令都在 `guarded_coding_harness` 目录下执行。

```powershell
# PowerShell
# 全量（pyproject 已配置 addopts = "-v -ra"）：逐函数显示 + 末尾 passed/failed 统计
python -m pytest

# 精简圆点模式（临时切换，无需改配置）
python -m pytest -q     # 按文件分组显示圆点，仍保留末尾统计与汇总
python -m pytest -qq    # 最简：圆点进度 + 单行汇总

# 显式指定逐函数模式（与默认等价）
python -m pytest -v

# 单个文件
python -m pytest tests\test_gate.py -v

# 单个用例
python -m pytest "tests\test_gate.py::test_g4_approved_write_executes_once_then_duplicate_denied" -v

# 按关键字筛选（-k 支持子串、and、or、not）
python -m pytest -k "g4 or c3" -v
python -m pytest -k "not slow"

# 首个失败即停止 / 只重跑上次失败 / 显示失败详情
python -m pytest -x
python -m pytest --lf
python -m pytest -vv --tb=long

# 把完整输出归档（Tee-Object 同时写控制台与文件）
python -m pytest | Tee-Object -FilePath ..\artifacts\pytest-q.txt
```

```bash
# Git Bash：pytest 参数与 PowerShell 完全一致，只有路径分隔符不同
# 默认（addopts = "-v -ra"）：逐函数显示 + 末尾统计
python -m pytest

# 精简圆点模式
python -m pytest -q     # 按文件分组圆点，仍保留末尾统计
python -m pytest -qq    # 最简：圆点进度 + 单行汇总

# 显式指定逐函数模式（与默认等价）
python -m pytest -v
python -m pytest tests/test_gate.py -v
python -m pytest "tests/test_gate.py::test_g4_approved_write_executes_once_then_duplicate_denied" -v
python -m pytest -k "g4 or c3" -v
python -m pytest -k "not slow"
python -m pytest -x
python -m pytest --lf
python -m pytest -vv --tb=long

# Git Bash 用 tee 实现同样的“控制台 + 文件”双写
python -m pytest | tee ../artifacts/pytest-q.txt
```

两种环境的差异对照：

| 差异点 | PowerShell | Git Bash |
|---|---|---|
| 项目内路径 | `tests\test_gate.py` | `tests/test_gate.py` |
| 激活虚拟环境 | `.\.venv\Scripts\Activate.ps1` | `source ./.venv/Scripts/activate` |
| 输出双写 | `Tee-Object -FilePath <文件>` | `tee <文件>` |
| 用例节点语法 | `"文件::用例名"` | `"文件::用例名"`（相同） |
| 参数 `-k` / `-v` / `-x` / `--lf` / `--tb` | 相同 | 相同 |

`pyproject.toml` 中的关键配置：

```toml
[tool.pytest.ini_options]
pythonpath = ["src", "../smolagents/src"]
testpaths = ["tests"]
addopts = "-v -ra"
filterwarnings = ["ignore::DeprecationWarning"]
```

因此无需手动设置 `PYTHONPATH`，也无需 `pip install` 本项目即可运行测试。

### 6.3 结果解读

默认 `addopts` 为 `-v -ra`，输出为逐函数模式（每个用例单独一行 `PASSED`/`FAILED`），末尾给汇总：

```text
tests/test_gate.py::test_g1_unknown_argument_is_denied PASSED          [  1%]
...
tests/test_completion.py::test_h2_... PASSED                          [100%]

============================= 74 passed in ~9s =============================
```

圆点模式（用 `python -m pytest -q`，`-q` 会抵消 `addopts` 里的 `-v`）：

```text
...................................................................      [100%]
74 passed in 9.15s
```

| 符号 | 含义 |
|---|---|
| `.` | 通过 |
| `s` | 跳过 |
| `F` | 断言失败 |
| `E` | 用例内部异常 |

> 再追加一次 `-q`（即 `-qq`）会隐藏 `====` 装饰与 warnings 摘要，仅保留进度点和单行 `N passed`。

- 链接逃逸用例 `test_s2_symlink_escape_is_rejected_when_supported` 在 Windows 上改用 junction（`_winapi.CreateJunction`，无需管理员），会实际执行而非 `skip`；当前 74 个用例全部通过。
- 失败时先看 `-vv --tb=short` 的断言差异；涉及子进程的用例（`test_c3_resume_in_new_process_skips_completed_writes`、`test_s4_*`）失败时，检查 `conftest.child_env()` 拼出的 `PYTHONPATH` 是否包含 `src`、`../smolagents/src`、工区与 `tests`。
- 时间敏感用例（超时/取消）偶发失败可单独重跑确认：

```powershell
# PowerShell
python -m pytest tests\test_sandbox.py -k "s4" -v
```

```bash
# Git Bash
python -m pytest tests/test_sandbox.py -k "s4" -v
```

### 6.4 测试夹具与可复用组件

`tests/conftest.py` 提供：

| 名称 | 用途 |
|---|---|
| `workspace` fixture | 在 `tmp_path/repo` 下创建 `src/app.py`、`tests/test_*.py`、`artifacts/sleep.py` 等样例工区 |
| `make_harness(workspace, script=None, **policy_kwargs)` | 用 `ScriptedModel` + 指定策略装配 `GuardedHarness`，返回 `(harness, model)` |
| `ScriptedModel` | 按脚本逐轮返回工具调用的假模型；脚本耗尽后返回纯文本 |
| `call(name, **arguments)` | 构造一条脚本化工具调用 |
| `read(path)` | 读取 UTF-8 文本 |
| `child_env(workspace)` | 构造子进程环境变量（含正确 `PYTHONPATH`） |
| `SAMPLE_SOURCE` / `SAMPLE_SOURCE_PATCHED` / `SAMPLE_SOURCE_BROKEN` | 错误版 / 正确版 / 恒定错误版 `add` 实现 |
| `SAMPLE_TEST` / `SLOW_TEST` | 目标测试与超时测试样例 |

### 6.5 编写新测试

命名约定：

- 文件名：`test_<领域>.py`，放在 `tests/` 下
- 函数名：`test_<验收编号>_<被测行为>`，例如 `test_g4_approved_write_executes_once_then_duplicate_denied`
- 只通过公共 API 或 `conftest` 夹具准备环境，不要改全局状态

#### 示例 1：Gate 单元级用例（不跑完整循环）

```python
"""新增：Gate 拒绝未知参数。"""

from __future__ import annotations

from conftest import make_harness
from contracts import ActionRequest, Decision


def test_g1_unknown_argument_is_denied(workspace):
    harness, _ = make_harness(workspace, [])
    verdict = harness.gate.evaluate(
        ActionRequest(
            tool_call_id="call-x",
            tool_name="read_file",
            arguments={"path": "src/app.py", "bogus": 1},
            step=1,
        )
    )
    assert verdict.decision == Decision.DENY
    assert verdict.error.code == "unknown_argument"
```

#### 示例 2：端到端用例（审批 → resume → completed）

```python
"""新增：写入必须审批，审批后重发才执行。"""

from __future__ import annotations

from conftest import SAMPLE_SOURCE, SAMPLE_SOURCE_PATCHED, call, make_harness, read


def test_write_requires_approval_then_completes(workspace):
    harness, model = make_harness(
        workspace,
        [call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED)],
        required_scopes=("target",),
        require_write_approval=True,
        max_steps=6,
        repeat_limit=3,
    )

    first = harness.run("修复 add")
    assert first.status == "waiting_approval"
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE

    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认修改")

    # 审批后模型需重新发起同一写入，随后运行测试并提交。
    model.script.extend(
        [
            call("write_file", path="src/app.py", content=SAMPLE_SOURCE_PATCHED),
            call("run_test", scope="target"),
            call("final_answer", answer="已修复"),
        ]
    )
    second = harness.resume()

    assert second.status == "completed", second.message
    assert read(workspace / "src" / "app.py") == SAMPLE_SOURCE_PATCHED
```

#### 编写要点

1. **断言要看结果，不要只看状态**：除 `result.status` 外，同时断言文件内容、`state.latest_evidence()`、`audit.records()` 或 `checker.check()`。
2. **写操作用 `require_write_approval=False`** 可跳过审批专注测试其他行为；要测审批链路时保持默认 `True`。
3. **预算相关用例**显式设置 `max_steps` / `max_tool_calls` / `max_test_runs`，避免默认值掩盖问题。
4. **重复调用**受 `repeat_limit` 控制，构造连续同参调用时显式设置该值。
5. **需要子进程的用例**用 `conftest.child_env(workspace)` 提供环境，避免路径解析失败。
6. **需要真实重启验证**（如 C3）参照 `test_state_checkpoint.py` 中的 `CHILD_RESUME_SCRIPT` 模式。

---

## 7. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `ModuleNotFoundError: No module named 'smolagents'` | 直接运行 Python 脚本而未安装 smolagents；执行 `python -m pip install -e ..\smolagents`，或按脚本方式把 `../smolagents/src` 加入 `sys.path` |
| `ModuleNotFoundError: No module named 'harness'` | 未在项目根目录运行，或未把 `src` 加入 `sys.path`；用 `python -m pytest` 时由 `pyproject.toml` 自动处理 |
| `pytest: command not found` | 使用 `python -m pytest`，并确认 `python -m pip install "pytest>=8"` |
| `No module named pytest`（提示符已有 `(.venv)`） | 环境已激活，但 pytest 只装在全局环境；执行 `python -m pip install "pytest>=8"`，再用 `python -c "import sys; print(sys.executable)"` 确认解释器指向 `.venv` |
| `source .venv/bin/activate` 报错（Windows） | Windows 的激活脚本在 `Scripts/` 下：`source ./.venv/Scripts/activate`；PowerShell 用 `.\.venv\Scripts\Activate.ps1` |
| `python: command not found`（Git Bash） | 改用 `py -3.14`，或把 Python 安装目录加入 `PATH` |
| Git Bash 中路径被 MSYS 改写 | 临时加 `MSYS_NO_PATHCONV=1`，例如 `MSYS_NO_PATHCONV=1 some-command` |
| 一直停在 `waiting_approval` | 需要先 `approve(tool_call_id)`，再 `resume()`；模型需重新发起同一调用才会执行 |
| `WorkspaceChanged` | 审批/证据绑定的指纹与当前工区不一致；重新读取文件并重新审批，或确认是否为预期变更 |
| `UnknownResult` / `waiting_reconcile` | 上次写入结果未知；先 `reconcile()`，再 `resume()` |
| `PolicyDenied`：已取消任务不可复活 | `cancel()` 后需新建 `GuardedHarness` 实例重新 `run()` |
| `AuditAnchorError` / `verify()` 报「缺少头锚点」「尾部被截断」 | `audit.jsonl` 与 `audit.jsonl.head` 必须成对存在；删日志时要连 `.head` 一起删，截断后不能直接续写 |
| `search_code` 报「包含嵌套量词」 | `(a+)+`、`(a*)*` 等会触发灾难性回溯，已被静态拒绝；改写为 `a+` 等非嵌套等价形式，或缩小 `path` |
| `reject()` 后仍停在 `waiting_approval` | 说明还有其它待审批项；全部 `approve()` / `reject()` 处理完才会清除 `blocked`，之后 `resume()` 可继续 |
| `duplicate_write` 出现在同一批次 | 同批同签名写入只允许执行一次；模型重复调用属预期拒绝，整批中止后重新规划即可 |
| `test_s2_symlink_escape...` 被 skip | 该用例已改用 junction（`_winapi.CreateJunction`）实际执行，NTFS 上不再 skip；仅当文件系统不支持 junction（如 FAT/exFAT）时才会 skip |

---

## 8. 项目目录结构（代码文件）

```text
guarded_coding_harness/
├── .env.example                    # 环境变量模板（真实 .env 不提交）
├── .gitignore                      # 忽略密钥、虚拟环境与缓存
├── README.md                       # 本文件
├── pyproject.toml                  # 打包、依赖与 pytest 配置
├── scripts/
│   ├── demo_run.py                 # 离线演示：审批 → resume → completed
│   ├── real_model_run.py           # 真实模型演示（DeepSeek）
│   └── real_model_approval_run.py  # 真实模型 + 完整审批链路（自动加载 .env）
├── src/
│   ├── contracts.py                # 枚举、异常、数据类、纯函数
│   ├── gate.py                     # 参数/权限/风险/审批/重复/预算判定
│   ├── sandbox.py                  # 路径与命令约束、超时、进程树清理
│   ├── state_store.py              # 状态、预算、台账、三态写入与证据
│   ├── checkpoint.py               # 原子快照与恢复计划
│   ├── audit.py                    # append-only 哈希链审计
│   └── harness.py                  # 装配层与 GuardedToolCallingAgent
└── tests/
    ├── conftest.py                 # 共享 fixture 与 ScriptedModel
    ├── test_gate.py                # G1–G4、S1（Gate 级）
    ├── test_sandbox.py             # S1–S4、工作区指纹
    ├── test_state_checkpoint.py    # 三态、快照、恢复计划、C1/C3
    ├── test_audit.py               # A1/A2、篡改检测
    ├── test_harness_loop.py        # 端到端 H2/H3、C1–C4、S4、暂停/取消
    └── test_completion.py          # H1/H2 完成契约
```

运行时生成（非源码，默认位于 `<workspace>/artifacts/.harness/`）：

```text
artifacts/.harness/
├── state.json
├── audit.jsonl
├── audit.jsonl.head
├── checkpoints/<run_id>/step-XXXXXX-<boundary>.json
└── logs/<ulid>.log
```
