# Guarded Coding Harness

基于 [smolagents](https://github.com/huggingface/smolagents) `ToolCallingAgent` 的受控编码执行框架。它不改写内核，只覆写 `process_tool_calls` 一处拦截缝，在工具真正执行前完成参数校验、权限与路径约束、风险分级、写审批、重复调用拦截、预算控制，并把每一步的决策、写入、测试证据、Checkpoint 和审计链落盘。

完成契约只认真实测试证据（`kind` 为 `test` / `test_run`，且 `returncode == 0`、指纹与当前工区一致）；`note`、`finding`、`check` 等自报记录不能作为交付物。

---

## 1. 环境要求

| 项目 | 要求 | 本项目验证环境 |
|---|---|---|
| 操作系统 | Windows / Linux / macOS | Windows（PowerShell 5.1、Git Bash） |
| Python | >= 3.10 | 3.14.7 |
| pytest | >= 8 | 8.4.2 |
| smolagents | 本地源码或 PyPI 包 | 同级目录 `../smolagents`（1.27.0.dev0） |
| 虚拟环境 | **必须使用独立的 venv / conda / uv 环境** | 项目根目录 `.venv/`（已被 `.gitignore` 忽略）；若本项目被当作 Harness workspace，则建在 `ch3/.venvs/guarded/` |

目录约定：`pyproject.toml` 中 `pythonpath = ["src", "../smolagents/src"]` 要求 smolagents 源码位于与本项目同级的 `../smolagents`。本项目的完整目录结构见第 8 节。

**为什么必须隔离**：

1. `pip install -e ../smolagents` 会带入 `transformers`、`huggingface-hub`、`requests` 等传递依赖，装进全局环境容易与其他项目冲突。
2. `HarnessPolicy.suite_command()` 用 `sys.executable` 启动子进程执行 pytest；隔离环境保证主进程、pytest 与被测仓库用同一解释器和同一套依赖。
3. 避免 PyPI 版 smolagents 与 `../smolagents/src` 本地源码混用，排查「到底加载了哪一份」非常费时。
4. 项目要求 `>=3.10`，用 venv 固定解释器版本，不受机器上其他 Python 影响。

> 注意：如果本项目本身就是 `GuardedHarness` 的 workspace，虚拟环境不要放进被指纹覆盖的位置。`workspace_fingerprint()` 只跳过 `artifacts`、`.harness`、`.git`、`__pycache__`、`.pytest_cache`，`.venv` 会被整体计入指纹，导致指纹计算变慢、测试证据频繁「过期」。稳妥做法是把环境建在项目外，例如 `c:\projet\homework\ch3\.venvs\guarded`。

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
- `pytest --collect-only` 显示 `54 tests collected`。

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

脚本只读取进程环境变量，不会自动解析 `.env`：请直接设置 `DEEPSEEK_API_KEY`，或在运行前加载该文件（Git Bash：`set -a; source .env; set +a`）。

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HARNESS_REAL_MODEL` | `deepseek-v4-pro` | 模型 id |
| `HARNESS_REAL_BASE` | `https://api.deepseek.com/v1` | OpenAI 兼容 API base |

产物写入 `ch3/artifacts/real-model-demo/`。脚本会驱动真实的 read → write → test → final_answer 流程，并在 `run-summary.md` 中给出 Diff、测试日志尾部、证据指纹与审计时间线。

> 注意：脚本启动时会清理上一轮的 `audit.jsonl`、`state.json`、`checkpoints`、`logs`、`run-summary.md`，只保留最新一次运行。

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

### 4.3 审批与恢复

```python
first = harness.run("修复 add")
if first.status == "waiting_approval":
    pending = harness.state.pending_items()[0]
    harness.approve(pending["tool_call_id"], actor="reviewer", reason="确认修改")
    second = harness.resume()
```

- `approve()` 会把审批绑定到「调用签名 + 当前工区指纹 + 步骤」，工区变化后旧审批自动失效（`WorkspaceChanged`）。
- `reject()` 直接拒绝待审批项。
- `resume()` 会先读取最新 Checkpoint，再执行 `plan_resume()`：终态、未确认进程、未销记测试意图、指纹变化、未决写入都会阻止恢复。

### 4.4 对账未知写入

```python
if result.status == "waiting_reconcile":
    # decisions: {signature 或 tool_call_id: "executed" | "not_executed" | "uncertain"}
    harness.reconcile()
    result = harness.resume()
```

`classify_write()` 依据写入前 sha256、写入后 sha256 与当前文件内容判定 `executed` / `not_executed` / `uncertain`；人工结论与现场不一致时会降级为 `uncertain`。

### 4.5 暂停与取消

```python
harness.pause()    # 当前步结束后停在 paused，可 resume()
harness.cancel()   # 终止当前测试进程树，进入 cancelled
harness.reset_cancel()  # 仅解除取消信号；已取消的任务不可复活
```

### 4.6 查看状态、审计与快照

```python
harness.state.data          # 完整状态：plan / budget / evidence / writes / tests / pending
harness.state.latest_evidence("target")
harness.audit.by_run(harness.run_id)
harness.audit.verify()      # -> (True, "ok")
harness.checkpoints.latest()
harness.checkpoints.list_snapshots()
harness.checker.check()     # -> (bool, message)
```

### 4.7 工具一览

| 工具 | 风险 | 参数 | 说明 |
|---|---|---|---|
| `list_files` | READ | `path="."`, `max_entries=50` | 列目录 |
| `search_code` | READ | `pattern`, `path="."` | 文本检索 |
| `read_file` | READ | `path`, `start_line=1`, `max_lines=200` | 读文件 |
| `inspect_workspace` | READ | 无 | 工区摘要 |
| `write_file` | WRITE | `path`, `content` | 原子写，需审批（可配置） |
| `apply_patch` | WRITE | `path`, `old_text`, `new_text` | 精确替换，需审批 |
| `run_test` | CONTROL | `scope` | 运行 `policy.suites` 中配置的套件 |
| `record_evidence` | READ | `scope`, `summary`, `kind="note"`, `ok=True` | 只能记录辅助证据，不能完成交付 |
| `update_plan` | CONTROL | `items: list[str]` | 更新计划（1–12 条） |
| `final_answer` | CONTROL | `answer` | 完成契约校验不通过会被拒绝 |

所有 READ/WRITE 路径都经过 `Sandbox.resolve()`：拒绝 `..` 逃逸、软链接逃逸、敏感目录（`.ssh`、`.aws`、`.gnupg`、`.docker`、`.kube`）、敏感文件名与后缀（`id_rsa`、`.pem`、`.key`、`.env*` 等）。

---

## 5. 运行时产物

默认目录：`<workspace>/artifacts/.harness/`。演示脚本通过 `harness_dir` 重定向到 `ch3/artifacts/<demo>/`。

| 文件/目录 | 说明 |
|---|---|
| `state.json` | 计划、预算、台账、证据、待审批、未决写入/测试 |
| `audit.jsonl` | append-only 审计链，每条含 `seq`、`prev_hash`、`hash` |
| `checkpoints/<run_id>/step-XXXXXX-<boundary>.json` | 按 run 隔离的原子快照 |
| `logs/<ulid>.log` | 命令/测试进程输出 |

校验审计链：

```python
ok, message = harness.audit.verify()
assert ok, message
```

### 5.1 为什么 `ch3/artifacts` 不放在本项目内

`ch3/artifacts` 存放的是**作业交付证据**（`pytest-q.txt`、两套 demo 的 `state.json` / `audit.jsonl` / `checkpoints` / `logs` / `run-summary.md`），不是 `guarded_coding_harness` 的源码或运行时依赖。保持与项目同级的原因：

| 维度 | 放在 `ch3/artifacts`（现状） | 放到 `guarded_coding_harness/artifacts` |
|---|---|---|
| 证据与实现分离 | 源码树只含 7 个模块、6 个测试与 2 个脚本 | 审计日志、工区副本与源码混在同一棵树 |
| 打包边界 | 安装/打 sdist 时天然不包含证据 | 需额外配置排除，否则有把审计记录与工区副本带入发行包的风险 |
| pytest 收集 | `testpaths = ["tests"]` 只指向项目测试 | 显式运行 `python -m pytest .` 或 IDE 自动发现时，工区内的 `workspace/tests/test_target.py` 可能被误收集 |
| 生命周期 | demo 可整目录清理重建，不影响源码 | `clean_old_run()` 删除的是项目内目录，误删风险更高 |
| 可重复生成 | 每次运行生成新 `run_id`，旧证据可整体归档 | 需在 `.gitignore` 与提交物之间反复权衡 |

两个脚本中的 `ROOT = Path(__file__).resolve().parents[2]` 与 `DEMO_DIR = ROOT / "artifacts" / ...` 正是按此约定实现的。Harness 自身的默认运行时目录仍是工区内的 `<workspace>/artifacts/.harness/`，与交付证据目录互不影响。

### 5.2 为什么 `.screenshots` 没有必要放进本项目

`ch3/.screenshots` 当前为空目录，项目内（源码、测试、脚本、`pyproject.toml`、README）对它零引用；它既不是运行时依赖，也不参与测试或打包。因此：

- **没有移动的必要**：移动一个空目录不会改变任何逻辑，也不会提升可复现性，只会把未使用的目录搬进源码树。
- **移动反而有副作用**：它容易被误认为项目资产，增加误提交、误打包与目录噪声。
- **若将来确有截图需求**：建议放到 `ch3/artifacts/screenshots/` 或课程提交目录，并在截图真正被文档或脚本引用后，再决定是否纳入项目。

### 5.3 完整运行记录 + 审计记录 + checkpoint恢复记录

以下两条记录取自仓库内已落盘的产物，可直接用 `python scripts/demo_run.py` 与 `python scripts/real_model_run.py` 复现。每次运行的 `run_id` 与哈希不同，但事件结构、证据契约与完成判定一致。

#### 记录 A：离线演示（`ScriptedModel`，确定性，含审批停机）

产物目录：`ch3/artifacts/harness-demo/`

| 项目 | 值 |
|---|---|
| run_id | `17940453828090001ebedd3b4` |
| session | `17940453828090002de43bc85` |
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
  "duration": 1.2118,
  "fingerprint": "fp:v1:b2ef38b713687dda0699a3547e599dc4f6945490570859742887b84b6704de9",
  "argv": ["C:\\Python314\\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_target.py"]
}
```

测试日志（`logs/17940453828530016e9a0257a.log`）：

```text
.                                                                        [100%]
1 passed in 0.00s
```

Checkpoint（按 run 隔离，共 8 个）：

```text
checkpoints/17940453828090001ebedd3b4/
├── step-000001-after_step.json              status=running
├── step-000002-after_step.json              status=running
├── step-000002-approval.json                status=waiting_approval
├── step-000002-halt-approval_required.json  status=waiting_approval
├── step-000003-after_step.json              status=running
├── step-000004-after_step.json              status=running
├── step-000005-after_step.json              status=running
└── step-000005-run_end.json                 status=completed
```

审计时间线（22 条，append-only + 哈希链）：

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
| 22 | `run.halt` | `completed` | `run_end` | — |

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
| run_id | `179404614736900014d88381b` |
| session | `179404614736900025043c6a7` |
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
| 1 | `call_00_9EyTt0WfPrltYYRdp9Cf0800` | `read_file` | `path=src/app.py` | `allow` / `allowed` | 读取错误实现 |
| 2 | `call_00_ZJIQhttsWGU0fWmH0Dgn5890` | `write_file` | `path=src/app.py` | `allow` / `allowed` | `write.intent` → `write.applied`，`post_sha256=1098a821…` |
| 3 | `call_00_ET_a2bV1o0ajyBznRh3Osnf5772` | `run_test` | `scope=target` | `allow` / `allowed` | `test.result`，`returncode=0`，耗时 0.4573s |
| 4 | `call_00_ET_8N3mA88IEgC8lECWjtJb9190` | `final_answer` | `answer=已修复 add` | `allow` / `allowed` | `run.halt` / `completed` |

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
  "duration": 0.4573,
  "fingerprint": "fp:v1:637d4f2242dcf639e9ec260ae76ca3aab20da1fbd72dfb000d78f7ddc0630896",
  "argv": ["C:\\projet\\homework\\ch3\\guarded_coding_harness\\.venv\\Scripts\\python.exe", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_target.py"]
}
```

