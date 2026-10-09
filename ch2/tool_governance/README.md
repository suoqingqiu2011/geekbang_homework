# 工具治理演示框架（Tool Governance Demo）

本目录包含一个 Python 治理框架演示项目，模拟 AI Agent 调用工具时的完整链路：
**参数校验 → 权限判断 → 业务预检 → 人工审批 → 超时处理 → 结果脱敏 → 审计追踪**。

---

## 1. 环境搭建

### 1.1 前置条件

- **Python >= 3.11**（依赖 `asyncio.timeout`、`StrEnum`、Pydantic v2 特性）
- 推荐虚拟环境管理工具（如 `venv`、`conda`、`pipenv`）

### 1.2 创建虚拟环境并安装依赖

#### Bash（Linux/macOS）

```bash
# 进入项目根目录
cd c:/projet/homework/ch2/tool_governance

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install pydantic pytest anyio
```

#### PowerShell（Windows）

```powershell
# 进入项目根目录（使用单引号包裹路径，避免 `\` 转义歧义）
cd 'c:\projet\homework\ch2\tool_governance'

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境（PowerShell 必须执行 .ps1 脚本；首次使用可能需要放宽执行策略）
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # 仅首次需要
.\.venv\Scripts\Activate.ps1

# 安装依赖（与 Bash 完全等效）
pip install pydantic pytest anyio
```

> **差异说明**：
> - `cd` 路径分隔符：Bash 接受 `/`，不接受 `\`（会当作转义符）。PowerShell 接受 `\`，但推荐用单引号 `'...'` 或正斜杠 `/`。
> - **激活虚拟环境**：Bash 使用 `source .venv/bin/activate`，PowerShell 使用 `.\.venv\Scripts\Activate.ps1`。这是两者最大的 shell 差异。
> - `pip` / `python` / `pytest` 命令在两种 shell 中完全等效，语法一致。

---

## 2. 项目结构

```
tool_governance/
├── tool_governance_demo.py    # 核心框架 + transfer 工具实现
├── conftest.py                # pytest 全局配置
└── tests/
    └── test_tool_governance.py  # 转账工具的验收测试
```

### 核心文件说明

| 文件 | 作用 |
|---|---|
| [tool_governance_demo.py](file:///c:/projet/homework/ch2/tool_governance/tool_governance_demo.py) | 治理框架主文件，含所有组件定义（PermissionEngine、ToolRuntime、ApprovalStore、AuditSink 等）、三个示例工具（get_order / create_refund / run_shell）及新增的 transfer 转账工具 |
| [conftest.py](file:///c:/projet/homework/ch2/tool_governance/conftest.py) | pytest 全局 fixture，用于统一测试配置 |
| [test_tool_governance.py](file:///c:/projet/homework/ch2/tool_governance/tests/test_tool_governance.py) | transfer 工具的 14 个验收测试用例，覆盖全部治理链路节点、自转账、审批绑定隔离与幂等去重 |

---

## 3. 快速启动

### 3.1 运行离线演示模式

以非交互式方式运行框架自带的演示脚本：

#### Bash

```bash
# 激活虚拟环境后（示例已省略）
python tool_governance_demo.py
```

#### PowerShell

```powershell
# 激活虚拟环境后（示例已省略）
python tool_governance_demo.py
```

此模式会依次调用 `get_order`、`create_refund`（两次，含审批）等工具，并将结果输出到控制台。**两种 Shell 的命令完全一致。**

### 3.2 运行真实模型闭环（可选）

需设置 API Key 后才能使用：

#### Bash

```bash
# 设置环境变量（当前 shell 会话有效）
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"

# 运行 agent 模式（支持自然语言输入）
python tool_governance_demo.py --agent --input "请查询订单 ord_1001 的状态和可退金额"
```

#### PowerShell

```powershell
# 设置环境变量（$env: 是 PowerShell 专属语法）
$env:DEEPSEEK_API_KEY = "sk-xxxxxxxxxxxxxxxx"

# 运行 agent 模式（与 Bash 完全等效）
python tool_governance_demo.py --agent --input "请查询订单 ord_1001 的状态和可退金额"
```

> **差异说明**：
> - **设置环境变量**：Bash 使用 `export KEY=value`，PowerShell 使用 `$env:KEY = "value"`。
> - **引用含空格的路径或字符串**：两种 Shell 都支持双引号 `"`，但 PowerShell 更常用单引号 `'` 作为纯字符串字面量。
> - `--agent` / `--input` 等 Python 参数在两种 Shell 中语法一致。

