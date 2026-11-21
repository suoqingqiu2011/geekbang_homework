"""End-to-end login session tests for login_demo, standard library only.

Location: ``tests/auth/test_session.py`` (run from the ``pi-ltm`` root).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def find_plugin_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "ltm_core").is_dir() and (candidate / "package.json").is_file():
            return candidate
    return start


PI_LTM_ROOT = find_plugin_root(Path(__file__).resolve().parent)
LOGIN_DEMO_ROOT = Path(os.environ.get("LOGIN_DEMO_ROOT", str(PI_LTM_ROOT.parent / "login_demo")))
BASE_URL = "http://127.0.0.1:3001"
ARTIFACTS = PI_LTM_ROOT / "tests" / ".artifacts"
SERVER_LOG = ARTIFACTS / "server.log"
SERVER: subprocess.Popen | None = None
LOG_HANDLE = None
NPM = shutil.which("npm") or ("npm.cmd" if os.name == "nt" else "npm")
NODE = shutil.which("node") or "node"


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def request(method: str, path: str, body: dict | None = None, token: str | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def setUpModule() -> None:
    global SERVER, LOG_HANDLE
    if port_open("127.0.0.1", 3001):
        raise unittest.SkipTest("port 3001 is already in use; refusing to touch an unknown process")

    dist = LOGIN_DEMO_ROOT / "dist" / "server.js"
    if not dist.is_file():
        subprocess.run([NPM, "run", "build"], cwd=LOGIN_DEMO_ROOT, check=True, timeout=300, shell=False)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    log_handle = open(SERVER_LOG, "w", encoding="utf-8")
    LOG_HANDLE = log_handle
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    SERVER = subprocess.Popen(
        [NODE, str(dist)],
        cwd=LOGIN_DEMO_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        shell=False,
        **options,
    )

    deadline = time.time() + 25
    while time.time() < deadline:
        if SERVER.poll() is not None:
            raise RuntimeError(f"login_demo exited early, see {SERVER_LOG}")
        try:
            status, body = request("GET", "/health")
            if status == 200 and body.get("ok") is True:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("login_demo did not become ready within 25s")


def tearDownModule() -> None:
    global SERVER, LOG_HANDLE
    if LOG_HANDLE is not None:
        try:
            LOG_HANDLE.close()
        except OSError:
            pass
        LOG_HANDLE = None
    if SERVER is None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(SERVER.pid), signal.SIGTERM)
        else:
            SERVER.terminate()
        SERVER.wait(timeout=5)
    except Exception:
        try:
            SERVER.kill()
            SERVER.wait(timeout=5)
        except Exception:
            pass
    finally:
        SERVER = None


class LoginSessionTest(unittest.TestCase):
    def test_login_success_returns_token(self) -> None:
        status, body = request("POST", "/api/login", {"email": "demo@example.com", "password": "demo123"})
        self.assertEqual(status, 200)
        self.assertTrue(body["accessToken"].startswith("demo-token."))
        self.assertEqual(body["user"]["id"], "u_1001")
        self.assertEqual(body["user"]["email"], "demo@example.com")
        self.assertEqual(body["user"]["role"], "member")

    def test_login_wrong_password_rejected(self) -> None:
        status, body = request("POST", "/api/login", {"email": "demo@example.com", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "Invalid password")

    def test_login_missing_fields_rejected(self) -> None:
        status, body = request("POST", "/api/login", {"email": "demo@example.com"})
        self.assertEqual(status, 400)
        self.assertIn("required", body["error"])

    def test_profile_without_token_rejected(self) -> None:
        status, body = request("GET", "/api/profile")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "Missing bearer token")

    def test_profile_with_token_succeeds(self) -> None:
        status, login = request("POST", "/api/login", {"email": "admin@example.com", "password": "admin123"})
        self.assertEqual(status, 200)
        status, profile = request("GET", "/api/profile", token=login["accessToken"])
        self.assertEqual(status, 200)
        self.assertEqual(profile["currentUser"]["userId"], "u_1002")
        self.assertEqual(profile["currentUser"]["role"], "admin")


if __name__ == "__main__":
    unittest.main()
