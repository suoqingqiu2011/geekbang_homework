from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ltm_core import memory
from ltm_core.types import LtmError, fail

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

HANDLERS = {
    "write": memory.write,
    "read": memory.read,
    "update": memory.update,
    "delete": memory.delete,
    "warm": memory.warm,
    "status": memory.status,
}


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        print(json.dumps(fail("BAD_REQUEST", "stdin must be a JSON object"), ensure_ascii=False))
        return 0

    op = str(payload.get("op") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}

    try:
        config = memory.load_config(PLUGIN_ROOT, cwd)
        handler = HANDLERS.get(op)
        if handler is None:
            result = fail("UNKNOWN_OP", f"unknown op: {op}", {"ops": sorted(HANDLERS)})
        else:
            result = handler(PLUGIN_ROOT, cwd, args, config)
    except LtmError as error:
        result = fail(error.code, error.message, error.data)
    except TimeoutError as error:
        result = fail("LOCK_TIMEOUT", str(error))
    except Exception as error:
        result = fail("INTERNAL", f"{type(error).__name__}: {error}")

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
