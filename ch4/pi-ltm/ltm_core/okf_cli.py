from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class OkfCliError(RuntimeError):
    pass


def _cli(plugin_root: Path, config: dict) -> list[str]:
    rel = str(config.get("okfCli") or "node_modules/pi-okf/dist/cli.js")
    candidate = Path(rel)
    if not candidate.is_absolute():
        candidate = plugin_root / rel
    if candidate.is_file():
        return [os.environ.get("PI_LTM_NODE", "node"), str(candidate)]
    executable = shutil.which("pi-okf")
    if executable:
        return [executable]
    raise OkfCliError("pi-okf CLI not found; run `npm install` in the plugin root")


def _run(cmd: list[str], timeout: int = 90) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def init_bundle(plugin_root: Path, bundle: Path, config: dict) -> bool:
    if (bundle / "index.md").is_file():
        return False
    bundle.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(_cli(plugin_root, config) + ["init", bundle.name, "--dir", str(bundle.parent)])
    if not (bundle / "index.md").is_file():
        raise OkfCliError(f"okf init failed rc={rc}: {(err or out).strip()[:500]}")
    return True


def validate(plugin_root: Path, bundle: Path, config: dict) -> dict:
    rc, out, err = _run(_cli(plugin_root, config) + ["validate", str(bundle), "--json"])
    try:
        payload = json.loads(out) if out.strip() else {}
    except ValueError:
        payload = {"raw": out.strip()[:2000]}
    return {"rc": rc, "payload": payload, "stderr": err.strip()[:2000]}


def index_write(plugin_root: Path, bundle: Path, config: dict) -> None:
    rc, out, err = _run(_cli(plugin_root, config) + ["index", str(bundle), "--write"])
    if rc != 0:
        raise OkfCliError(f"okf index failed rc={rc}: {(err or out or '').strip()[:500]}")


def capture(plugin_root: Path, bundle: Path, config: dict, summary: str) -> None:
    rc, out, err = _run(_cli(plugin_root, config) + ["capture", str(bundle), "--summary", summary])
    if rc != 0:
        raise OkfCliError(f"okf capture failed rc={rc}: {(err or out or '').strip()[:500]}")


def inspect_json(plugin_root: Path, file: Path, config: dict) -> dict | None:
    rc, out, err = _run(_cli(plugin_root, config) + ["inspect", str(file), "--json"])
    if rc != 0 or not out.strip():
        return None
    try:
        payload = json.loads(out)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("frontmatter", "data", "meta"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return payload