测试日志（`logs/179404615143700099f358c27.log`）：

```text
.                                                                        [100%]
1 passed in 0.00s
```

Checkpoint（共 5 个）：

```text
checkpoints/179404614736900014d88381b/
├── step-000001-after_step.json  status=running
├── step-000002-after_step.json  status=running
├── step-000003-after_step.json  status=running
├── step-000004-after_step.json  status=running
└── step-000004-run_end.json     status=completed
```

审计时间线（14 条）：

| seq | event | decision | rule | tool_call_id |
|---|---|---|---|---|
| 1 | `run.start` | `start` | `run` | — |
| 2 | `gate.decision` | `allow` | `allowed` | `call_00_9EyTt0WfPrltYYRdp9Cf0800` |
| 3 | `gate.batch` | `batch` | `commit` | — |
| 4 | `gate.decision` | `allow` | `allowed` | `call_00_ZJIQhttsWGU0fWmH0Dgn5890` |
| 5 | `gate.batch` | `batch` | `commit` | — |
| 6 | `write.intent` | `intent` | `write_file` | `call_00_ZJIQhttsWGU0fWmH0Dgn5890` |
| 7 | `write.applied` | `allow` | `write_file` | `call_00_ZJIQhttsWGU0fWmH0Dgn5890` |
| 8 | `gate.decision` | `allow` | `allowed` | `call_00_ET_a2bV1o0ajyBznRh3Osnf5772` |
| 9 | `gate.batch` | `batch` | `commit` | — |
| 10 | `test.intent` | `intent` | `run_test` | `call_00_ET_a2bV1o0ajyBznRh3Osnf5772` |
| 11 | `test.result` | `allow` | `run_test` | `call_00_ET_a2bV1o0ajyBznRh3Osnf5772` |
| 12 | `gate.decision` | `allow` | `allowed` | `call_00_ET_8N3mA88IEgC8lECWjtJb9190` |
| 13 | `gate.batch` | `batch` | `commit` | — |
| 14 | `run.halt` | `completed` | `run_end` | — |

