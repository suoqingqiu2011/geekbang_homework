"""第二章作业验收：转账工具（transfer）的 5 个链路测试。

约法三章（作业"不能改动的地方"）：
- 所有调用都经过 `ToolRuntime.invoke`，不直接调 handler。
- 不改 `PermissionEngine.decide` 的优先级顺序，测试只观察它的输出。

未完成对应任务时，测试给的是"哪一步没做"的失败信息，而不是导入错误：
缺少 `ACCOUNTS` 会命中 `accounts()` 的断言，没注册 transfer 工具会拿到 `TOOL_NOT_FOUND`。
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

import pytest

import tool_governance_demo as demo

TRANSFER = "transfer"
APPROVAL_ID = "approval_transfer"
FROM_ACCOUNT = "ACC-A-123456"  # tenant_a，余额 100000.0
TO_ACCOUNT = "ACC-A-654321"  # tenant_a，余额 5000.0
SMALL_ACCOUNT = "ACC-A-888888"  # tenant_a，余额 20000.0
SLEEP_SECONDS = 3.0  # transfer_handler 里刻意制造的慢调用


def async_test(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """不依赖 pytest-asyncio，让 async 测试在任何 pytest 环境下都能跑。"""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


def transfer_context(**overrides: Any) -> demo.ExecutionContext:
    """带 transfer:execute 权限和 transfer 执行白名单的上下文。"""

    defaults: dict[str, Any] = {
        "permissions": frozenset({"order:read", "refund:create", "shell:run", "transfer:execute"}),
        "allowed_tools": frozenset({"get_order", "create_refund", "run_shell", "transfer"}),
    }
    return demo.base_context(**{**defaults, **overrides})


def accounts() -> dict[tuple[str, str], float]:
    store = getattr(demo, "ACCOUNTS", None)
    assert isinstance(store, dict), "任务 1 未完成：tool_governance_demo.ACCOUNTS 不存在"
    return store


def balance(account: str, tenant_id: str = "tenant_a") -> float:
    store = accounts()
    assert (tenant_id, account) in store, f"任务 1 未完成：ACCOUNTS 缺少 {(tenant_id, account)}"
    return store[(tenant_id, account)]


def approve(approvals: demo.ApprovalStore, arguments: Mapping[str, Any]) -> None:
    """对同一份参数做一次性审批（摘要绑定，值必须与调用时逐字节一致）。"""

    approvals.approve(APPROVAL_ID, transfer_context(), TRANSFER, dict(arguments))


async def transfer(
    runtime: demo.ToolRuntime,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    **context_overrides: Any,
) -> demo.ToolResult:
    return await runtime.invoke(
        demo.ToolCall(tool_call_id, TRANSFER, arguments),
        transfer_context(**context_overrides),
    )


@pytest.fixture(autouse=True)
def isolated_accounts() -> Iterator[None]:
    """ACCOUNTS 是模块级可变状态，逐个用例还原，保证互相隔离且可重复运行。"""

    store = getattr(demo, "ACCOUNTS", None)
    snapshot = dict(store) if isinstance(store, dict) else None
    demo.reset_side_effects()
    yield
    if snapshot is not None:
        store.clear()
        store.update(snapshot)


@async_test
async def test_transfer_rejects_injected_arguments_and_invalid_amount() -> None:
    """参数校验：extra="forbid" 挡住模型注入，Field 约束挡住非法账号与额度。"""

    runtime, _approvals, _audit = demo.build_runtime()
    valid = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 100.0}

    injected = await transfer(runtime, "call_tr_inject", {**valid, "approved": True, "user_id": "u_admin"})
    assert injected.ok is False
    assert injected.code == "INVALID_ARGUMENT"

    malformed = await transfer(runtime, "call_tr_format", {**valid, "from_account": "ACC-123456"})
    assert malformed.code == "INVALID_ARGUMENT"

    zero = await transfer(runtime, "call_tr_zero", {**valid, "amount": 0.0})
    assert zero.code == "INVALID_ARGUMENT"

    over_schema = await transfer(runtime, "call_tr_schema", {**valid, "amount": 100_000.1})
    assert over_schema.code == "INVALID_ARGUMENT"

    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_precheck_blocks_over_limit_and_insufficient_balance() -> None:
    """业务预检：教学金额区间拦截与余额不足都在 handler 之前拒绝。"""

    runtime, approvals, _audit = demo.build_runtime()

    over_limit = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 60_000.0}
    approve(approvals, over_limit)
    rejected = await transfer(runtime, "call_tr_limit", over_limit, approval_id=APPROVAL_ID)
    assert rejected.ok is False
    assert rejected.action is demo.DecisionAction.DENY
    assert rejected.code == "EXCEED_LIMIT"

    short = {"from_account": SMALL_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 30_000.0}
    approve(approvals, short)
    rejected = await transfer(runtime, "call_tr_balance", short, approval_id=APPROVAL_ID)
    assert rejected.action is demo.DecisionAction.DENY
    assert rejected.code == "INSUFFICIENT_BALANCE"

    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(SMALL_ACCOUNT) == 20_000.0
    assert balance(TO_ACCOUNT) == 5_000.0


@async_test
async def test_transfer_rejects_self_transfer() -> None:
    """业务预检：禁止自转账。转出与转入账户相同时，即使持有效审批也在预检阶段被拒绝，余额不变。"""

    runtime, approvals, _audit = demo.build_runtime()
    self_arguments = {"from_account": FROM_ACCOUNT, "to_account": FROM_ACCOUNT, "amount": 1_000.0}
    approve(approvals, self_arguments)

    result = await transfer(runtime, "call_tr_self", self_arguments, approval_id=APPROVAL_ID)
    assert result.ok is False
    assert result.action is demo.DecisionAction.DENY
    assert result.code == "SELF_TRANSFER"

    # 自转账必须发生在任何扣款之前，转出账户余额不得变化。
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_is_denied_without_permission_or_whitelist_and_in_plan_mode() -> None:
    """权限判断：带上审批也越不过 RBAC、执行白名单和 plan 只读契约。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    approve(approvals, arguments)
    approved: dict[str, Any] = {"approval_id": APPROVAL_ID}

    no_permission = await transfer(
        runtime,
        "call_tr_rbac",
        arguments,
        permissions=frozenset({"order:read", "refund:create", "shell:run"}),
        **approved,
    )
    assert no_permission.action is demo.DecisionAction.DENY
    assert no_permission.code == "PERMISSION_DENIED"

    not_whitelisted = await transfer(
        runtime,
        "call_tr_whitelist",
        arguments,
        allowed_tools=frozenset({"get_order", "create_refund", "run_shell"}),
        **approved,
    )
    assert not_whitelisted.code == "TOOL_NOT_ALLOWED"

    plan_mode = await transfer(
        runtime,
        "call_tr_plan",
        arguments,
        mode=demo.PermissionMode.PLAN,
        **approved,
    )
    assert plan_mode.code == "PLAN_MODE_DENIED"

    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_requires_approval_bound_to_arguments_and_masks_accounts() -> None:
    """人工审批 + 成功路径 + 结果脱敏 + 审计追踪。"""

    runtime, approvals, audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_200.0}

    pending = await transfer(runtime, "call_tr_000001", arguments)
    assert pending.ok is False
    assert pending.action is demo.DecisionAction.CONFIRM
    assert pending.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0

    approve(approvals, arguments)
    tampered = await transfer(
        runtime, "call_tr_000002", {**arguments, "amount": 9_000.0}, approval_id=APPROVAL_ID
    )
    assert tampered.action is demo.DecisionAction.CONFIRM
    assert tampered.code == "APPROVAL_REQUIRED"

    executed = await transfer(runtime, "call_tr_000003", arguments, approval_id=APPROVAL_ID)
    assert executed.ok is True
    assert executed.action is demo.DecisionAction.ALLOW
    assert executed.code == "OK"
    assert executed.content["txn_id"] == "000003"
    assert executed.content["amount"] == 1_200.0
    assert executed.content["status"] == "accepted"
    assert executed.content["from"] == "ACC-A-****3456"
    assert executed.content["to"] == "ACC-A-****4321"
    assert balance(FROM_ACCOUNT) == 98_800.0
    assert balance(TO_ACCOUNT) == 6_200.0

    assert [(record.phase, record.code) for record in audit.records] == [
        ("decision", "APPROVAL_REQUIRED"),
        ("decision", "APPROVAL_REQUIRED"),
        ("decision", "APPROVED"),
        ("execution", "OK"),
    ]

    replay = await transfer(runtime, "call_tr_000004", arguments, approval_id=APPROVAL_ID)
    assert replay.action is demo.DecisionAction.CONFIRM


