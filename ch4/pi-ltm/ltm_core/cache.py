from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from ltm_core.types import CACHE_VERSION, now_iso


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load(cache_path: Path) -> dict:
    try:
        with open(cache_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "builtAt": "", "entries": {}}


def _persist(cache_path: Path, entries: dict) -> None:
    atomic_write_text(
        cache_path,
        json.dumps(
            {"version": CACHE_VERSION, "builtAt": now_iso(), "entries": entries},
            ensure_ascii=False,
            indent=2,
        ),
    )


def sync(
    bundle_root: Path,
    cache_path: Path,
    inspect: Callable[[Path], dict | None],
    only: set[str] | None = None,
) -> dict:
    previous = load(cache_path)
    previous_entries: dict = previous.get("entries", {})
    dirty = False
    reserved = {"index.md", "log.md"}
    if only is None:
        entries: dict = {}
        files = (
            sorted(path for path in bundle_root.glob("*.md") if path.name not in reserved)
            if bundle_root.is_dir()
            else []
        )
    else:
        entries = dict(previous_entries)
        files = [bundle_root / rel for rel in sorted(only)]

    for file in files:
        rel = file.relative_to(bundle_root).as_posix()
        try:
            stat = file.stat()
        except OSError:
            if entries.pop(rel, None) is not None:
                dirty = True
            continue
        hit = previous_entries.get(rel)
        if hit and hit.get("mtime") == stat.st_mtime and hit.get("size") == stat.st_size:
            entries[rel] = hit
            continue
        record = inspect(file)
        if record is None:
            if entries.pop(rel, None) is not None:
                dirty = True
            continue
        entries[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "record": record}
        dirty = True

    if dirty or (only is None and set(entries) != set(previous_entries)):
        _persist(cache_path, entries)
    if only is not None:
        return {rel: entries[rel] for rel in only if rel in entries}
    return entries
