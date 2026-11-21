from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ltm_core.types import LtmError

_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_MNT = re.compile(r"^/mnt/([A-Za-z])/(.*)$")


def to_internal(raw: str) -> str:
    drive = _DRIVE.match(raw)
    if drive and os.name != "nt":
        return f"/mnt/{drive.group(1).lower()}/{drive.group(2).replace('\\', '/')}"
    if os.name == "nt":
        mount = _MNT.match(raw.replace("\\", "/"))
        if mount:
            return f"{mount.group(1).upper()}:\\" + mount.group(2).replace("/", "\\")
        return raw
    return raw


def normalize(raw: str | None, cwd: str | None = None) -> str:
    candidate = Path(to_internal(raw or cwd or os.getcwd()))
    if not candidate.is_absolute():
        candidate = Path(to_internal(cwd or os.getcwd())) / candidate
    return str(candidate.resolve())


def to_display(path: str) -> str:
    if os.name == "nt":
        return path
    mount = _MNT.match(path)
    if mount:
        return f"{mount.group(1).upper()}:\\" + mount.group(2).replace("/", "\\")
    return path


def project_id(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def resolve(raw: str | None, cwd: str | None = None) -> dict:
    canonical = normalize(raw, cwd)
    return {"path": canonical, "project_id": project_id(canonical)}


def allowed_roots(cwd: str | None, extra: list[str] | None = None) -> list[str]:
    roots: list[str] = [normalize(cwd or os.getcwd())]
    for item in extra or []:
        if item:
            roots.append(normalize(str(item), cwd))
    for item in os.environ.get("PI_LTM_ALLOWED_ROOTS", "").split(os.pathsep):
        if item.strip():
            roots.append(normalize(item.strip(), cwd))
    unique: list[str] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def assert_allowed(canonical: str, roots: list[str]) -> None:
    target = Path(canonical)
    for root in roots:
        try:
            if target == Path(root) or target.is_relative_to(root):
                return
        except (OSError, ValueError):
            continue
    raise LtmError(
        "PATH_FORBIDDEN",
        f"project path is outside the allowed roots: {canonical}",
        {"allowedRoots": roots},
    )
