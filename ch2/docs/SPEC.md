# 转账工具（Transfer）规格计划书

> 版本：v1.0  
> 状态：Ready for Agent  
> 关联作业：`第二章实战作业——为治理框架增加"转账"工具`（`docs/q2.md`）  
> 目标文件：`tool_governance/tool_governance_demo.py`

---

## 1. 问题陈述（Problem Statement）

现有治理框架 `tool_governance_demo.py` 已经内置了一条完整的工具治理链路（Pydantic 参数校验 → 权限状态机 → 一次性审批 → 超时恢复 → 结果脱敏 → 审计追踪），但目前仅注册了 `get_order`、`create_refund`、`run_shell` 三类工具，缺少一个**涉及资金变动、同时触发整条治理链路的代表性工具**。本作业的目标是新增一个名为 `transfer` 的"跨账户转账"工具，用它亲手跑通上述链路，作为第二章"工具治理"的核心实战案例。

用户视角要解决的问题：**模型发起的转账必须经过参数校验、业务预检、RBAC/白名单判断、人工审批、超时兜底、结果脱敏与审计留痕，任何一环缺失都不能真正扣款。**

---

## 2. 解决方案（Solution）

在现有框架上，以**最小侵入**方式接入 `transfer` 工具，复用框架已有的 `ToolRuntime.invoke`、`PermissionEngine.decide`、`ApprovalStore`、`AuditSink` 与 `_redact` 能力。新增内容仅限以下六处（对应 `q2.md` 的六个任务）：

1. 新增模拟账户数据 `ACCOUNTS`（模块级可变字典）。
2. 定义 `TransferArgs` 参数模型（继承 `StrictArgs`，`extra="forbid"`）。
3. 实现 `transfer_precheck` 业务预检（金额区间 + 转出账户存在性 + 余额充足）。
4. 实现 `transfer_handler` 转账处理（超时模拟 + 转入账户校验 + 余额转移 + 返回结果）。
5. 在 `build_tools()` 末尾注册 `transfer` 的 `ToolDefinition`。
6. 在 `_redact()` 中追加账户号脱敏。

新增代码不修改 `PermissionEngine.decide` 的任何一行，不改动测试文件，不破坏框架既有的优先级语义。

---

## 3. 系统架构概述

### 3.1 分层结构

```
┌─────────────────────────────────────────────────────────────┐
│  调用方：模型 Agent / CLI / 测试（统一走 ToolRuntime.invoke）│
└────────────────────────────┬────────────────────────────────┘
                             │ ToolCall(name, arguments)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  ToolRuntime.invoke（唯一执行入口）                          │
│   1. 工具查找 → 2. Pydantic 校验 → 3. 权限/预检决策          │
│   4. 执行（含超时/重试恢复）→ 5. 结果脱敏 → 6. 审计写入       │
└───────┬───────────────────────────────┬─────────────────────┘
        │                               │
        ▼                               ▼
PermissionEngine.decide          ToolDefinition(handler/precheck/policy)
（9 级优先级状态机）              ├── transfer_precheck（业务预检）
                                  └── transfer_handler（副作用）
        │
        ▼
ApprovalStore（一次性、参数绑定审批）   AuditSink（审计记录）
```

### 3.2 关键组件职责

| 组件 | 职责 | 依赖 |
|---|---|---|
| `StrictArgs` | 参数基类，`extra="forbid"` + `strict=True`，防注入 | Pydantic BaseModel |
| `TransferArgs` | 转账参数 Schema（正则 + 额度约束） | `StrictArgs` |
| `ToolPolicy` | 单工具治理策略（效果/风险/权限/审批/超时/重试/幂等） | 数据类 |
| `ToolDefinition` | 工具元数据 + handler + precheck + canonical_target | `ToolPolicy` |
| `PermissionEngine` | 9 级固定优先级权限状态机，返回 allow/deny/confirm | 规则 + `ApprovalStore` |
| `ToolRuntime` | 校验→决策→执行→脱敏→审计的编排器 | 以上全部 |
| `ApprovalStore` | 一次性参数绑定审批（SHA-256 摘要 + TTL + used 标记） | 无 |
| `AuditSink` | 决策/执行两阶段审计留痕 | `AuditRecord` |
| `_redact` | 递归脱敏（敏感 key + 邮箱 + 账户号） | 无 |