#### 两条记录的差异与共同点

| 维度 | 记录 A（离线） | 记录 B（真实模型） |
|---|---|---|
| 决策来源 | `ScriptedModel` 固定脚本 | `deepseek-v4-pro` 实时生成 |
| 是否调用 `update_plan` | 是（revision=1） | 否（`plan.items=[]`） |
| 写审批链路 | 触发 `waiting_approval`，人工批准后同签名重发 | 策略关闭审批，直接 `allow` |
| 步数 / 工具调用 | 5 / 5 | 4 / 4 |
| 审计事件数 | 22 | 14 |
| 测试解释器 | 系统 Python `C:\Python314\python.exe` | 项目 `.venv\Scripts\python.exe` |
| 写入 `post_sha256` | `680df8de…`（内容含结尾换行） | `1098a821…`（内容无结尾换行，落盘为 CRLF） |
| 测试耗时 | 1.2118s | 0.4573s |

共同点：两者的唯一交付证据都是 `kind=test`、`scope=target`、`returncode=0`，且证据指纹等于当前工区指纹；`note`/`finding`/`check` 均未参与完成判定；`final_answer` 都经过 `CompletionChecker` 才被接受。

> 说明：记录 B 的 `run-summary.md` 中「最终文件 sha256」为 LF 归一化后的内容哈希（`8f75a686…`，31 字节），而 `state.json → writes[].post_sha256` 对应磁盘原始字节（含 CRLF，32 字节，`1098a821…`）。两者不同属预期，验收以 `state.json` 与实际文件 `Get-FileHash` 为准。