@async_test
async def test_transfer_approval_expires_after_ttl() -> None:
    """人工审批：审批有过期时间（TTL 300s），过期后即使参数一致也无法放行，回到 CONFIRM。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}

    # 用可控时钟替换 demo 模块里的 time.time，让审批签发后越过 TTL 再消费。
    fake_time = {"now": 1_000_000.0}
    original_time = demo.time.time
    demo.time.time = lambda: fake_time["now"]
    try:
        approve(approvals, arguments)  # expires_at = now + 300
        fake_time["now"] += 301  # 越过 300s TTL
        expired = await transfer(runtime, "call_tr_expired", arguments, approval_id=APPROVAL_ID)
    finally:
        demo.time.time = original_time

    assert expired.ok is False
    assert expired.action is demo.DecisionAction.CONFIRM
    assert expired.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_approval_rejected_for_different_user() -> None:
    """人工审批：审批绑定到颁发它的 user_id，其它用户不能消费同一张审批。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    approve(approvals, arguments)  # 绑定 user_id="u_100"

    other_user = await transfer(
        runtime, "call_tr_other_user", arguments, approval_id=APPROVAL_ID, user_id="u_admin"
    )
    assert other_user.action is demo.DecisionAction.CONFIRM
    assert other_user.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_approval_rejected_for_different_tenant() -> None:
    """人工审批：审批绑定到颁发它的 tenant_id，其它租户不能消费同一张审批。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    approve(approvals, arguments)  # 绑定 tenant_id="tenant_a"

    # 预检会先按调用租户去查账户，目标租户 tenant_z 名下必须有同名账户才能走到审批步骤，
    # 由此单独验证"审批的租户绑定"（fixture 会在用例结束后还原 ACCOUNTS）。
    demo.ACCOUNTS[("tenant_z", FROM_ACCOUNT)] = 100_000.0
    demo.ACCOUNTS[("tenant_z", TO_ACCOUNT)] = 5_000.0
    other_tenant = await transfer(
        runtime, "call_tr_other_tenant", arguments, approval_id=APPROVAL_ID, tenant_id="tenant_z"
    )
    assert other_tenant.action is demo.DecisionAction.CONFIRM
    assert other_tenant.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_approval_rejected_for_different_tool() -> None:
    """人工审批：审批绑定到工具名，用别的工具签发的审批无法放行 transfer。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    # 在同一 approval_id 下为「其它工具」create_refund 签发审批，再用它调 transfer。
    approvals.approve(
        APPROVAL_ID,
        transfer_context(),
        "create_refund",
        {"order_id": "ord_1001", "amount": 50.0, "reason": "cross-tool demo"},
    )

    cross_tool = await transfer(runtime, "call_tr_cross_tool", arguments, approval_id=APPROVAL_ID)
    assert cross_tool.action is demo.DecisionAction.CONFIRM
    assert cross_tool.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_timeout_is_reported_as_unknown_and_leaves_balances_untouched() -> None:
    """超时处理：非幂等写超时是 TIMEOUT_UNKNOWN，且超时点必须早于扣款。"""

    runtime, approvals, audit = demo.build_runtime()
    # 超过 80000 不命中教学拦截区间，通过余额检查与审批后触发 sleep(3.0)。
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 90_000.0}
    approve(approvals, arguments)

    started = time.perf_counter()
    result = await transfer(runtime, "call_tr_timeout", arguments, approval_id=APPROVAL_ID)
    elapsed = time.perf_counter() - started

    assert result.ok is False
    assert result.code == "TIMEOUT_UNKNOWN"
    assert elapsed < SLEEP_SECONDS
    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(TO_ACCOUNT) == 5_000.0
    assert audit.records[-1].phase == "execution"
    assert audit.records[-1].code == "TIMEOUT_UNKNOWN"


