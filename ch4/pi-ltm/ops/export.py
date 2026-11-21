from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ltm_core import memory, project


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy the project OKF bundle to a directory")
    parser.add_argument("--project", default=None)
    parser.add_argument("--out", required=True)
    options = parser.parse_args()

    plugin_root = Path(__file__).resolve().parents[1]
    config = memory.load_config(plugin_root, options.project)
    info = project.resolve(options.project, None)
    bundle = memory._scope_root(info, "project", config) / "okf"
    if not bundle.is_dir():
        print(f"bundle not found: {bundle}")
        return 1

    destination = Path(options.out)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "okf"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle, target)
    print(f"exported {bundle} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