复现命令：

```bash
# 记录 A
python scripts/demo_run.py

# 记录 B（需 openai SDK 与 DEEPSEEK_API_KEY，见 2.2 方式 C、3.2）
set -a; source .env; set +a
python scripts/real_model_run.py
```

#### 记录 A 的审计记录（`ch3/artifacts/harness-demo/audit.jsonl`）

基本信息：

| 项目 | 值 |
|---|---|
| run_id | `17940453828090001ebedd3b4` |
| 记录条数 | 22，`seq` 1–22 连续 |
| 时间跨度 | `09:56:22.811691Z` → `09:56:24.085242Z`（约 1.27s） |
| 首条哈希（锚点） | `h:dfee3b5f97696da08dae145fec457c1b73f5bc3b310a34e8ac1d49991234d2a3` |
| 末条哈希（链尾） | `h:18a508901940262004848c085cd9e2ae580c909e432b9a7c92f56e4f5052bef8` |
| actor 分布 | human 3、harness 14、model 5 |
| 文件形态 | append-only JSONL，无删除、无改写 |

每条记录包含 `seq`、`ts`、`event`、`run_id`、`tool_call_id`、`actor`、`decision`、`rule`、`detail`、`prev_hash`、`hash`，其中：

```text
hash = "h:" + sha256(prev_hash + canonical_json(body))
```