> **注意**：Agent 模式默认使用 `deepseek-v4-flash` 模型，可通过环境变量 `DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL` 自定义。

---

## 4. 功能模块概览

| 组件 | 职责 | 关键类/函数 |
|---|---|---|
| **参数校验** | Pydantic 严格类型 + 正则 + 范围约束，防注入 | `StrictArgs`、`TransferArgs`、`model_validate()` |
| **权限状态机** | 9 级固定优先级决策引擎（deny → plan → whitelist → RBAC → precheck → approval → bypass → allow → default） | `PermissionEngine.decide()` |
| **一次性审批** | 基于 SHA-256 摘要的参数绑定审批，TTL + used 标记，防重放 | `ApprovalStore.approve()` / `consume()` |
| **超时恢复** | `asyncio.timeout` 自动掐断执行，幂等读可重试，非幂等写报 `TIMEOUT_UNKNOWN` | `ToolRuntime._execute_with_recovery()` |
| **结果脱敏** | 递归遍历返回 dict，对敏感 key、邮箱、账户号进行掩码 | `_redact()` |
| **审计留痕** | 记录决策与执行两阶段日志（trace_id、decision、code、latency_ms） | `AuditSink.append()` |

---

## 5. 测试指南

### 5.1 运行全部 Transfer 测试

#### Bash

```bash
# 在项目根目录或 tool_governance 目录下执行
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

#### PowerShell

```powershell
# 在同一目录下执行（命令与 Bash 完全等效）
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

> **说明**：pytest 命令在两环境下语法完全一致，无差异。唯一注意的是工作目录路径分隔符不同。

### 5.2 常用测试命令格式

| 场景 | Bash 命令 | PowerShell 命令 | 说明 |
|---|---|---|---|
| 运行全部测试 | `python -m pytest tests/test_tool_governance.py -v` | 同左 | 完全等效 |
| 仅运行 transfer 相关测试 | `python -m pytest tests/test_tool_governance.py -v -k "transfer"` | 同左 | 完全等效 |
| 只跑失败或新失败的测试 | `python -m pytest tests/test_tool_governance.py -v --lf` | 同左 | 完全等效 |
| 详细输出（含打印信息） | `python -m pytest tests/test_tool_governance.py -v -s` | 同左 | 完全等效 |
| 生成 HTML 报告 | `pip install pytest-html && python -m pytest tests/test_tool_governance.py -v --html=report.html` | `pip install pytest-html; python -m pytest tests/test_tool_governance.py -v --html=report.html` | Bash 用 `&&` 链式执行（前成功才继续），PowerShell 用 `;` 顺序执行（始终继续） |
| 指定单个测试用例 | `python -m pytest tests/test_tool_governance.py::test_transfer_rejects_unknown_accounts_via_mock -v` | 同左 | 完全等效 |
| 按标签运行（如需加 pytest.mark） | `python -m pytest tests/test_tool_governance.py -v -m "slow"` | 同左 | 完全等效 |
| 查看测试收集信息（不运行） | `python -m pytest tests/test_tool_governance.py -v --collect-only` | 同左 | 完全等效 |

> **差异说明**：
> - **命令链**：Bash 的 `&&` 表示"前命令成功则继续"，`||` 表示"前命令失败则继续"。PowerShell 没有原生的 `&&` 替代语法，改用 `;` 顺序执行（始终逐个执行），或用 `if ($LASTEXITCODE -eq 0) { ... }` 模拟条件链。本项目中 HTML 报告命令的 pip 与 pytest 本就可以分开运行，所以用 `;` 即可。
> - `-k` / `-v` / `-m` / `-s` 等 pytest 选项在两环境中完全一致。

### 5.3 当前测试列表（14 个用例）

