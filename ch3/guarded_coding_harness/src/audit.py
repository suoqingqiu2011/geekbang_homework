"""审计：append-only JSONL + 哈希链 + 按 run/tool_call 溯源。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from contracts import canonical_json, now_iso, sha256_text

GENESIS = "genesis"


class AuditLog:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq: int | None = None
        self._prev_hash: str | None = None

    # -- 读取 ---------------------------------------------------------------
    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result

    def _state(self) -> tuple[int, str]:
        if self._seq is None or self._prev_hash is None:
            items = self.records()
            self._seq = len(items)
            self._prev_hash = items[-1].get("hash", GENESIS) if items else GENESIS
        return self._seq, self._prev_hash

    # -- 追加 ------------------------------------------------------------------
    def append(
        self,
        event: str,
        run_id: str | None,
        tool_call_id: str | None = None,
        actor: str = "harness",
        decision: str | None = None,
        rule: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seq, prev = self._state()
        seq += 1
        body = {
            "seq": seq,
            "ts": now_iso(),
            "event": event,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "actor": actor,
            "decision": decision,
            "rule": rule,
            "detail": detail or {},
            "prev_hash": prev,
        }
        body["hash"] = "h:" + sha256_text(prev + canonical_json(body))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(canonical_json(body) + "\n")
        self._seq = seq
        self._prev_hash = body["hash"]
        return body

    # -- 溯源 ---------------------------------------------------------------
    def trace(self, tool_call_id: str) -> list[dict[str, Any]]:
        return [item for item in self.records() if item.get("tool_call_id") == tool_call_id]

    def by_run(self, run_id: str) -> list[dict[str, Any]]:
        return [item for item in self.records() if item.get("run_id") == run_id]

    # -- 校验 ---------------------------------------------------------------
    def verify(self) -> tuple[bool, str]:
        items = self.records()
        prev = GENESIS
        for index, item in enumerate(items, start=1):
            if int(item.get("seq", 0)) != index:
                return False, f"seq 不连续：期望 {index}，实际 {item.get('seq')}"
            if item.get("prev_hash") != prev:
                return False, f"prev_hash 不一致：第 {index} 条"
            body = {key: value for key, value in item.items() if key != "hash"}
            expected = "h:" + sha256_text(prev + canonical_json(body))
            if item.get("hash") != expected:
                return False, f"哈希校验失败：第 {index} 条"
            prev = item["hash"]
        return True, "ok"