`body` 为除 `hash` 外的全部字段，首条 `prev_hash = "genesis"`。任何字段被改动，从该条起后续哈希全部失配。

完整事件序列：

| seq | 时间 (UTC) | actor | event | decision | rule | tool_call_id | hash 前缀 |
|---|---|---|---|---|---|---|---|
| 1 | 09:56:22.811691 | human | `run.start` | `start` | `run` | — | `dfee3b5f9769` |
| 2 | 09:56:22.821042 | harness | `gate.decision` | `allow` | `allowed` | `call-001` | `a59fc6fe96cf` |
| 3 | 09:56:22.822679 | harness | `gate.batch` | `batch` | `commit` | — | `9fddffbcd377` |
| 4 | 09:56:22.823014 | model | `plan.update` | `allow` | `update_plan` | `call-001` | `adcdf0dd336a` |
| 5 | 09:56:22.827067 | harness | `gate.decision` | `require_approval` | `write_requires_approval` | `call-002` | `63380cd02fa3` |
| 6 | 09:56:22.827293 | harness | `approval.requested` | `require_approval` | `write_requires_approval` | `call-002` | `0412b9d35b73` |
| 7 | 09:56:22.827455 | harness | `gate.batch` | `batch` | `commit` | — | `18a75ed9f27d` |
| 8 | 09:56:22.832210 | harness | `run.halt` | `approval_required` | `ApprovalRequired` | — | `e4d942a4feb4` |
| 9 | 09:56:22.834587 | human | `approval.granted` | `allow` | `approved` | `call-002` | `1d350cc2694a` |
| 10 | 09:56:22.842719 | harness | `resume.plan` | `resume` | `可恢复` | — | `d27bee442eaf` |
| 11 | 09:56:22.843960 | human | `run.start` | `resume` | `resume` | — | `890838bc4562` |
| 12 | 09:56:22.846832 | harness | `gate.decision` | `allow` | `approval_granted` | `call-003` | `cb5cb8f67abb` |
| 13 | 09:56:22.847029 | harness | `gate.batch` | `batch` | `commit` | — | `10693c9a6ecb` |
| 14 | 09:56:22.847761 | model | `write.intent` | `intent` | `write_file` | `call-003` | `47b1df2cadfa` |
| 15 | 09:56:22.849363 | model | `write.applied` | `allow` | `write_file` | `call-003` | `7fc1d5ca5cb3` |
| 16 | 09:56:22.852353 | harness | `gate.decision` | `allow` | `allowed` | `call-004` | `4d519b21cb1b` |
| 17 | 09:56:22.852566 | harness | `gate.batch` | `batch` | `commit` | — | `e68a338e6016` |
| 18 | 09:56:22.852785 | model | `test.intent` | `intent` | `run_test` | `call-004` | `7cb6be33d3ed` |
| 19 | 09:56:24.075896 | model | `test.result` | `allow` | `run_test` | `call-004` | `cc0a0013653c` |
| 20 | 09:56:24.078914 | harness | `gate.decision` | `allow` | `allowed` | `call-005` | `f6076e25d61b` |
| 21 | 09:56:24.079166 | harness | `gate.batch` | `batch` | `commit` | — | `3bf14c8264c3` |
| 22 | 09:56:24.085242 | harness | `run.halt` | `completed` | `run_end` | — | `18a508901940` |

