from __future__ import annotations

from datetime import datetime, timezone

MEMORY_TYPE = "Memory"
GENERATED_BY = "process:pi-okf-memory"
STATUS_ACTIVE = "stable"
STATUS_DEPRECATED = "deprecated"
CACHE_VERSION = 1


class LtmError(Exception):
    def __init__(self, code: str, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ok(data: dict | None = None) -> dict:
    return {"ok": True, "code": "OK", "message": "", "data": data or {}}


def fail(code: str, message: str, data: dict | None = None) -> dict:
    return {"ok": False, "code": code, "message": message, "data": data or {}}