---

## 4. 核心功能模块说明

### 4.1 账户数据模块（任务 1）

- 位置：`ORDERS` 之后新增 `ACCOUNTS`。
- 类型：`dict[tuple[str, str], float]`，Key 为 `(tenant_id, account_id)`，Value 为余额。
- 数据必须为**可变字典**（测试通过快照还原实现用例隔离）。

预设账户（来自 `q2.md 任务 1`）：

| tenant_id | account_id | 余额 |
|---|---|---|
| `tenant_a` | `ACC-A-123456` | `100_000.0` |
| `tenant_a` | `ACC-A-654321` | `5_000.0` |
| `tenant_a` | `ACC-A-888888` | `20_000.0` |
| `tenant_b` | `ACC-B-111111` | `50_000.0` |

### 4.2 参数模型模块（任务 2）

`TransferArgs(StrictArgs)`，三个字段：

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `from_account` | `str` | `^ACC-[A-Z]-[0-9]{6}$` | 转出账户（租户编码 + 6 位账号） |
| `to_account` | `str` | `^ACC-[A-Z]-[0-9]{6}$` | 转入账户，同上 |
| `amount` | `float` | `gt=0, le=100_000` | 转账金额，必须 > 0 且 ≤ 10 万 |

> 约束说明：正则中的 `[A-Z]` 是**账户 ID 内的租户编码位**（账户 id 格式），不是 `tenant_id`。`amount` 严格 `gt=0`——0 元转账不允许执行。

### 4.3 业务预检模块（任务 3）

`transfer_precheck(raw_arguments, context)`：**只判断、不改余额**，按以下顺序抛 `PolicyDenied`：

| 顺序 | 条件 | 错误码 | 说明 |
|---|---|---|---|
| 1 | `50_000 < amount <= 80_000` | `EXCEED_LIMIT` | 教学专用拦截区间 |
| 2 | `(tenant_id, from_account)` 不在 `ACCOUNTS` | `FROM_ACCOUNT_NOT_FOUND` | 转出账户不存在 |
| 3 | `ACCOUNTS[from_key] < amount` | `INSUFFICIENT_BALANCE` | 余额不足 |

**关键边界**：`amount > 80_000` 必须放行（不命中区间 1），通过余额检查与审批后进入任务 4 的超时分支。

### 4.4 转账处理模块（任务 4）

`transfer_handler(tool_call_id, raw_arguments, context)` 执行顺序：

1. `isinstance(arguments, TransferArgs)` 类型收窄守卫。
2. **超时模拟**：若 `amount > 80_000`，先 `await asyncio.sleep(3.0)` —— **必须发生在任何余额修改之前**，让框架的 `asyncio.timeout` 先掐断执行。
3. **转入账户校验**：`(tenant_id, to_account)` 不在 `ACCOUNTS` → `raise PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")`。
4. **余额转移**：`ACCOUNTS[from_key] -= amount`；`ACCOUNTS[to_key] += amount`。
5. **返回结果**：`txn_id`（`tool_call_id` 后 6 位）、`from`、`to`、`amount`、`status="accepted"`。

### 4.5 工具注册模块（任务 5）

在 `build_tools()` 的 return 列表末尾追加 `transfer` 的 `ToolDefinition`：

| 配置项 | 值 |
|---|---|
| `name` | `"transfer"` |
| `description` | `"为当前租户创建跨账户转账"` |
| `parameters_model` | `TransferArgs` |
| `effect` | `Effect.WRITE` |
| `risk` | `Risk.HIGH` |
| `permission` | `"transfer:execute"` |
| `requires_approval` | `True` |
| `timeout_seconds` | `2.0`（**< 3.0**，保证超时演示生效） |
| `max_retries` | `0`（非幂等写不重试） |
| `idempotent` | `False` |
| `handler` | `transfer_handler` |
| `precheck` | `transfer_precheck` |
| `canonical_target` | `lambda args: f"{args.from_account}:{args.to_account}:{args.amount}:{int(time.time() // 300)}"`（from + to + amount + 300s 时间窗口） |