表中哈希为 `h:` 之后的前 12 位，完整值见源文件。

按 `tool_call_id` 溯源：

| tool_call_id | seq | 语义 |
|---|---|---|
| `call-001` | 2、4 | 计划提交与落盘 |
| `call-002` | 5、6、9 | 写入被拦 → 请求审批 → 人工批准（未执行写入） |
| `call-003` | 12、14、15 | 凭批准票放行 → 写入意图 → 写入完成 |
| `call-004` | 16、18、19 | 放行 → 测试意图 → 测试结果（`returncode=0`） |
| `call-005` | 20 | 提交答案 |

`call-002` 与 `call-003` 签名相同（`sig:fc3bd1fc…`），但审计能区分「被拦截的一次」与「凭批准票执行的一次」，不会混为一次。

关键安全不变量与对应证据：

| 不变量 | 审计证据 |
|---|---|
| 写入前必须先有门禁判定 | seq 12（`allow`）在 seq 14（`write.intent`）之前 |
| 未审批不落盘 | seq 5–8 之间没有 `write.intent`，文件未变 |
| 审批绑定工区指纹 | seq 6 与 seq 9 的 `fingerprint` 均为 `fp:v1:08c7d0ee…` |
| 写入前后可对账 | seq 14 `pre=828092436119…`；seq 15 `pre=828092436119…`、`post=680df8de3e5c…` |
| 测试结果带指纹 | seq 19 `fingerprint=fp:v1:b2ef38b7…`，与写入后工区一致 |
| 完成前必有测试证据 | seq 19（`test.result`）在 seq 22（`run.halt/completed`）之前 |
| 停机可恢复且留痕 | seq 8 `run.halt` → seq 10 `resume.plan` → seq 11 `run.start(rule=resume)` |

本 run 未出现（说明未被触发，而非缺失）的事件：`DENY`、`duplicate_write`、`repeat_limit`、`budget_exceeded`、`workspace_changed`、`unknown_result`。

复核脚本：

```python
import sys
sys.path.insert(0, r"c:\projet\homework\ch3\guarded_coding_harness\src")

from audit import AuditLog

log = AuditLog(r"c:\projet\homework\ch3\artifacts\harness-demo\audit.jsonl")
print(log.verify())                    # -> (True, 'ok')
print(len(log.records()))              # -> 22
print(log.trace("call-002"))           # -> 3 条
print(log.by_run("17940453828090001ebedd3b4")[-1]["event"])   # -> run.halt
```

篡改检测：修改任意一条记录的 `decision` 后调用 `verify()`，会返回 `(False, "哈希校验失败：第 N 条")`；删除中间一条则先报 `seq 不连续`。相关用例见 `tests/test_audit.py`。

原始记录样例（锚点与停机、审批）：

