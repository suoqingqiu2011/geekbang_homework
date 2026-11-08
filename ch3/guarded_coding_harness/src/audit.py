"""审计：append-only JSONL + 哈希链 + 按 run/tool_call 溯源。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from contracts import canonical_json, now_iso, sha256_text, ulid

GENESIS = "genesis"


class AuditAnchorError(RuntimeError):
    """审计头锚点与日志尾部不一致：可能被截断、清空或写入竞争。"""


class AuditLog:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path = self.path.with_name(self.path.name + ".head")
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

    def head(self) -> dict[str, Any] | None:
        if not self.head_path.is_file():
            return None
        try:
            payload = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_head(self, seq: int, digest: str) -> None:
        tmp = self.head_path.with_name(self.head_path.name + f".tmp-{ulid()}")
        tmp.write_text(canonical_json({"seq": int(seq), "hash": digest}), encoding="utf-8")
        os.replace(tmp, self.head_path)

    def _state(self) -> tuple[int, str]:
        if self._seq is None or self._prev_hash is None:
            items = self.records()
            head = self.head()
            # 续写前先对齐头锚点：截断/清空后若静默续写，会洗掉篡改痕迹。
            if head is not None:
                expected = int(head.get("seq", 0))
                actual = len(items)
                if actual != expected:
                    raise AuditAnchorError(
                        f"审计日志与头锚点不一致：锚点 seq={expected}，实际 {actual} 条；"
                        "疑似尾部被截断或日志被清空"
                    )
                if expected and items[-1].get("hash") != head.get("hash"):
                    raise AuditAnchorError("审计日志最后一条的哈希与头锚点不一致")
            elif items:
                raise AuditAnchorError("审计日志非空但缺少头锚点文件，无法证明尾部完整")
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
        self._write_head(seq, body["hash"])
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

        head = self.head()
        if not items:
            if head is not None:
                return False, "审计日志为空但头锚点存在：疑似被清空"
            return True, "ok"
        if head is None:
            return False, "审计日志非空但缺少头锚点：无法排除尾部截断"
        expected_seq = int(head.get("seq", 0))
        if len(items) < expected_seq:
            return False, f"审计尾部被截断：头锚点 seq={expected_seq}，实际 {len(items)} 条"
        if len(items) > expected_seq:
            return False, f"审计记录多于头锚点：锚点 seq={expected_seq}，实际 {len(items)} 条"
        if items[-1].get("hash") != head.get("hash"):
            return False, "审计日志最后一条的哈希与头锚点不一致"
        return True, "ok"
