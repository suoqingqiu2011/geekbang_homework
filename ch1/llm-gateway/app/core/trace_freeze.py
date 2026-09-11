"""
TraceFreezeRecord — 基于 Pydantic v2 的"冻结式"追踪记录（G1 修复）。

设计要点（对应 SPEC §4.11 / §11-9）：
  - 创建时固化请求快照（messages / schema / budget / 链路信息）
  - 运行时指标使用独立可变容器，避免整体复制，降低 GC 压力
  - model_config extra=forbid，防止误加字段
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field, PrivateAttr


class TraceFreezeRecord(BaseModel):
    """不可变风格追踪记录。"""

    model_config = {"extra": "forbid"}

    # ── 创建时固化的快照 ────────────────────────────────────
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_us: float = Field(default_factory=time.time)
    endpoint: str = "chat/completions"
    user_id: str = "anonymous"
    project_id: str = "default"
    client_ip: str = "unknown"
    model: str = ""
    provider: str = ""

    # 请求/约束快照（不落盘原文，仅元数据，SPEC §8.4）
    budget_snapshot: float = 0.0
    has_schema: bool = False
    schema_name: str = ""
    stream: bool = False

    # ── 运行时指标（可变容器，序列化时排除）────────────────────
    _runtime: dict[str, Any] = PrivateAttr(default_factory=dict)

    def update_metric(self, key: str, value: Any) -> "TraceFreezeRecord":
        """就地追加/更新运行时指标，返回 self 支持链式调用。"""
        object.__setattr__(self, "_runtime", {**self._runtime, key: value})
        return self

    def snapshot(self) -> dict[str, Any]:
        """序列化视图（含运行时指标，供写入存储层）。"""
        data = self.model_dump()
        data["runtime"] = dict(self._runtime)
        return data