| 编号 | 测试函数 | 覆盖节点 | 预期结果 |
|---|---|---|---|
| 1 | `test_transfer_rejects_injected_arguments_and_invalid_amount` | 参数校验（拒绝注入） | `INVALID_ARGUMENT` |
| 2 | `test_transfer_precheck_blocks_over_limit_and_insufficient_balance` | 业务预检（金额拦截 + 余额不足） | `EXCEED_LIMIT` / `INSUFFICIENT_BALANCE` |
| 3 | `test_transfer_rejects_self_transfer` | 业务预检（自转账拦截） | `SELF_TRANSFER` |
| 4 | `test_transfer_is_denied_without_permission_or_whitelist_and_in_plan_mode` | 权限判断（RBAC + 白名单 + plan 模式） | `PERMISSION_DENIED` / `TOOL_NOT_ALLOWED` / `PLAN_MODE_DENIED` |
| 5 | `test_transfer_requires_approval_bound_to_arguments_and_masks_accounts` | 审批绑定 + 成功路径 + 脱敏 + 审计 + 防重放 | `CONFIRM` → `APPROVED` → `OK`，账号已脱敏 |
| 6 | `test_transfer_approval_expires_after_ttl` | 审批时效（TTL 过期） | `CONFIRM` / `APPROVAL_REQUIRED` |
| 7 | `test_transfer_approval_rejected_for_different_user` | 审批跨用户隔离 | `CONFIRM` / `APPROVAL_REQUIRED` |
| 8 | `test_transfer_approval_rejected_for_different_tenant` | 审批跨租户隔离 | `CONFIRM` / `APPROVAL_REQUIRED` |
| 9 | `test_transfer_approval_rejected_for_different_tool` | 审批跨工具隔离 | `CONFIRM` / `APPROVAL_REQUIRED` |
| 10 | `test_transfer_timeout_is_reported_as_unknown_and_leaves_balances_untouched` | 超时处理（非幂等写超时） | `TIMEOUT_UNKNOWN`，余额不变 |
| 11 | `test_transfer_rejects_unknown_accounts_via_mock` | 账户不存在分支（Q4.3 不确定点） | `FROM_ACCOUNT_NOT_FOUND` / `ACCOUNT_NOT_FOUND` |
| 12 | `test_transfer_normal_execution_stays_within_timeout` | 正常不超时误杀（Q5.2 不确定点） | `OK`，耗时 < 2.0s |
| 13 | `test_transfer_idempotency_key_deduplicates_execution` | 幂等去重（防界面双击 / 重复提交） | 首次 `OK`，同键重提 `DUPLICATE_REQUEST`，余额只扣一次 |
| 14 | `test_transfer_canonical_target_includes_from_to_amount` | canonical_target 粒度（规则匹配内容键） | 输出 `from:to:amount` |

### 5.4 测试结果解读

```
tests/test_tool_governance.py::test_transfer_xxx PASSED     ← 测试通过
tests/test_tool_governance.py::test_transfer_yyy FAILED     ← 测试失败
```

**成功标志**（两种 Shell 输出的结果一致）：

```
============================== 14 passed in 2.11s ==============================
```

**常见失败场景**：

