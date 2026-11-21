from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def file_lock(
    lock_path: Path,
    timeout_ms: int = 5000,
    stale_ms: int = 10000,
    retries: int = 8,
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_ms / 1000.0
    delay = 0.02
    attempt = 0
    handle: int | None = None

    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, f"{os.getpid()} {time.time()}\n".encode("utf-8"))
            break
        except FileExistsError:
            attempt += 1
            try:
                age_ms = (time.time() - lock_path.stat().st_mtime) * 1000
            except FileNotFoundError:
                continue
            if age_ms > stale_ms:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if attempt >= retries or time.time() >= deadline:
                raise TimeoutError(f"lock timeout after {timeout_ms}ms: {lock_path}")
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    try:
        yield
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