### 4.6 结果脱敏模块（任务 6）

在 `_redact()` 邮箱脱敏之后追加一行账号脱敏：

```python
value = re.sub(r"(ACC-\w-)\d{2}(\d{4})", r"\1****\2", value)
```

- 采用**分组引用**方式（方案 B），保留账户前缀与末 4 位，中间 2 位替换为 `****`。
- 匹配结果：`ACC-A-123456 → ACC-A-****3456`。

---

## 5. 接口定义（Interface Definitions）

### 5.1 参数模型 `TransferArgs`

```json
{
  "type": "object",
  "properties": {
    "from_account": {"type": "string", "pattern": "^ACC-[A-Z]-[0-9]{6}$"},
    "to_account":   {"type": "string", "pattern": "^ACC-[A-Z]-[0-9]{6}$"},
    "amount":       {"type": "number", "exclusiveMinimum": 0, "maximum": 100000}
  },
  "required": ["from_account", "to_account", "amount"],
  "additionalProperties": false
}
```

### 5.2 函数签名

```python
async def transfer_precheck(raw_arguments: ArgsModel, context: ExecutionContext) -> None
async def transfer_handler(tool_call_id: str, raw_arguments: ArgsModel, context: ExecutionContext) -> Mapping[str, Any]
```

### 5.3 返回结果结构（handler 原始输出，脱敏前）

```python
{
    "txn_id": "000003",          # tool_call_id 后 6 位
    "from":   "ACC-A-123456",
    "to":     "ACC-A-654321",
    "amount": 1200.0,
    "status": "accepted"
}
```

### 5.4 错误码约定

| 错误码 | 来源阶段 | 含义 |
|---|---|---|
| `INVALID_ARGUMENT` | Pydantic 校验 | 参数格式/额度非法或注入额外字段 |
| `EXCEED_LIMIT` | 业务预检 | 金额落入教学拦截区间 |
| `FROM_ACCOUNT_NOT_FOUND` | 业务预检 | 转出账户不存在 |
| `INSUFFICIENT_BALANCE` | 业务预检 | 转出账户余额不足 |
| `ACCOUNT_NOT_FOUND` | 执行 | 转入账户不存在 |
| `PERMISSION_DENIED` | 权限 | 缺少 `transfer:execute` 权限 |
| `TOOL_NOT_ALLOWED` | 白名单 | `transfer` 不在执行白名单 |
| `PLAN_MODE_DENIED` | 模式 | plan 模式禁止写操作 |
| `APPROVAL_REQUIRED` | 审批 | 高风险写需人工确认（CONFIRM） |
| `TIMEOUT_UNKNOWN` | 执行 | 非幂等写超时，副作用状态未知 |

---

## 6. 数据流设计（Data Flow）

### 6.1 权限决策优先级（`PermissionEngine.decide`，固定不可改）

```
1. deny 规则（硬拒绝） → DENY
2. plan 模式 + 非只读 → DENY
3. 执行白名单检查 → DENY
4. RBAC 权限检查 → DENY
5. 业务预检失败 → DENY
6. 高风险写 + 无有效审批 → CONFIRM（有审批则 ALLOW）
7. bypass 模式 → ALLOW
8. allow 规则 → ALLOW
9. 默认放行 → ALLOW
```

### 6.2 `transfer` 一次完整的成功调用流程