```json
{"actor":"human","decision":"start","detail":{"fingerprint":"fp:v1:08c7d0ee9e52bf444ce0f485fa76da4730ed447280a926a0bced5aa5e12d9a6f","session":"17940453828090002de43bc85","task":"修复 add 函数"},"event":"run.start","hash":"h:dfee3b5f97696da08dae145fec457c1b73f5bc3b310a34e8ac1d49991234d2a3","prev_hash":"genesis","rule":"run","run_id":"17940453828090001ebedd3b4","seq":1,"tool_call_id":null,"ts":"2026-11-07T09:56:22.811691+00:00"}
{"actor":"harness","decision":"approval_required","detail":{"message":"写操作需要人工审批","step":2},"event":"run.halt","hash":"h:e4d942a4feb477e0eb1a289bdf85f94f1b629fad54dc2d89788488fb0a05c45b","prev_hash":"h:18a75ed9f27d856498f5f18c6dc51fe16005b9fabcbac2917be86cf8b0f01a18","rule":"ApprovalRequired","run_id":"17940453828090001ebedd3b4","seq":8,"tool_call_id":null,"ts":"2026-11-07T09:56:22.832210+00:00"}
{"actor":"human","decision":"allow","detail":{"fingerprint":"fp:v1:08c7d0ee9e52bf444ce0f485fa76da4730ed447280a926a0bced5aa5e12d9a6f","reason":"演示审批","signature":"sig:fc3bd1fca0d7daee26a4c6707407eda12b2ae95d638dcba1eddb91dc6ae75d48","ttl_steps":3},"event":"approval.granted","hash":"h:1d350cc2694aafdcacde543aaaf1530376a963b745531b40aecd03bcd05f7b3d","prev_hash":"h:e4d942a4feb477e0eb1a289bdf85f94f1b629fad54dc2d89788488fb0a05c45b","rule":"approved","run_id":"17940453828090001ebedd3b4","seq":9,"tool_call_id":"call-002","ts":"2026-11-07T09:56:22.834587+00:00"}
{"actor":"harness","decision":"completed","detail":{"step":5,"stop_reason":"completed"},"event":"run.halt","hash":"h:18a508901940262004848c085cd9e2ae580c909e432b9a7c92f56e4f5052bef8","prev_hash":"h:3bf14c8264c303b1f9ff870a7105a277a74f747c1aae58c48f31ae4367bd87a2","rule":"run_end","run_id":"17940453828090001ebedd3b4","seq":22,"tool_call_id":null,"ts":"2026-11-07T09:56:24.085242+00:00"}
```

#### 记录 A 的 Checkpoint 恢复记录

数据来源：`ch3/artifacts/harness-demo/checkpoints/17940453828090001ebedd3b4/`，对应「写入被拦 → 停机 → 批准 → 恢复」这一段。

快照清单（按 run 隔离，共 8 个）：

