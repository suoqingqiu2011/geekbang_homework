"""执行边界：路径三区、命令白名单、超时、取消与真实终止。"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from contracts import canonical_json, now_iso, sha256_file, ulid


class SandboxViolation(Exception):
    """沙箱拒绝：路径越界、敏感目标、命令不在白名单等。"""


DEFAULT_TIMEOUT = 30.0
TAIL_CHARS = 4000
POLL_SECONDS = 0.05
KILL_GRACE_SECONDS = 2.0

FORBIDDEN_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube"}
FORBIDDEN_NAMES = {
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "credentials",
    "netrc",
    ".netrc",
}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".keystore"}

SHELL_METACHARS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "&", "\n", "\r")

ALLOWED_COMMAND_NAMES = {
    "python",
    "python3",
    "py",
    "pytest",
    "git",
    "pwd",
    "ls",
    "dir",
    "echo",
    "where",
    "which",
}

GIT_READONLY_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "describe"}

MINIMAL_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "LANG",
    "LC_ALL",
)

SKIP_DIR_NAMES = {".harness", ".git", "__pycache__", ".pytest_cache"}
# artifacts 只在工区根目录跳过；src/artifacts 之类的深层同名目录仍计入指纹。
SKIP_ROOT_DIR_NAMES = {"artifacts"}


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    terminated: bool = False
    pid: int | None = None
    log_path: str = ""
    output_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "duration": round(self.duration, 4),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "terminated": self.terminated,
            "pid": self.pid,
            "log_path": self.log_path,
            "output_tail": self.output_tail,
        }


def _is_within(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _forbidden_parts(parts: Sequence[str]) -> bool:
    for part in parts:
        low = part.lower()
        if low in FORBIDDEN_DIRS or low in FORBIDDEN_NAMES:
            return True
        if low.startswith(".env"):
            return True
        if Path(part).suffix.lower() in FORBIDDEN_SUFFIXES:
            return True
    return False


def workspace_fingerprint(root: str | os.PathLike[str]) -> str:
    """fp:v1:sha256(canonical_json(sorted[(relpath, sha256)]))。"""
    base = os.path.abspath(str(root))
    entries: list[list[str]] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        at_root = os.path.abspath(dirpath) == base
        skip = SKIP_DIR_NAMES | (SKIP_ROOT_DIR_NAMES if at_root else set())
        dirnames[:] = [d for d in sorted(dirnames) if d not in skip]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full):
                continue
            try:
                rel = os.path.relpath(full, base).replace(os.sep, "/")
            except ValueError:
                continue
            digest = sha256_file(full)
            if digest:
                entries.append([rel, digest])
    payload = canonical_json(entries)
    return "fp:v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _WinJob:
    """Windows Job Object：KILL_ON_JOB_CLOSE + TerminateJobObject。"""

    KILL_ON_JOB_CLOSE = 0x0002000
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_INFORMATION = 0x0200
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("CreateJobObjectW failed")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(
            self.handle,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

    def assign(self, pid: int) -> bool:
        ctypes = self._ctypes
        handle = self._k32.OpenProcess(self.PROCESS_TERMINATE | self.PROCESS_SET_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, handle))
        finally:
            self._k32.CloseHandle(handle)

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def is_process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int | None, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(POLL_SECONDS)
    return not is_process_alive(pid)


def kill_tree(pid: int | None, job: "_WinJob" | None = None) -> bool:
    """杀掉进程树并确认退出；返回是否确认退出。"""
    if not pid:
        return True
    if job is not None:
        try:
            job.terminate()
        except Exception:
            pass
    if os.name == "nt":
        if job is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return _wait_dead(pid, KILL_GRACE_SECONDS)


class Sandbox:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        writable: Sequence[str] = ("artifacts",),
        timeout: float = DEFAULT_TIMEOUT,
        allowed_commands: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        log_dir: str = "artifacts/.harness/logs",
    ) -> None:
        self.root = os.path.realpath(str(root))
        self.writable = tuple(writable)
        self.timeout = float(timeout)
        self.allowed_commands = set(allowed_commands) if allowed_commands else set(ALLOWED_COMMAND_NAMES)
        self.extra_env = dict(env or {})
        self.log_dir = os.path.abspath(os.path.join(self.root, log_dir))
        os.makedirs(self.log_dir, exist_ok=True)
        import threading

        self._cancel_event = threading.Event()
        self.last_pid: int | None = None
        self.recent_pid: int | None = None

    # -- 路径 ---------------------------------------------------------------
    def resolve(self, path: str | os.PathLike[str], *, mode: str = "read") -> Path:
        raw = str(path)
        literal = os.path.abspath(os.path.join(self.root, raw) if not os.path.isabs(raw) else raw)
        real = os.path.realpath(literal)

        if not _is_within(self.root, literal):
            raise SandboxViolation(f"路径越界：{raw} 解析为 {literal}，位于工作区之外")
        if not _is_within(self.root, real):
            raise SandboxViolation(f"软链接逃逸：{raw} 的真实落点 {real} 位于工作区之外")

        rel_literal = os.path.relpath(literal, self.root)
        rel_real = os.path.relpath(real, self.root)
        for rel in (rel_literal, rel_real):
            if rel == os.curdir:
                continue
            parts = [p for p in rel.replace("\\", "/").split("/") if p]
            if _forbidden_parts(parts):
                raise SandboxViolation(f"禁止访问敏感路径：{raw}")

        if mode == "write":
            allowed = False
            for zone in self.writable:
                zone_path = os.path.realpath(os.path.join(self.root, zone))
                if _is_within(zone_path, real):
                    allowed = True
                    break
            if not allowed:
                raise SandboxViolation(f"只读区不可写：{raw}（可写区：{', '.join(self.writable)}）")
        elif mode != "read":
            raise ValueError(f"未知模式：{mode}")

        return Path(real)

    # -- 命令 ---------------------------------------------------------------
    def check_command(self, argv: Sequence[str]) -> None:
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise SandboxViolation("命令必须是非空字符串序列")
        joined = " ".join(argv)
        for meta in SHELL_METACHARS:
            if meta in joined:
                raise SandboxViolation(f"命令包含禁止的 shell 元字符：{meta!r}")
        raw_name = os.path.basename(argv[0]).lower()
        name = raw_name[:-4] if raw_name.endswith(".exe") else raw_name
        if name not in self.allowed_commands:
            raise SandboxViolation(f"命令不在白名单：{name}")
        if name == "git":
            sub = argv[1] if len(argv) > 1 else ""
            if sub not in GIT_READONLY_SUBCOMMANDS:
                raise SandboxViolation(f"git 只允许只读子命令，实际：{sub!r}")

    # -- 执行 ---------------------------------------------------------------
    def request_cancel(self) -> None:
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        self._cancel_event.clear()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def _build_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in MINIMAL_ENV_KEYS if k in os.environ}
        env.update(self.extra_env)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        cancel_event: Any = None,
    ) -> CommandResult:
        argv = [str(a) for a in argv]
        self.check_command(argv)
        work_dir = self.resolve(cwd or self.root, mode="read")
        timeout = self.timeout if timeout is None else float(timeout)
        event = cancel_event or self._cancel_event

        os.makedirs(self.log_dir, exist_ok=True)
        log_path = os.path.join(self.log_dir, f"{ulid()}.log")
        job: _WinJob | None = None
        flags = 0
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True

        start = time.monotonic()
        timed_out = cancelled = False
        pid: int | None = None
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(work_dir),
                    env=self._build_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **kwargs,
                )
                pid = proc.pid
                self.last_pid = pid
                self.recent_pid = pid
                if os.name == "nt":
                    try:
                        job = _WinJob()
                        if not job.assign(pid):
                            job.close()
                            job = None
                    except Exception:
                        job = None

                while True:
                    if proc.poll() is not None:
                        break
                    if event is not None and event.is_set():
                        cancelled = True
                        break
                    if time.monotonic() - start > timeout:
                        timed_out = True
                        break
                    time.sleep(POLL_SECONDS)

                terminated = False
                if timed_out or cancelled:
                    kill_tree(pid, job)
                    terminated = True
                try:
                    returncode = proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    kill_tree(pid, job)
                    terminated = True
                    returncode = -9
                if job is not None:
                    job.close()
                    job = None
                duration = time.monotonic() - start
        finally:
            # 进程结束后立即清空活跃 PID：非运行态的 cancel() 不会拿旧 PID 误杀无关进程。
            self.last_pid = None

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            text = ""
        tail = text[-TAIL_CHARS:] if len(text) > TAIL_CHARS else text
        return CommandResult(
            argv=argv,
            returncode=int(returncode if returncode is not None else -1),
            stdout=tail,
            stderr="",
            duration=duration,
            timed_out=timed_out,
            cancelled=cancelled,
            terminated=terminated,
            pid=pid,
            log_path=log_path,
            output_tail=tail,
        )

    # -- 指纹 ---------------------------------------------------------------
    def fingerprint(self) -> str:
        return workspace_fingerprint(self.root)