```
ToolRuntime.invoke("transfer", args)
  │
  ├─ Pydantic 校验 TransferArgs ── 失败 → INVALID_ARGUMENT（DENY）
  │
  ├─ PermissionEngine.decide
  │    ├─ RBAC（transfer:execute）          → 失败 PERMISSION_DENIED
  │    ├─ 白名单（transfer）                → 失败 TOOL_NOT_ALLOWED
  │    ├─ transfer_precheck                 → 失败 EXCEED_LIMIT / FROM_ACCOUNT_NOT_FOUND / INSUFFICIENT_BALANCE
  │    └─ 审批 consume（参数绑定摘要）      → 失败 → CONFIRM（APPROVAL_REQUIRED）
  │
  ├─ 审计写入（phase=decision）
  │
  ├─ _execute_with_recovery（asyncio.timeout=2.0）
  │    └─ transfer_handler
  │         ├─ amount > 80000 → sleep(3.0) → TimeoutError
  │         ├─ to_account 不存在 → ACCOUNT_NOT_FOUND
  │         └─ 扣款/加款 → 返回结果
  │
  ├─ 脱敏 _redact（账户号 → ACC-A-****3456）
  │
  └─ 审计写入（phase=execution）→ 返回 ToolResult(ok=True)
```

### 6.3 审批参数绑定机制

审批摘要由 `_approval_digest` 基于 `canonical_target` 的稳定序列化 + `tool_name` 计算 SHA-256。`ApprovalStore.consume` 校验：

- `user_id`、`tenant_id`、`tool_name` 完全一致；
- `digest` 逐字节匹配（即 `from`、`to`、`amount` 任一变化都会导致摘要不同）；
- 未过期（TTL 300s）且未使用（`used=False`）。

**一次性语义**：`consume` 通过后立即 `used=True`，同一审批 ID 无法重放。

---

## 7. 性能指标（Performance Metrics）

| 指标 | 目标值 | 说明 |
|---|---|---|
| 单次正常转账端到端延迟 | < 100ms（不含 sleep 分支） | 纯内存操作 + 一次 SHA-256 |
| 超时分支 | 触发点早于 3.0s | `timeout_seconds=2.0` 先于 `sleep(3.0)` 生效 |
| 非幂等写重试次数 | 0 | `max_retries=0`，避免重复扣款 |
| 审批摘要计算 | O(1) 级别 | 固定字段稳定序列化 |
| 审计写入 | 每次调用 2 条（decision + execution） | 决策失败时 1 条 |

> 教学演示环境为单线程顺序执行，无并发竞争要求。是否引入 `asyncio.Lock` 属于并发安全章节的后续课题，本 spec **不包含**并发优化。

---

## 8. 安全要求（Security Requirements）

1. **参数防注入**：`TransferArgs` 继承 `StrictArgs`，`extra="forbid"` 为最后屏障，**不可删除**。任何 `approved`、`user_id` 等额外字段注入 → `INVALID_ARGUMENT`。
2. **权限最小化**：`transfer:execute` 必须显式授予，且 `transfer` 必须出现在执行白名单。
3. **审批绑定**：高风险写操作强制一次性、参数绑定审批，防重放、防篡改。
4. **结果脱敏**：账户号、邮箱、敏感 key（`token/secret/password/authorization`）一律脱敏后才返回给模型。
5. **超时兜底**：非幂等写超时返回 `TIMEOUT_UNKNOWN`，不盲目重试，避免未知副作用。
6. **审计留痕**：决策与执行两个阶段均记录 `trace_id`、`tool_call_id`、`decision`、`code`、`latency_ms`，可追溯。
7. **权限引擎不可变**：`PermissionEngine.decide` 的 9 级优先级顺序是固定框架，**不得改动任何一行**。

---

## 9. 部署说明（Deployment）

### 9.1 前置条件

- Python 3.11+（依赖 `asyncio.timeout`、`StrEnum`、Pydantic v2）。
- 已安装 `pydantic`、`pytest`。

### 9.2 验收命令