@async_test
async def test_transfer_rejects_unknown_accounts_via_mock() -> None:
    """账户不存在分支：验证 PolicyDenied 被 invoke 捕获并映射为 DENY（不确定点 Q4.3）。

    用一个满足参数正则、但不在 ACCOUNTS 中的 mock 账号 ACC-A-999999：
    - 作为转出账户 → 业务预检阶段拦截 FROM_ACCOUNT_NOT_FOUND（无需审批）。
    - 作为转入账户 → 通过预检与审批后，在 handler 内以 ACCOUNT_NOT_FOUND 拒绝。
    """

    runtime, approvals, audit = demo.build_runtime()
    missing = "ACC-A-999999"  # 正则合法、但业务上不存在的 mock 账户

    missing_from = await transfer(
        runtime,
        "call_tr_missing_from",
        {"from_account": missing, "to_account": TO_ACCOUNT, "amount": 100.0},
    )
    assert missing_from.ok is False
    assert missing_from.action is demo.DecisionAction.DENY
    assert missing_from.code == "FROM_ACCOUNT_NOT_FOUND"

    missing_to = {"from_account": FROM_ACCOUNT, "to_account": missing, "amount": 100.0}
    approve(approvals, missing_to)
    missing_to_result = await transfer(
        runtime, "call_tr_missing_to", missing_to, approval_id=APPROVAL_ID
    )
    assert missing_to_result.ok is False
    assert missing_to_result.action is demo.DecisionAction.DENY
    assert missing_to_result.code == "ACCOUNT_NOT_FOUND"

    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(TO_ACCOUNT) == 5_000.0
    assert audit.records[-1].phase == "execution"
    assert audit.records[-1].code == "ACCOUNT_NOT_FOUND"


