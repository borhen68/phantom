import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory
from tools import dispatch


class ToolSafetyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.home.cleanup)
        self.env = {
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_HOME": self.home.name,
        }
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        memory.init()

    def test_write_file_blocks_escape(self):
        result, err = dispatch("write_file", {"path": "../escape.txt", "content": "blocked"})
        self.assertTrue(err)
        self.assertIn("allowed roots", result)

    def test_shell_blocks_destructive_command(self):
        result, err = dispatch("shell", {"cmd": "git reset --hard"})
        self.assertTrue(err)
        self.assertIn("blocked", result.lower())

    def test_safe_skill_can_read_workspace_file(self):
        source = Path(self.workspace.name) / "data.txt"
        source.write_text("hello", encoding="utf-8")
        code = "def run(inputs):\n    with open(inputs['path']) as f:\n        return f.read().upper()\n"

        result, err = dispatch("create_skill", {"name": "upper_reader", "description": "Read text", "code": code})
        self.assertFalse(err, result)

        result, err = dispatch("use_skill", {"name": "upper_reader", "inputs": {"path": str(source)}})
        self.assertFalse(err, result)
        self.assertEqual(result, "HELLO")

    def test_safe_skill_allows_function_local_safe_imports(self):
        source = Path(self.workspace.name) / "data.json"
        source.write_text('{"x": 1}', encoding="utf-8")
        code = (
            "def run(inputs):\n"
            "    import json\n"
            "    with open(inputs['path']) as f:\n"
            "        return str(json.load(f)['x'])\n"
        )

        result, err = dispatch("create_skill", {"name": "json_reader", "description": "Read json", "code": code})
        self.assertFalse(err, result)
        result, err = dispatch("use_skill", {"name": "json_reader", "inputs": {"path": str(source)}})
        self.assertFalse(err, result)
        self.assertEqual(result, "1")

    def test_unsafe_skill_import_is_rejected(self):
        code = "import subprocess\n\ndef run(inputs):\n    return 'bad'\n"
        result, err = dispatch("create_skill", {"name": "bad_skill", "description": "bad", "code": code})

        self.assertTrue(err)
        self.assertIn("blocked module", result.lower())

    def test_unsafe_skill_dunder_escape_is_rejected(self):
        code = "def run(inputs):\n    return str((1).__class__.__mro__)\n"
        result, err = dispatch("create_skill", {"name": "dunder_skill", "description": "bad", "code": code})

        self.assertTrue(err)
        self.assertIn("blocked", result.lower())

    def test_helper_functions_are_rejected_by_allowlist(self):
        code = (
            "def helper(value):\n"
            "    return value\n\n"
            "def run(inputs):\n"
            "    return helper('ok')\n"
        )
        result, err = dispatch("create_skill", {"name": "helper_skill", "description": "bad", "code": code})

        self.assertTrue(err)
        self.assertIn("run(inputs)", result)

    def test_confirm_mode_can_block_writes(self):
        with mock.patch.dict(os.environ, {"PHANTOM_CONFIRM": "1"}, clear=False), \
             mock.patch("tools.prompt_user", return_value=False):
            result, err = dispatch("write_file", {"path": "approved.txt", "content": "blocked"})

        self.assertTrue(err)
        self.assertIn("checkpoint declined", result.lower())

    def test_skill_history_and_rollback_tools(self):
        dispatch("create_skill", {"name": "cycler", "description": "v1", "code": "def run(inputs):\n    return 'v1'\n"})
        dispatch("create_skill", {"name": "cycler", "description": "v2", "code": "def run(inputs):\n    return 'v2'\n"})

        history, err = dispatch("skill_history", {"name": "cycler"})
        self.assertFalse(err, history)
        self.assertIn("v2", history)

        result, err = dispatch("rollback_skill", {"name": "cycler", "version": 1})
        self.assertFalse(err, result)
        result, err = dispatch("use_skill", {"name": "cycler", "inputs": {}})
        self.assertFalse(err, result)
        self.assertEqual(result, "v1")

    def test_replay_demonstration_executes_safe_structured_steps(self):
        demo = memory.save_demonstration(
            goal="collect release notes",
            summary="safe replay",
            steps=[
                {"action": "write_file", "inputs": {"path": "notes.txt", "content": "release"}, "instructions": "Write notes", "executable": True, "risk": "high"},
                {"action": "read_file", "inputs": {"path": "notes.txt"}, "instructions": "Read notes", "executable": True},
            ],
        )

        preview, err = dispatch("replay_demonstration", {"id": demo["id"]})
        self.assertFalse(err, preview)
        self.assertIn("Dry-run replay plan", preview)

        result, err = dispatch("replay_demonstration", {"id": demo["id"], "execute": True, "allow_risky": False})
        self.assertTrue(err)
        self.assertIn("blocked high-risk step", result)

        with mock.patch("tools.prompt_user", return_value=True):
            result, err = dispatch("replay_demonstration", {"id": demo["id"], "execute": True, "allow_risky": True})
        self.assertFalse(err, result)
        self.assertIn("verified", result)
        self.assertTrue((Path(self.workspace.name) / "notes.txt").exists())

    def test_replay_demonstration_requires_human_approval_for_risky_steps(self):
        demo = memory.save_demonstration(
            goal="edit release notes",
            summary="risky replay",
            steps=[
                {"action": "write_file", "inputs": {"path": "notes.txt", "content": "release"}, "instructions": "Overwrite notes", "executable": True, "risk": "high"},
            ],
        )

        with mock.patch("tools.prompt_user", return_value=False):
            result, err = dispatch("replay_demonstration", {"id": demo["id"], "execute": True, "allow_risky": True})

        self.assertTrue(err)
        self.assertIn("declined risky replay step", result.lower())

    def test_replay_demonstration_batches_browser_steps(self):
        demo = memory.save_demonstration(
            goal="check dashboard status",
            summary="browser replay",
            steps=[
                {"action": "browser_goto", "inputs": {"url": "https://example.com"}, "instructions": "Open dashboard", "executable": True},
                {"action": "browser_click", "inputs": {"selector": "#status"}, "instructions": "Open status panel", "executable": True},
                {"action": "browser_extract_text", "inputs": {"selector": "h1", "name": "heading"}, "instructions": "Read heading", "executable": True},
            ],
        )

        mocked_result = {
            "ok": True,
            "final_url": "https://example.com/status",
            "title": "Status",
            "steps_executed": ["goto https://example.com", "click #status", "extract_text h1"],
            "extracted": [{"name": "heading", "selector": "h1", "text": "All systems operational"}],
            "screenshots": [],
            "step_results": [
                {"index": 1, "action": "goto", "ok": True, "verified": True, "detail": "url=https://example.com/status"},
                {"index": 2, "action": "click", "ok": True, "verified": True, "detail": "action completed"},
                {"index": 3, "action": "extract_text", "ok": True, "verified": True, "detail": "extracted=All systems operational"},
            ],
            "drift_report": None,
        }
        with mock.patch("tools.browser_runtime.run_browser_workflow", return_value=mocked_result) as patched:
            result, err = dispatch("replay_demonstration", {"id": demo["id"], "execute": True})

        self.assertFalse(err, result)
        self.assertEqual(patched.call_count, 1)
        self.assertIn("browser_workflow", result)
        self.assertIn("All systems operational", result)
        self.assertIn("Verification:", result)


if __name__ == "__main__":
    unittest.main()
