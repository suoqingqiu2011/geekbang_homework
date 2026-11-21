from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ltm_core import memory, okf_cli, project


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the bundle and rebuild the index cache")
    parser.add_argument("--project", default=None)
    options = parser.parse_args()

    plugin_root = Path(__file__).resolve().parents[1]
    info = project.resolve(options.project, None)
    config = memory.load_config(plugin_root, info["path"])
    root = memory._scope_root(info, "project", config)
    bundle = root / "okf"

    if not bundle.is_dir():
        print(json.dumps({"ok": False, "reason": "bundle missing", "bundle": str(bundle)}))
        return 1

    validation = okf_cli.validate(plugin_root, bundle, config)
    records = memory._load(plugin_root, root, config)
    report = {
        "bundle": str(bundle),
        "validateRc": validation["rc"],
        "validate": validation["payload"],
        "records": len(records),
        "keys": [item["key"] for item in records],
        "lockLeftover": (root / ".lock").exists(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if validation["rc"] != 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
