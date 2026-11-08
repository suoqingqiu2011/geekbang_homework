"""唯一门禁：参数、风险、重复、预算、审批。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from contracts import (
    ActionRequest,
    Decision,
    GateVerdict,
    RiskClass,
    StopReason,
    StructuredError,
    call_signature,
    canonical_json,
    sha256_file,
)
from sandbox import SandboxViolation


PreCheck = Callable[[ActionRequest, Any], None]


@dataclass
class _BatchAccounting:
    """批次内预占：同批请求在 commit 前看不到彼此的台账与预算消耗。"""

    counts: dict[str, int] = field(default_factory=dict)
    applied: set[str] = field(default_factory=set)
    reserved: dict[str, float] = field(default_factory=dict)

    def count(self, signature: str) -> int:
        return int(self.counts.get(signature, 0))


@dataclass
class ToolSpec:
    name: str
    risk: RiskClass
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    types: dict[str, Any] = field(default_factory=dict)
    choices: dict[str, set[Any]] = field(default_factory=dict)
    pre_check: PreCheck | None = None
    hint: str = ""

    def all_fields(self) -> tuple[str, ...]:
        return tuple(self.required) + tuple(self.optional)


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return " 或 ".join(_type_name(item) for item in expected)
    return getattr(expected, "__name__", str(expected))


def _type_ok(value: Any, expected: Any) -> bool:
    if isinstance(expected, tuple):
        return any(_type_ok(value, item) for item in expected)
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate_arguments(spec: ToolSpec, arguments: Any) -> tuple[dict[str, Any], StructuredError | None]:
    if not isinstance(arguments, dict):
        return {}, StructuredError(
            code="invalid_arguments",
            message=f"参数必须是对象，实际为 {type(arguments).__name__}",
            tool=spec.name,
            rule="arguments",
            detail={"hint": spec.hint},
        )

    known = set(spec.all_fields())
    unknown = [key for key in arguments if key not in known]
    if unknown:
        return {}, StructuredError(
            code="unknown_argument",
            message=f"存在未知参数：{', '.join(sorted(unknown))}；允许的参数：{', '.join(sorted(known))}",
            tool=spec.name,
            rule="arguments",
            detail={"unknown": sorted(unknown), "allowed": sorted(known), "hint": spec.hint},
        )

    missing = [key for key in spec.required if key not in arguments]
    if missing:
        return {}, StructuredError(
            code="missing_argument",
            message=f"缺少必填参数：{', '.join(missing)}",
            tool=spec.name,
            rule="arguments",
            detail={"missing": missing, "required": list(spec.required), "hint": spec.hint},
        )

    cleaned: dict[str, Any] = {}
    for key in spec.all_fields():
        if key not in arguments:
            continue
        value = arguments[key]
        expected = spec.types.get(key)
        if expected is not None and not _type_ok(value, expected):
            return {}, StructuredError(
                code="invalid_argument_type",
                message=f"参数 {key} 类型错误：期望 {_type_name(expected)}，实际 {type(value).__name__}",
                tool=spec.name,
                rule="arguments",
                detail={"argument": key, "expected": _type_name(expected), "hint": spec.hint},
            )
        if key in spec.choices and value not in spec.choices[key]:
            return {}, StructuredError(
                code="invalid_argument_value",
                message=f"参数 {key} 取值非法：{value!r}；允许 {sorted(spec.choices[key])}",
                tool=spec.name,
                rule="arguments",
                detail={"argument": key, "allowed": sorted(spec.choices[key]), "hint": spec.hint},
            )
        cleaned[key] = value
    return cleaned, None


class Gate:
    def __init__(self, specs: dict[str, ToolSpec], state: Any, audit: Any, policy: Any, sandbox: Any) -> None:
        self.specs = specs
        self.state = state
        self.audit = audit
        self.policy = policy
        self.sandbox = sandbox
        self._decisions: dict[str, Decision] = {}

    # -- 判定 ---------------------------------------------------------------
    def evaluate(self, request: ActionRequest, *, batch: _BatchAccounting | None = None) -> GateVerdict:
        spec = self.specs.get(request.tool_name)
        if spec is None:
            return self._deny(request, "unknown_tool", f"未知工具：{request.tool_name}", "unknown_tool")
        request.risk = spec.risk

        cleaned, error = validate_arguments(spec, request.arguments)
        if error is not None:
            error.tool_call_id = request.tool_call_id
            return GateVerdict(request=request, decision=Decision.DENY, rule="arguments", error=error)
        request.arguments = cleaned
        request.signature = call_signature(request.tool_name, cleaned)

        # tool_calls 在 commit 时对每个请求都计一次（拒绝也计），先预占再判定。
        tool_calls_room = self._budget_has_room("tool_calls", batch)
        if batch is not None:
            batch.reserved["tool_calls"] = batch.reserved.get("tool_calls", 0.0) + 1.0

        if spec.pre_check is not None:
            try:
                spec.pre_check(request, self.sandbox)
            except SandboxViolation as exc:
                return self._deny(request, "sandbox_violation", str(exc), "sandbox")
            except Exception as exc:
                return self._deny(request, "pre_check_failed", f"{type(exc).__name__}: {exc}", "pre_check")

        if spec.risk == RiskClass.WRITE:
            target_path = request.context.get("target_path")
            if target_path:
                request.context["pre_sha256"] = sha256_file(target_path)
            request.context["fingerprint_at_request"] = self.sandbox.fingerprint()

        # 重复写入：只把 applied=true 视为已执行，附首次结果回执。
        already_applied = self.state.ledger_applied(request.signature) or (
            batch is not None and request.signature in batch.applied
        )
        if spec.risk == RiskClass.WRITE and already_applied:
            first = self.state.ledger_result(request.signature)
            return GateVerdict(
                request=request,
                decision=Decision.DENY,
                rule="duplicate_write",
                error=StructuredError(
                    code="duplicate_write",
                    message="该写入已经执行过，拒绝重复执行；首次结果见 detail.first_result",
                    tool=request.tool_name,
                    tool_call_id=request.tool_call_id,
                    rule="duplicate_write",
                    detail={"signature": request.signature, "first_result": first, "same_batch": first is None},
                ),
            )

        repeat_count = self.state.ledger_count(request.signature)
        if batch is not None:
            repeat_count += batch.count(request.signature)
        if repeat_count >= int(self.policy.repeat_limit):
            return GateVerdict(
                request=request,
                decision=Decision.DENY,
                rule="repeat_limit",
                error=StructuredError(
                    code="repeat_limit",
                    message=(
                        f"相同动作已出现 {repeat_count} 次，"
                        f"达到上限 {self.policy.repeat_limit}；请更换参数或调整计划"
                    ),
                    tool=request.tool_name,
                    tool_call_id=request.tool_call_id,
                    rule="repeat_limit",
                    detail={"signature": request.signature},
                ),
            )

        if not tool_calls_room:
            return self._deny(request, "budget_exceeded", "tool_calls 预算已耗尽", "budget")
        if spec.risk == RiskClass.WRITE and not self._budget_has_room("writes", batch):
            return self._deny(request, "budget_exceeded", "writes 预算已耗尽", "budget")
        if request.tool_name == "run_test" and not self._budget_has_room("test_runs", batch):
            return self._deny(request, "budget_exceeded", "test_runs 预算已耗尽", "budget")

        if spec.risk == RiskClass.WRITE and bool(getattr(self.policy, "require_write_approval", True)):
            fingerprint = request.context.get("fingerprint_at_request") or self.sandbox.fingerprint()
            approval = self.state.find_approval(request.signature, fingerprint, request.step)
            if approval is not None:
                self._reserve_allow(request, spec, batch)
                return GateVerdict(
                    request=request,
                    decision=Decision.ALLOW,
                    rule="approval_granted",
                    detail={"approval": approval},
                )
            return GateVerdict(
                request=request,
                decision=Decision.REQUIRE_APPROVAL,
                rule="write_requires_approval",
                error=StructuredError(
                    code="approval_required",
                    message="写操作需要人工审批",
                    tool=request.tool_name,
                    tool_call_id=request.tool_call_id,
                    rule="write_requires_approval",
                    detail={"signature": request.signature},
                ),
            )

        self._reserve_allow(request, spec, batch)
        return GateVerdict(request=request, decision=Decision.ALLOW, rule="allowed")

    def _budget_has_room(self, key: str, batch: _BatchAccounting | None) -> bool:
        used = self.state.budget_used(key)
        if batch is not None:
            used += batch.reserved.get(key, 0.0)
        return used < self.state.budget_limit(key)

    def _reserve_allow(self, request: ActionRequest, spec: ToolSpec, batch: _BatchAccounting | None) -> None:
        if batch is None:
            return
        if spec.risk == RiskClass.WRITE:
            batch.reserved["writes"] = batch.reserved.get("writes", 0.0) + 1.0
            batch.applied.add(request.signature)
        if request.tool_name == "run_test":
            batch.reserved["test_runs"] = batch.reserved.get("test_runs", 0.0) + 1.0

    @staticmethod
    def _deny(request: ActionRequest, code: str, message: str, rule: str) -> GateVerdict:
        return GateVerdict(
            request=request,
            decision=Decision.DENY,
            rule=rule,
            error=StructuredError(
                code=code,
                message=message,
                tool=request.tool_name,
                tool_call_id=request.tool_call_id,
                rule=rule,
            ),
        )

    # -- 批次 ---------------------------------------------------------------
    def evaluate_batch(self, requests: list[ActionRequest], step: int) -> list[GateVerdict]:
        verdicts: list[GateVerdict] = []
        batch = _BatchAccounting()
        for request in requests:
            request.step = int(step)
            verdict = self.evaluate(request, batch=batch)
            verdicts.append(verdict)
            # 同批后续请求必须看得到本请求已占用的次数与已写入标记。
            batch.counts[request.signature] = batch.count(request.signature) + 1
            self._decisions[request.signature] = verdict.decision
            self.audit.append(
                "gate.decision",
                self.state.run_id,
                tool_call_id=request.tool_call_id,
                actor="harness",
                decision=verdict.decision.value,
                rule=verdict.rule,
                detail={
                    "tool": request.tool_name,
                    "risk": request.risk.value,
                    "signature": request.signature,
                    "arguments": request.arguments,
                    "step": request.step,
                    "error": verdict.error.to_dict() if verdict.error else None,
                },
            )
        return verdicts

    def commit_batch(
        self,
        requests: list[ActionRequest],
        verdicts: list[GateVerdict],
        step: int,
        *,
        charge_side_effects: bool = True,
    ) -> None:
        for request, verdict in zip(requests, verdicts):
            # tool_calls 入口即计（拒绝也计）。
            self.state.charge("tool_calls", 1)
            # 整批无法执行时，ALLOW 项既没落盘也没跑测，不能记台账、扣写预算或消耗审批。
            if charge_side_effects or verdict.decision != Decision.ALLOW:
                self.state.ledger_record(request.signature, request.tool_call_id)
            if verdict.decision == Decision.ALLOW:
                if not charge_side_effects:
                    continue
                if request.risk == RiskClass.WRITE:
                    self.state.charge("writes", 1)
                    approval = self.state.find_approval(
                        request.signature,
                        request.context.get("fingerprint_at_request") or self.sandbox.fingerprint(),
                        request.step,
                    )
                    if approval is not None:
                        self.state.consume_approval(approval["tool_call_id"])
                    self.state.pop_pending_by_signature(request.signature)
                if request.tool_name == "run_test":
                    self.state.charge("test_runs", 1)
            elif verdict.decision == Decision.REQUIRE_APPROVAL:
                self.state.record_pending(
                    request.tool_call_id,
                    tool_name=request.tool_name,
                    arguments=request.arguments,
                    signature=request.signature,
                    pre_sha256=request.context.get("pre_sha256"),
                    fingerprint_at_request=request.context.get("fingerprint_at_request") or self.sandbox.fingerprint(),
                    step=request.step,
                    target=request.context.get("target", ""),
                )
                self.state.set_blocked(
                    "等待人工审批",
                    stop_reason=StopReason.APPROVAL_REQUIRED.value,
                    signature=request.signature,
                )
                self.audit.append(
                    "approval.requested",
                    self.state.run_id,
                    tool_call_id=request.tool_call_id,
                    actor="harness",
                    decision=Decision.REQUIRE_APPROVAL.value,
                    rule="write_requires_approval",
                    detail={
                        "signature": request.signature,
                        "tool": request.tool_name,
                        "pre_sha256": request.context.get("pre_sha256"),
                        "target": request.context.get("target", ""),
                        "step": request.step,
                    },
                )
        self.audit.append(
            "gate.batch",
            self.state.run_id,
            actor="harness",
            decision="batch",
            rule="commit",
            detail={"step": step, "signatures": [request.signature for request in requests]},
        )

    def decision_for(self, signature: str) -> Decision | None:
        return self._decisions.get(signature)

    def clear_batch(self) -> None:
        self._decisions.clear()

    def describe(self) -> str:
        return canonical_json(
            {
                name: {
                    "risk": spec.risk.value,
                    "required": list(spec.required),
                    "optional": list(spec.optional),
                }
                for name, spec in self.specs.items()
            }
        )