```bash
cd c:\projet\homework\ch2
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

**预期**：5 个测试全部 PASSED。

### 9.3 验收标准（非测试补充项）

- 打印审计日志时账号已脱敏为 `ACC-A-****3456`。
- 审批流程执行前返回 `CONFIRM`（`APPROVAL_REQUIRED`）状态。

### 9.4 变更范围

仅修改 `tool_governance/tool_governance_demo.py` 的六个 TODO 占位处；**不修改** `PermissionEngine.decide`、不修改 `tests/` 文件、不新建运行时代码文件。

---

## 10. 测试决策（Testing Decisions）

### 10.1 测试原则

- 只测**外部行为**（返回的 `ToolResult.code / action / content`、余额变化、审计顺序），不测实现细节。
- 所有调用**必须走 `runtime.invoke()`**，不得直接调用 `transfer_handler`。
- 既有 seam：`demo.build_runtime()` → `runtime.invoke()`。

### 10.2 必测用例（复用 `tests/test_tool_governance.py`）

1. **参数校验**：注入额外字段、非法账号格式、0 / 超额度金额 → `INVALID_ARGUMENT`。
2. **业务预检**：`60_000` → `EXCEED_LIMIT`；小账户转 `30_000` → `INSUFFICIENT_BALANCE`；余额不变。
3. **权限判断**：缺权限 / 不在白名单 / plan 模式 → 对应拒绝码。
4. **审批 + 成功 + 脱敏 + 审计**：先 CONFIRM；审批后成功；脱敏断言 `ACC-A-****3456` / `****4321`；审计顺序 `[APPROVAL_REQUIRED, APPROVAL_REQUIRED, APPROVED, OK]`；审批不可重放。
5. **超时**：`90_000` → `TIMEOUT_UNKNOWN`，耗时 < 3.0s，余额不变。
6. **账户不存在（mock `ACC-A-999999`）**：转出不存在 → `FROM_ACCOUNT_NOT_FOUND`；转入不存在 → `ACCOUNT_NOT_FOUND`（验证 `PolicyDenied` 被 `invoke` 捕获映射为 DENY，对应不确定点 Q4.3）。
7. **正常执行不超时误杀**：普通金额转账耗时 < `timeout_seconds=2.0`，返回 `OK`，余额变更正确（对应不确定点 Q5.2）。
8. **canonical_target 时间窗口粒度**：断言 `canonical_target` 输出为 `from:to:amount:窗口`（300s 桶，对应不确定点 Q5.3）。

### 10.3 状态隔离

`ACCOUNTS` 为模块级可变状态，通过 pytest fixture `isolated_accounts` 做快照还原，保证用例互相隔离、可重复运行。

### 10.4 测试决策机制（Test Decision Mechanism）

**决策总则**：测试仅通过唯一入口 `runtime.invoke()` 观察**外部行为**（`ToolResult.code/action/content`、余额变化、审计顺序），与内部实现解耦；新增或调整用例时遵循以下决策规则，确保治理链路每个节点都有明确的正反例覆盖。

**最小 mock 数据面（沿用现有 fixture 与 helper）**：

- **mock 账户**：`ACCOUNTS` 内置有效账户 `FROM_ACCOUNT` / `TO_ACCOUNT` / `SMALL_ACCOUNT`；另定义满足参数正则但**不在 `ACCOUNTS`** 的 mock 账号 `ACC-A-999999`，用于显式区分"参数格式合法但业务账户不存在"的分支。
- **mock 审批**：`approve()` 基于同一份参数的完整稳定序列化生成一次性审批，`consume` 校验 `user_id/tenant_id/tool_name/digest/TTL/used`，参数逐字节绑定。
- **mock 上下文**：`transfer_context()` 注入 `transfer:execute` 权限与 `transfer` 白名单；通过覆盖 `permissions/allowed_tools/mode/approval_id` 触发不同决策分支。

**决策规则**：

1. **节点全覆盖**：参数校验、业务预检、权限、审批、执行、脱敏、审计七个节点，每节点至少一个成功路径 + 一个反例。
2. **错误码分支覆盖**：每个错误码（`INVALID_ARGUMENT / EXCEED_LIMIT / FROM_ACCOUNT_NOT_FOUND / INSUFFICIENT_BALANCE / ACCOUNT_NOT_FOUND / PERMISSION_DENIED / TOOL_NOT_ALLOWED / PLAN_MODE_DENIED / APPROVAL_REQUIRED / TIMEOUT_UNKNOWN`）都必须有可命中的用例。
3. **不确定点显性化（用 mock 数据固定）**：将前序设计审查中标记"待测试验证"的不确定点全部落实为可重复测试——`ACCOUNT_NOT_FOUND` 是否被 `invoke` 捕获（Q4.3）、正常转账是否在 `timeout_seconds=2.0` 内完成不被误杀（Q5.2）、`canonical_target` 是否含 300s 时间窗口桶（Q5.3）。
4. **禁止直接调用 handler**：所有子路径（含业务错误）都必须经由 `runtime.invoke()` 触发，不绕过框架。
5. **状态隔离**：`isolated_accounts` fixture 在用例前后对 `ACCOUNTS` 快照还原；`approve`/`reset_side_effects` 保证审批与副作用互相独立、可重复。

---

## 11. 用户故事（User Stories）

1. 作为模型 Agent，我想要一个 `transfer` 工具，以便在获得批准后执行跨账户转账。
2. 作为系统，我要在参数校验阶段拒绝格式非法的账户号与非法的转账金额，以便阻止注入与脏数据。
3. 作为系统，我要在业务预检阶段拦截教学区间的金额并检查余额，以便在产生副作用前拒绝非法转账。
4. 作为系统，我要对高风险转账强制一次性参数绑定审批，以便防止重放与篡改。
5. 作为系统，我要对非幂等写超时返回 `TIMEOUT_UNKNOWN`，以便不盲目重试造成重复扣款。
6. 作为系统，我要在结果返回前对账户号脱敏，以便避免敏感数据泄漏给模型。
7. 作为系统，我要记录决策与执行两阶段的审计日志，以便事后追溯每一笔转账。
8. 作为运维，我要能通过一条 pytest 命令验证整条治理链路，以便快速验收。

---

## 12. 范围外（Out of Scope）

- 并发/分布式事务与锁机制（超卖防护）。
- 真实支付渠道、账务系统对接。
- 转账的幂等键 / 对账 / 冲正机制。
- 审批的持久化存储（当前为内存 `ApprovalStore`）。
- 更宽泛的租户/账户动态接入（当前仅 A/B 租户编码，正则固定 `[A-Z]`）。
- `PermissionEngine.decide` 优先级语义的任何调整。

---

## 13. 扩展性考虑（Extensibility Considerations）

1. **账户编码扩展**：当前正则 `[A-Z]` 仅覆盖单字母租户编码；若需 `tenant_c` 等多编码或更复杂编码，可将正则抽为可配置常量，避免散落硬编码。
2. **审批去重**：`canonical_target` 采用 from + to + amount + 300s 时间窗口的粒度，`from:to:amount:窗口` 内相同的转账视为同一目标，叠加时间窗维度以抑制窗口内的重复提交；如仍需更强幂等，可在未来叠加 `txn_id` 维度。
3. **并发安全**：若未来引入真实并发，可在 handler 内以 `asyncio.Lock` 或账户级锁保护余额读写；本作业刻意省略以聚焦治理链路。
4. **审计持久化**：`AuditSink` 目前为内存列表，可替换为日志/数据库 sink 而不改调用方。
5. **审批存储**：`ApprovalStore` 可替换为 Redis/DB 实现，接口 `approve/consume` 保持不变。
6. **脱敏规则**：`_redact` 的账户脱敏正则与邮箱脱敏并列，未来可配置化为规则表。

---

## 14. 进一步说明（Further Notes）

- 本 spec 描述的是**设计决策**，不含具体文件行号与完整代码片段，以避免与实现脱节。关键决策（参数 Schema、错误码枚举、审批绑定字段、超时值）已在上文以结构化方式固化。
- 关键设计来自前序 `grill-me` 压力测试与用户确认：转出账户不存在使用独立错误码 `FROM_ACCOUNT_NOT_FOUND`；`amount` 严格 `gt=0`；脱敏采用分组引用方案 B；`timeout_seconds=2.0`（< 3.0s）。
- 实现落地后，以第 9 节的验收命令为准进行验证。