```text
checkpoints/17940453828090001ebedd3b4/
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
| `saved_at` | `2026-11-07T09:56:22.831398+00:00` |
| `checksum` | `e1e4bd12a4386a1ec3e8d8fc52e98601cde3792921b670d22e7dc120b01bae4e` |

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

审批快照 `step-000002-approval.json`（`checksum=7bc16ba9c46c5ff5…`，`saved_at=09:56:22.833790`）与停机快照的唯一实质差异，是 `approvals` 由 `{}` 变为：

```json
{
  "call-002": {
    "actor": "human",
    "at": "2026-11-07T09:56:22.832880+00:00",
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

恢复判定（`plan_resume(halt_snapshot, fingerprint="fp:v1:08c7d0ee…", step=2)`）按固定优先级走查：

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
| 09:56:22.827289 | Gate 判定 `require_approval`，写入 `blocked` | 快照 `blocked.at` |
| 09:56:22.831398 | 落盘停机快照 | 快照 `saved_at` |
| 09:56:22.832880 | 人工 `approve("call-002")` | 快照 `approvals.call-002.at` |
| 09:56:22.833790 | 落盘审批快照 | 快照 `saved_at` |
| 09:56:22.834587 | 审计 seq 9 `approval.granted` | audit.jsonl |
| 09:56:22.842719 | 审计 seq 10 `resume.plan` | audit.jsonl |
| 09:56:22.843960 | 审计 seq 11 `run.start(rule=resume)`，`step=2` | audit.jsonl |
| 09:56:22.846832 | 审计 seq 12 `gate.decision / allow / approval_granted`（`call-003`） | audit.jsonl |
| 09:56:22.849363 | 审计 seq 15 `write.applied`，`post_sha256=680df8de…` | audit.jsonl |

快照落盘（`22.833790`）早于审计写入（`22.834587`）：先持久化恢复依据，再写审计。

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

快照完整性：`save()` 先写 `.tmp-<ulid>` 再 `os.replace` 原子替换，并写入 `checksum = sha256(canonical_json(snapshot))`；`load()` 重算 checksum，不一致则返回 `None`，不参与恢复。`latest()` 按 `(step, mtime_ns)` 取最大，因此停机后读到的是更新的审批快照。

同一条 halt 快照，改动单一条件即可触发对应拒绝分支：

| 改动 | `plan_resume` 返回 |
|---|---|
| `status` → `completed` | `refuse` / `completed`，`任务已是终态` |
| `termination.unconfirmed = true` | `refuse` / `unknown_result`，`上次进程退出未确认` |
| `tests` 加一条 `state="intent"` | `refuse` / `unknown_result`，`存在未销记的测试意图` |
| 当前指纹换成别的值 | `refuse` / `workspace_changed`，`旧证据与旧审批失效` |
| 删除批准项 | `refuse` / `approval_required`，`存在阻断标记` |
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
                run_id="17940453828090001ebedd3b4")
latest = cp.latest()
print(latest["boundary"], latest["step"])        # -> approval 2

halt = cp.load(cp.directory / "step-000002-halt-approval_required.json")
print(plan_resume(halt["snapshot"],
                  fingerprint=halt["snapshot"]["fingerprint"],
                  step=2).action)                # -> resume
```

把 `fingerprint` 换成其他值再调用，返回 `refuse` / `workspace_changed`；传入 `approval` 快照则同样返回 `resume`。相关用例见 `tests/test_state_checkpoint.py`（恢复优先级、快照篡改、三态分类、台账释放）。

这段恢复说明四件事：停机不丢状态（`pending`/`blocked`/`ledger`/`budget` 全部落盘）；批准不等于执行（`writes` 仍为空）；恢复有前置条件（指纹、未决测试、未确认进程、批准有效期缺一不可）；快照可自证（`checksum` 使篡改或半截写入被拒绝加载）。

---

## 6. 测试使用指南

### 6.1 测试文件与覆盖范围

共 54 个用例（53 passed + 1 skipped）。

| 文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `tests/test_gate.py` | 9 | G1 参数校验、G2 重复调用、G3 未审批写入、G4 审批后只执行一次、S1 Gate 级越界、伪造 `test` 证据 |
| `tests/test_sandbox.py` | 8 | S1 路径逃逸/敏感目标、S2 写区与前缀仿冒/软链接、S3 命令白名单/元字符/CWD、S4 超时与取消杀进程树、指纹排除运行时产物 |
| `tests/test_state_checkpoint.py` | 13 | 三态分类、快照原子往返与篡改、`plan_resume` 优先级、台账释放、`begin_session` 拒绝未决意图、按 run 隔离、新进程 resume |
| `tests/test_audit.py` | 4 | A1 被拒调用可追溯、A2 事件序列与哈希链、`step.error`、篡改检测 |
| `tests/test_harness_loop.py` | 12 | 端到端完成、H2 过期证据、H3 预算停机、C1–C4、S4 取消、暂停恢复、取消不可复活 |
| `tests/test_completion.py` | 8 | H1/H2 完成契约：必须是最新、当前指纹、非自报证据 |

### 6.2 运行命令

所有命令都在 `guarded_coding_harness` 目录下执行。

```powershell
# PowerShell
# 全量（pyproject 已配置 testpaths 与 -q，无需再追加 -q）
python -m pytest

# 全量并显示每个用例
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
python -m pytest
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
addopts = "-q"
filterwarnings = ["ignore::DeprecationWarning"]
```

因此无需手动设置 `PYTHONPATH`，也无需 `pip install` 本项目即可运行测试。

### 6.3 结果解读

典型输出：

```text
...................................s..................                   [100%]
53 passed, 1 skipped in 13.71s
```

| 符号 | 含义 |
|---|---|
| `.` | 通过 |
| `s` | 跳过 |
| `F` | 断言失败 |
| `E` | 用例内部异常 |

> `addopts` 已包含 `-q`。若命令行再追加一次 `-q`，实际会变成 `-qq`，汇总行会被隐藏，只剩进度点；需要看到 `53 passed, 1 skipped` 时请直接运行 `python -m pytest`。

- **1 skipped** 固定为 `test_s2_symlink_escape_is_rejected_when_supported`：Windows 默认无创建符号链接权限，用例会自动 `pytest.skip`。开启「开发者模式」或以管理员身份运行后可实际执行。
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
| `test_s2_symlink_escape...` 被 skip | Windows 无符号链接权限，属预期；开启开发者模式或管理员运行 |

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
│   └── real_model_run.py           # 真实模型演示（DeepSeek）
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
├── checkpoints/<run_id>/step-XXXXXX-<boundary>.json
└── logs/<ulid>.log
```
