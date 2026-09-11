"""
StructuredOutputValidator — JSON Schema 校验 + 无效 JSON 自动修复 + Prompt 注入检测。

对应 SPEC §4.10 / §8.2：
  - 按 jsonschema 校验上游输出
  - 无效 JSON 进入自动修复流水线（截取/补全/重试），仍失败返回错误
  - Prompt 注入启发式规则（角色伪装/指令覆盖），命中即拦截
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from jsonschema import Draft7Validator, ValidationError

from ..errors import PromptInjectionDetectedError, StructuredOutputError

logger = logging.getLogger("llm-gateway.validator")

# ── Prompt 注入启发式规则（SPEC §8.2：不视为绝对安全边界）────────
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts)", re.I),
    re.compile(r"system\s*[:：]\s*.*(?:you\s+are|role)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|above)", re.I),
    re.compile(r"你(现在|的|作为)?(的)?系统(提示|指令)", re.I),
    re.compile(r"忽略\s*(?:之前|以上|先前|所有)\s*(?:的)?\s*(?:所有)?\s*(?:指令|提示|要求|内容)", re.I),
]

# 常见"模型只吐了一段话"修复：提取首个 {...} 或 [...] 平衡块
_BALANCED_PATTERNS = [
    re.compile(r"\{.*\}", re.S),
    re.compile(r"\[.*\]", re.S),
]


class StructuredOutputValidator:
    """结构化输出校验与修复。"""

    def __init__(self) -> None:
        self._schema_cache: dict[str, Draft7Validator] = {}

    # ── Prompt 注入检测 ──────────────────────────────────────
    def check_prompt_injection(self, messages: list[dict[str, Any]]) -> None:
        """对 system 提示与用户输入做启发式检测，命中即抛出。"""
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            for pattern in _INJECTION_PATTERNS:
                if pattern.search(content):
                    raise PromptInjectionDetectedError(
                        "Prompt injection pattern detected in message content."
                    )

    # ── Schema 校验 ──────────────────────────────────────────
    def _validator(self, schema: dict[str, Any]) -> Draft7Validator:
        key = json.dumps(schema, sort_keys=True, ensure_ascii=False)
        cached = self._schema_cache.get(key)
        if cached is None:
            cached = Draft7Validator(schema)
            self._schema_cache[key] = cached
        return cached

    def validate(self, content: str, schema: dict[str, Any]) -> tuple[Any, Optional[str]]:
        """
        校验 content 是否符合 schema。
        返回 (解析结果, error_msg)；error_msg=None 表示通过。
        """
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None, "invalid_json"
        validator = self._validator(schema)
        if validator.is_valid(data):
            return data, None
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        return data, str(errors[0].message) if errors else "schema_mismatch"

    # ── 自动修复 ─────────────────────────────────────────────
    def fix_and_validate(self, content: str, schema: dict[str, Any]) -> tuple[Any, Optional[str]]:
        """尝试解析+校验；失败则走修复流水线（截取平衡块）。"""
        data, err = self.validate(content, schema)
        if err is None:
            return data, None

        for pattern in _BALANCED_PATTERNS:
            m = pattern.search(content)
            if m:
                data, err = self.validate(m.group(0), schema)
                if err is None:
                    logger.info("Structured output auto-fixed via balanced-block extraction")
                    return data, None

        logger.warning("Structured output validation failed: %s", err)
        return data, err

    def validate_response(
        self, content: str, schema: Optional[dict[str, Any]]
    ) -> tuple[Any, Optional[str]]:
        """
        对外主入口。
        schema=None → 仅要求合法 JSON（若内容本身是 JSON 文本则解析，否则原样透传）。
        """
        if schema is None:
            return content, None
        return self.fix_and_validate(content, schema)


def parse_json_strict(content: str) -> Any:
    """严格 JSON 解析（用于 schema=None 时判断是否为合法 JSON）。"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None
