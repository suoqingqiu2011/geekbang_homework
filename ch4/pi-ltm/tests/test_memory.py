"""Unit tests for the pi-okf-memory core, standard library only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from ltm_core import memory

OKF_CLI = PLUGIN_ROOT / "node_modules" / "pi-okf" / "dist" / "cli.js"


def okf_ready() -> bool:
    return OKF_CLI.is_file() and shutil.which("node") is not None


@unittest.skipUnless(okf_ready(), "pi-okf CLI not installed; run npm install in pi-ltm")
class MemoryCoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pi-ltm-test-"))
        os.environ["PI_LTM_ALLOWED_ROOTS"] = str(cls.tmp)
        cls.config = memory.load_config(PLUGIN_ROOT, str(cls.tmp))

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("PI_LTM_ALLOWED_ROOTS", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        for child in self.tmp.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _write(self, **overrides) -> dict:
        args = {
            "key": "session-test-entry",
            "content": "- path: tests/test_session.py\n- command: python -m unittest tests.test_session -v",
            "source": "tests/test_session.py",
            "title": "会话测试入口",
        }
        args.update(overrides)
        return memory.write(PLUGIN_ROOT, str(self.tmp), args, self.config)

    def _source_file(self, text: str = "v1") -> Path:
        path = self.tmp / "docs" / "note.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_write_creates_valid_okf_concept(self) -> None:
        result = self._write()
        self.assertTrue(result["ok"], result)
        path = Path(result["data"]["path"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('type: "Memory"', text)
        self.assertIn("revision: 1", text)
        self.assertIn("# Citations", text)

    def test_write_syncs_index_and_log(self) -> None:
        result = self._write()
        self.assertTrue(result["ok"], result)
        bundle = self.tmp / ".pi" / "ltm" / "okf"
        index = (bundle / "index.md").read_text(encoding="utf-8")
        log = (bundle / "log.md").read_text(encoding="utf-8")
        self.assertIn("session-test-entry", index)
        self.assertIn("session-test-entry", log)

    def test_log_sync_failure_surfaces_warning(self) -> None:
        with mock.patch.object(
            memory.okf_cli,
            "capture",
            side_effect=memory.okf_cli.OkfCliError("okf capture failed rc=1: boom"),
        ):
            result = self._write()
        self.assertTrue(result["ok"], result)
        self.assertIn("log.md sync failed", result["data"]["warnings"][0])
        self.assertIn("boom", result["data"]["warnings"][0])

    def test_capture_raises_on_nonzero_exit(self) -> None:
        with mock.patch.object(memory.okf_cli, "_run", return_value=(1, "", "boom")):
            with self.assertRaises(memory.okf_cli.OkfCliError):
                memory.okf_cli.capture(PLUGIN_ROOT, self.tmp, self.config, "x")

    def test_read_by_key_and_query(self) -> None:
        self._write()
        by_key = memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)
        self.assertTrue(by_key["ok"], by_key)
        self.assertEqual(by_key["data"]["items"][0]["key"], "session-test-entry")
        self.assertTrue(by_key["data"]["items"][0]["exists"])
        by_query = memory.read(PLUGIN_ROOT, str(self.tmp), {"query": "unittest"}, self.config)
        self.assertTrue(by_query["ok"], by_query)
        missing = memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "absent"}, self.config)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "NOT_FOUND")

    def test_write_duplicate_key_conflicts(self) -> None:
        self._write()
        second = self._write()
        self.assertFalse(second["ok"])
        self.assertEqual(second["code"], "CONFLICT")
        self.assertTrue(second["data"]["id"].startswith("mem_"))

    def test_write_rejects_out_of_range_citation(self) -> None:
        result = self._write(content="see [2] for details")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CITATION_MISMATCH")
        self.assertFalse((self.tmp / ".pi").exists())

    def test_write_rejects_embedded_citations_section(self) -> None:
        result = self._write(content="x\n\n# Citations\n\n[1] fake")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CITATION_MISMATCH")

    def test_write_accepts_in_range_citation(self) -> None:
        result = self._write(content="see [1] for details")
        self.assertTrue(result["ok"], result)

    def test_update_rejects_out_of_range_citation(self) -> None:
        created = self._write()
        self.assertTrue(created["ok"], created)
        updated = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "content": "see [2]"},
            self.config,
        )
        self.assertFalse(updated["ok"])
        self.assertEqual(updated["code"], "CITATION_MISMATCH")
        reloaded = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )
        self.assertNotIn("[2]", reloaded["data"]["items"][0]["content"])

    def test_write_rejects_credentials(self) -> None:
        result = self._write(content="api_key = sk-abcdef1234567890abcdef")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SENSITIVE_CONTENT")
        self.assertTrue(result["data"]["reasons"])
        self.assertFalse((self.tmp / ".pi").exists())

    def test_write_rejects_private_key(self) -> None:
        result = self._write(content="-----BEGIN RSA PRIVATE KEY-----\nMIIEexample")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SENSITIVE_CONTENT")
        self.assertIn("private key block", result["data"]["reasons"])

    def test_write_rejects_credentials_in_source(self) -> None:
        result = self._write(source="https://example.com/callback?token=abcdefgh12345678")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SENSITIVE_CONTENT")

    def test_write_accepts_security_documentation(self) -> None:
        result = self._write(
            content="- rule: passwords are hashed with bcrypt\n- rule: token refresh is handled by the auth service"
        )
        self.assertTrue(result["ok"], result)

    def test_update_rejects_credentials(self) -> None:
        created = self._write()
        self.assertTrue(created["ok"], created)
        updated = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "content": "token = abcdefgh12345678"},
            self.config,
        )
        self.assertFalse(updated["ok"])
        self.assertEqual(updated["code"], "SENSITIVE_CONTENT")
        reloaded = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )
        self.assertNotIn("abcdefgh12345678", reloaded["data"]["items"][0]["content"])

    def test_write_and_read_kind(self) -> None:
        result = self._write(kind="requirement")
        self.assertTrue(result["ok"], result)
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["kind"], "requirement")
        self.assertIn("kind:requirement", item["tags"])

    def test_write_rejects_invalid_kind(self) -> None:
        result = self._write(kind="guess")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_ARGS")
        self.assertFalse((self.tmp / ".pi").exists())

    def test_update_changes_kind(self) -> None:
        created = self._write(kind="fact")
        self.assertTrue(created["ok"], created)
        updated = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {
                "key": "session-test-entry",
                "kind": "inference",
                "confirm": True,
                "reason": "重新归类",
            },
            self.config,
        )
        self.assertTrue(updated["ok"], updated)
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["kind"], "inference")
        self.assertIn("kind:inference", item["tags"])
        self.assertNotIn("kind:fact", item["tags"])

    def test_source_status_tracks_changes(self) -> None:
        source = self._source_file("v1")
        created = self._write(source="docs/note.md")
        self.assertTrue(created["ok"], created)
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["source_status"], "ok")
        self.assertFalse(item["stale"])

        source.write_text("v2", encoding="utf-8")
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["source_status"], "changed")
        self.assertTrue(item["stale"])

        source.unlink()
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["source_status"], "missing")
        self.assertTrue(item["stale"])

    def test_read_can_skip_source_verification(self) -> None:
        self._source_file("v1")
        self._write(source="docs/note.md")
        item = memory.read(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "verifySources": False},
            self.config,
        )["data"]["items"][0]
        self.assertNotIn("source_status", item)

    def test_read_key_uses_index_first(self) -> None:
        self._write(key="alpha-entry", content="alpha", source="x")
        self._write(key="beta-entry", content="beta", source="x")
        cache_file = self.tmp / ".pi" / "ltm" / ".index" / "memory-index.json"
        cache_file.unlink()
        inspected = []
        original = memory._inspect

        def counting(plugin_root, file, config):
            inspected.append(file.name)
            return original(plugin_root, file, config)

        with mock.patch.object(memory, "_inspect", side_effect=counting):
            result = memory.read(
                PLUGIN_ROOT, str(self.tmp), {"key": "alpha-entry"}, self.config
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(inspected), 1)
        self.assertTrue(inspected[0].startswith("alpha-entry-"))

    def test_read_query_uses_index_first(self) -> None:
        self._write(key="alpha-entry", title="唯一标题", content="alpha", source="x")
        self._write(key="beta-entry", title="另一个标题", content="beta", source="x")
        cache_file = self.tmp / ".pi" / "ltm" / ".index" / "memory-index.json"
        cache_file.unlink()
        inspected = []
        original = memory._inspect

        def counting(plugin_root, file, config):
            inspected.append(file.name)
            return original(plugin_root, file, config)

        with mock.patch.object(memory, "_inspect", side_effect=counting):
            result = memory.read(
                PLUGIN_ROOT, str(self.tmp), {"query": "唯一标题"}, self.config
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(inspected), 1)
        self.assertEqual(result["data"]["items"][0]["key"], "alpha-entry")

    def test_update_requires_confirmation_for_changed_source(self) -> None:
        source = self._source_file("v1")
        self._write(source="docs/note.md")
        source.write_text("v2", encoding="utf-8")

        blocked = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "content": "new"},
            self.config,
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "CONFIRM_REQUIRED")
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertNotEqual(item["content"], "new")

        updated = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {
                "key": "session-test-entry",
                "content": "new",
                "confirm": True,
                "reason": "source v2",
            },
            self.config,
        )
        self.assertTrue(updated["ok"], updated)
        item = memory.read(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config
        )["data"]["items"][0]
        self.assertEqual(item["content"], "new")
        self.assertEqual(item["source_status"], "ok")
        self.assertEqual(item["supersedes"][-1]["reason"], "source v2")

    def test_expired_records_are_hidden_and_excluded_from_warm(self) -> None:
        self._write()
        expired = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {
                "key": "session-test-entry",
                "lifecycle": "expired",
                "confirm": True,
                "reason": "superseded",
            },
            self.config,
        )
        self.assertTrue(expired["ok"], expired)
        self.assertFalse(
            memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)["ok"]
        )
        included = memory.read(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "includeExpired": True},
            self.config,
        )
        self.assertTrue(included["ok"], included)
        self.assertEqual(included["data"]["items"][0]["lifecycle"], "expired")

        warmed = memory.warm(PLUGIN_ROOT, str(self.tmp), {}, self.config)
        project = warmed["data"]["scopes"]["project"]
        self.assertEqual(project["keys"], [])
        self.assertEqual(project["expiredRecords"], 1)

        restored = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {
                "key": "session-test-entry",
                "lifecycle": "active",
                "confirm": True,
                "reason": "restored",
            },
            self.config,
        )
        self.assertTrue(restored["ok"], restored)
        self.assertTrue(
            memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)["ok"]
        )

    def test_update_rejects_invalid_lifecycle(self) -> None:
        self._write()
        result = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "session-test-entry", "lifecycle": "gone"},
            self.config,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_ARGS")

    def test_update_locates_record_and_bumps_revision(self) -> None:
        created = self._write()
        self.assertTrue(created["ok"], created)
        updated = memory.update(
            PLUGIN_ROOT,
            str(self.tmp),
            {
                "key": "session-test-entry",
                "content": "- path: tests/auth/test_session.py\n- command: python -m unittest tests.auth.test_session -v",
                "source": "tests/auth/test_session.py",
                "confirm": True,
                "reason": "会话测试迁移到 tests/auth",
            },
            self.config,
        )
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["data"]["revision"], 2)
        self.assertEqual(updated["data"]["path"], created["data"]["path"])
        reloaded = memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)
        item = reloaded["data"]["items"][0]
        self.assertIn("tests/auth/test_session.py", item["content"])
        self.assertEqual(item["supersedes"][0]["revision"], 1)
        self.assertEqual(item["supersedes"][0]["reason"], "会话测试迁移到 tests/auth")

    def test_update_missing_record(self) -> None:
        result = memory.update(PLUGIN_ROOT, str(self.tmp), {"key": "absent"}, self.config)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "NOT_FOUND")

    def test_soft_delete_moves_to_trash(self) -> None:
        self._write()
        deleted = memory.delete(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(deleted["data"]["mode"], "trash")
        path = Path(deleted["data"]["path"])
        self.assertTrue(path.is_file())
        self.assertTrue(path.name.endswith(".md.orig"))
        self.assertIn("trash", path.parts)
        after = memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)
        self.assertFalse(after["ok"])

    def test_hard_delete_removes_file(self) -> None:
        created = self._write()
        self.assertTrue(created["ok"], created)
        deleted = memory.delete(
            PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry", "hard": True}, self.config
        )
        self.assertTrue(deleted["ok"], deleted)
        self.assertEqual(deleted["data"]["mode"], "hard")
        self.assertFalse(Path(created["data"]["path"]).exists())

    def test_path_traversal_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="pi-ltm-outside-"))
        try:
            result = memory.write(
                PLUGIN_ROOT,
                str(self.tmp),
                {"key": "escape", "content": "x", "source": "x", "project": str(outside)},
                self.config,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "PATH_FORBIDDEN")
            self.assertFalse((outside / ".pi").exists())
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_invalid_args_do_not_touch_store(self) -> None:
        result = memory.write(
            PLUGIN_ROOT, str(self.tmp), {"key": "", "content": "", "source": ""}, self.config
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_ARGS")
        self.assertFalse((self.tmp / ".pi").exists())

    def test_disabled_config_blocks_operations(self) -> None:
        config_file = self.tmp / ".pi" / "ltm" / "ltm.config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        config = memory.load_config(PLUGIN_ROOT, str(self.tmp))

        result = memory.write(
            PLUGIN_ROOT,
            str(self.tmp),
            {"key": "disabled-entry", "content": "x", "source": "x"},
            config,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "DISABLED")
        self.assertFalse((self.tmp / ".pi" / "ltm" / "okf").exists())

        for operation in (memory.read, memory.update, memory.delete):
            result = operation(PLUGIN_ROOT, str(self.tmp), {"key": "disabled-entry"}, config)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "DISABLED")

        status = memory.status(PLUGIN_ROOT, str(self.tmp), {}, config)
        self.assertTrue(status["ok"], status)
        self.assertFalse(status["data"]["enabled"])

    def test_warm_caps_keys_and_reports_total(self) -> None:
        config_file = self.tmp / ".pi" / "ltm" / "ltm.config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps({"session": {"maxKeysPerScope": 2}}), encoding="utf-8"
        )
        config = memory.load_config(PLUGIN_ROOT, str(self.tmp))

        for index in range(3):
            result = memory.write(
                PLUGIN_ROOT,
                str(self.tmp),
                {"key": f"warm-key-{index}", "content": f"x{index}", "source": "x"},
                config,
            )
            self.assertTrue(result["ok"], result)

        warmed = memory.warm(PLUGIN_ROOT, str(self.tmp), {}, config)
        self.assertTrue(warmed["ok"], warmed)
        project = warmed["data"]["scopes"]["project"]
        self.assertEqual(len(project["keys"]), 2)
        self.assertEqual(project["totalKeys"], 3)

    def test_cache_rebuild_and_cross_process_read(self) -> None:
        created = self._write()
        self.assertTrue(created["ok"], created)
        cache_file = self.tmp / ".pi" / "ltm" / ".index" / "memory-index.json"
        self.assertTrue(cache_file.is_file())
        cache_file.unlink()
        rebuilt = memory.read(PLUGIN_ROOT, str(self.tmp), {"key": "session-test-entry"}, self.config)
        self.assertTrue(rebuilt["ok"], rebuilt)
        self.assertTrue(cache_file.is_file())

        env = dict(os.environ)
        env["PYTHONPATH"] = str(PLUGIN_ROOT)
        env["PI_LTM_ALLOWED_ROOTS"] = str(self.tmp)
        payload = json.dumps(
            {"op": "read", "cwd": str(self.tmp), "args": {"key": "session-test-entry"}}
        )
        proc = subprocess.run(
            [sys.executable, "-m", "ltm_core.cli"],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(PLUGIN_ROOT),
            env=env,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = json.loads(proc.stdout)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["data"]["items"][0]["key"], "session-test-entry")


if __name__ == "__main__":
    unittest.main()