| 现象 | 可能原因 | 排查方法 |
|---|---|---|
| `FAILED` 且报错 `NotImplementedError` | TODO 未实现（任务 3/4 占位代码） | 检查 [tool_governance_demo.py](file:///c:/projet/homework/ch2/tool_governance/tool_governance_demo.py) 中的 `TODO(任务 N)` |
| `FAILED` 且报错 `AssertionError` | 实现逻辑有误（如错误码拼写不对） | 查看具体断言失败行号，对比 [q2.md](file:///c:/projet/homework/ch2/docs/q2.md) 要求 |
| `FAILED` 且报错 `ImportError` | 环境缺少依赖 | 重新 `pip install pydantic pytest` |
| `collected 0 items` | 路径错误或 `-k` 过滤太严 | 确认工作目录为 `tool_governance/`，使用 `-k "transfer"` 而非全名 |

### 5.5 编写新的测试用例

#### 步骤 1：确定要覆盖的场景

每个新测试应明确对应治理链路中的一个节点或其边界条件。例如：
- 新增一种金额区间的边界值（`amount = 50_000` 刚好不在拦截区间）
- 新增一个不同的租户 ID 测试跨租户隔离
- 验证审批过期后的行为

#### 步骤 2：使用项目提供的辅助函数

测试文件中已提供以下 helper，复用它们可以减少样板代码：

```python
import asyncio
import functools
from collections.abc import Awaitable, Callable
import tool_governance_demo as demo

# --- 1. async_test 装饰器 ---
# 让异步测试在没有 pytest-asyncio 的情况下也能运行
def async_test(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    @functools.wraps(test)
    def run(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))
    return run

# --- 2. transfer_context() ---
# 创建带 transfer:execute 权限和 transfer 白名单的 ExecutionContext
def transfer_context(**overrides) -> demo.ExecutionContext:
    defaults = {
        "permissions": frozenset({"order:read", "refund:create", "shell:run", "transfer:execute"}),
        "allowed_tools": frozenset({"get_order", "create_refund", "run_shell", "transfer"}),
    }
    return demo.base_context(**{**defaults, **overrides})

# --- 3. approve() ---
# 对同一份参数做一次性审批
def approve(approvals, arguments):
    approvals.approve("approval_transfer", transfer_context(), "transfer", dict(arguments))

# --- 4. balance() ---
# 查询指定账户余额
def balance(account: str, tenant_id: str = "tenant_a") -> float:
    store = getattr(demo, "ACCOUNTS", {})
    return store[(tenant_id, account)]
```

#### 步骤 3：使用 fixture 保证状态隔离

```python
# 项目已通过 @pytest.fixture(autouse=True) 注册了 isolated_accounts，
# 每个测试前后自动快照还原 ACCOUNTS，无需手动清理。
```

#### 步骤 4：编写测试模板

```python
@async_test
async def test_your_new_scenario(self) -> None:
    """简短描述这个测试覆盖什么场景和哪个决策规则。"""

    # --- Arrange：准备 runtime、approvals、audit ---
    runtime, approvals, audit = demo.build_runtime()

    # --- Prepare：构造测试参数 ---
    arguments = {"from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 1_000.0}

    # --- Act：通过 runtime.invoke 触发调用 ---
    result = await runtime.invoke(
        demo.ToolCall("call_test_0001", "transfer", arguments),
        transfer_context(),  # 或使用 approved 上下文
    )

    # --- Assert：断言外部行为（code/action/content/balance/audit） ---
    assert result.ok is True
    assert result.action is demo.DecisionAction.ALLOW
    assert result.code == "OK"
    assert balance("ACC-A-123456") == 99_000.0

    # （可选）审计日志验证
    assert len(audit.records) == 2
    assert audit.records[0].phase == "decision"
    assert audit.records[1].phase == "execution"
```

#### 步骤 5：运行新测试

##### Bash

```bash
# 单独运行某个测试
python -m pytest tests/test_tool_governance.py::test_your_new_scenario -v

# 或全部跑一次确保没破坏其他用例
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

##### PowerShell

```powershell
# 单独运行某个测试（与 Bash 完全等效）
python -m pytest tests/test_tool_governance.py::test_your_new_scenario -v

# 或全部跑一次确保没破坏其他用例（与 Bash 完全等效）
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

> **说明**：pytest 的 `::` 选择特定测试函数在两种 Shell 中语法一致，无差异。

---

## 6. 常见问题排查

### Q1：导入报错 `ModuleNotFoundError: No module named 'pydantic'`

#### Bash

```bash
pip install pydantic
```

#### PowerShell

```powershell
pip install pydantic
```

两条命令完全等效。如果安装了多个 Python 版本，可显式指定解释器：

```powershell
# Windows：指定具体 Python 版本
py -3.14 -m pip install pydantic
```

### Q2：异步测试报错 `TypeError: object function can't be used in 'await' expression`

缺少 `@async_test` 装饰器。异步测试函数必须用该装饰器包装，否则 `pytest` 直接调用无法等待 `await`。

### Q3：`PermissionEngine.decide` 返回的预期行为与实际不符

首先确认你的修改没有触碰 `PermissionEngine.decide`——它是固定框架，**任何改动都会导致不可预测的结果**。如果需要调整权限语义，只能通过添加 `PermissionRule` 或通过 `ExecutionContext` 的 `mode/permissions/allowed_tools` 参数控制。

### Q4：`ACCOUNTS` 数据在测试间串扰

这是正常的——`ACCOUNTS` 是模块级可变状态。若手动测试后余额变了，重启 Python 解释器即可恢复初始状态。测试用例本身已通过 `isolated_accounts` fixture 自动隔离。

---

## 7. 扩展建议

- **新增工具**：参照 `transfer` 的实现模式，继承 `StrictArgs` 定义参数，编写 `precheck` + `handler`，在 `build_tools()` 注册 `ToolDefinition`。
- **自定义脱敏规则**：在 `_redact()` 追加新的正则替换逻辑。
- **持久化审计日志**：替换 `AuditSink` 的内存列表实现为数据库 sink，接口不变。
- **并发安全**：引入 `asyncio.Lock` 保护 `ACCOUNTS` 读写，防止超卖。

---

*最后更新：2026-10-08 · 关联作业：第二章实战作业（docs/q2.md）*