@async_test
async def test_transfer_normal_execution_stays_within_timeout() -> None:
    """正常业务不超时误杀：普通金额在 timeout_seconds=2.0 内完成并返回 OK（不确定点 Q5.2）。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    approve(approvals, arguments)

    started = time.perf_counter()
    result = await transfer(runtime, "call_tr_normal_timing", arguments, approval_id=APPROVAL_ID)
    elapsed = time.perf_counter() - started

    assert result.ok is True
    assert result.action is demo.DecisionAction.ALLOW
    assert result.code == "OK"
    assert elapsed < 2.0  # 远小于超时阈值，不应被误杀
    assert balance(FROM_ACCOUNT) == 99_000.0
    assert balance(TO_ACCOUNT) == 6_000.0


@async_test
async def test_transfer_idempotency_key_deduplicates_execution() -> None:
    """幂等键：同一 idempotency_key 重复提交只执行一次，第二次在预检阶段被 DUPLICATE_REQUEST 拒绝。"""

    runtime, approvals, _audit = demo.build_runtime()
    key = "idem_double_click_0001"
    arguments = {
        "from_account": FROM_ACCOUNT,
        "to_account": TO_ACCOUNT,
        "amount": 1_000.0,
        "idempotency_key": key,
    }

    approve(approvals, arguments)
    first = await transfer(runtime, "call_tr_idem_1", arguments, approval_id=APPROVAL_ID)
    assert first.code == "OK"

    # 同一幂等键再次提交：即使重新审批，也在预检阶段被拒绝，不再扣款。
    approve(approvals, arguments)
    duplicate = await transfer(runtime, "call_tr_idem_2", arguments, approval_id=APPROVAL_ID)
    assert duplicate.ok is False
    assert duplicate.action is demo.DecisionAction.DENY
    assert duplicate.code == "DUPLICATE_REQUEST"

    # 只执行一次：余额只变一次。
    assert balance(FROM_ACCOUNT) == 99_000.0
    assert balance(TO_ACCOUNT) == 6_000.0


@async_test
async def test_transfer_canonical_target_includes_from_to_amount() -> None:
    """canonical_target 粒度：输出为 from:to:amount，作规则匹配的内容键（不含时间窗）。"""

    transfer_def = next(t for t in demo.build_tools() if t.name == TRANSFER)
    args = demo.TransferArgs(from_account=FROM_ACCOUNT, to_account=TO_ACCOUNT, amount=1_000.0)
    target = transfer_def.canonical_target(args)

    # from + to + amount（float -> "1000.0"）
    expected = f"{FROM_ACCOUNT}:{TO_ACCOUNT}:1000.0"
    assert target == expected
