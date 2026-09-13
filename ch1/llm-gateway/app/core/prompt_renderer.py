"""
PromptRenderer — Jinja2 模板渲染 + 模板存储（SPEC §4.5）。

- 模板存于 SQLite（MetricsStore.prompt_templates），支持版本更新
- 渲染时检测缺失变量 → PromptMissingVarsError（不调用上游）
- 系统提示词与用户输入隔离，降低 Prompt 注入面（SPEC §8.2）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError

from ..errors import InvalidRequestError, PromptMissingVarsError
from ..storage.metrics_store import MetricsStore

logger = logging.getLogger("llm-gateway.prompt-renderer")

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class PromptRenderer:
    """提示词渲染器。"""

    def __init__(self, store: MetricsStore) -> None:
        self._store = store
        self._env = Environment(undefined=StrictUndefined, autoescape=False)
        # 内存缓存：(template_id, version) -> content（模板更新由 upsert 后失效）
        self._cache: dict[tuple[str, int | None], str] = {}

    def invalidate(self, template_id: str) -> None:
        for key in [k for k in self._cache if k[0] == template_id]:
            self._cache.pop(key, None)

    def _extract_vars(self, content: str) -> set[str]:
        return set(_VAR_PATTERN.findall(content))

    async def render(
        self, template_id: str, variables: dict[str, Any], version: int | None = None
    ) -> str:
        """渲染模板。缺失变量 → PromptMissingVarsError。

        version=None → 使用最新版本；否则精确渲染指定版本。
        """
        key = (template_id, version)
        content = self._cache.get(key)
        if content is None:
            record = await self._store.get_template(template_id, version=version)
            if record is None:
                ver_desc = f" (version {version})" if version is not None else ""
                raise InvalidRequestError(f"Prompt template not found: {template_id}{ver_desc}")
            content = record["content"]
            self._cache[key] = content

        required = self._extract_vars(content)
        missing = required - set(variables.keys())
        if missing:
            raise PromptMissingVarsError(
                f"Prompt template '{template_id}' missing variables: {sorted(missing)}"
            )
        try:
            return self._env.from_string(content).render(variables)
        except UndefinedError as exc:
            raise PromptMissingVarsError(f"Prompt template '{template_id}' undefined variable: {exc}") from exc
        except TemplateSyntaxError as exc:
            raise InvalidRequestError(f"Prompt template '{template_id}' syntax error: {exc}") from exc

    async def upsert(self, template_id: str, name: str, content: str) -> int:
        version = await self._store.upsert_template(template_id, name, content)
        self.invalidate(template_id)
        return version
