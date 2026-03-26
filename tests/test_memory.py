import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "PHANTOM_HOME": self.tempdir.name,
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_SCOPE": "tests::memory",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        memory.init()

    def test_recall_handles_tied_scores(self):
        memory.save_episode("build server", "success", "implemented server", [])
        memory.save_episode("ship server", "success", "documented server", [])

        recalled = memory.recall("server")

        self.assertEqual(len(recalled), 2)
        self.assertEqual({episode["goal"] for episode in recalled}, {"build server", "ship server"})

    def test_save_skill_writes_to_disk(self):
        memory.save_skill("parse_csv", "Parse CSV rows", "def run(inputs):\n    return 'ok'\n")

        skill_root = Path(self.tempdir.name) / "skills"
        skill_path = next(skill_root.rglob("parse_csv.py"))
        self.assertTrue(skill_path.exists())

    def test_recent_runs_returns_persisted_metrics(self):
        memory.save_run("build api", "done", {
            "outcome": "success",
            "duration_ms": 123,
            "tasks_planned": 3,
            "tasks_completed": 3,
            "tool_calls": 2,
            "tool_errors": 0,
            "critic_blocks": 1,
            "planner_fallback": False,
            "parallel": True,
        })

        runs = memory.recent_runs()

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["goal"], "build api")
        self.assertEqual(runs[0]["duration_ms"], 123)

    def test_conflicting_world_facts_increment_versions(self):
        memory.learn("project_dir", "/tmp/a")
        memory.learn("project_dir", "/tmp/b")

        facts = memory.recent_world_facts(limit=1)

        self.assertEqual(facts[0]["version"], 2)
        self.assertEqual(facts[0]["conflicts"], 1)

    def test_skill_versions_can_roll_back(self):
        memory.save_skill("parse_csv", "first", "def run(inputs):\n    return 'v1'\n")
        memory.save_skill("parse_csv", "second", "def run(inputs):\n    return 'v2'\n")

        versions = memory.list_skill_versions("parse_csv")
        self.assertEqual([item["version"] for item in versions[:2]], [2, 1])
        self.assertTrue(memory.rollback_skill("parse_csv", 1))
        self.assertIn("v1", memory.get_skill_code("parse_csv"))

    def test_world_model_is_isolated_by_scope(self):
        memory.learn("project_dir", "/tmp/scope-a")
        self.assertEqual(memory.know("project_dir"), "/tmp/scope-a")

        with mock.patch.dict(os.environ, {"PHANTOM_SCOPE": "tests::memory-other"}, clear=False):
            memory.init()
            self.assertIsNone(memory.know("project_dir"))
            memory.learn("project_dir", "/tmp/scope-b")
            self.assertEqual(memory.know("project_dir"), "/tmp/scope-b")

        self.assertEqual(memory.know("project_dir"), "/tmp/scope-a")

    def test_demonstration_copies_screenshots_and_recalls_steps(self):
        screenshot = Path(self.workspace.name) / "demo-shot.png"
        screenshot.write_text("fake image bytes", encoding="utf-8")

        saved = memory.save_demonstration(
            goal="deploy release to dashboard",
            summary="Human showed the release flow",
            steps=[
                "Open dashboard settings",
                {"action": "shell", "inputs": {"cmd": "echo deploy"}, "instructions": "Run deploy helper", "expected": "deployment queued"},
                {"action": "browser_click", "inputs": {"selector": "#deploy"}, "instructions": "Click Deploy", "executable": True},
            ],
            screenshots=[f"{screenshot}::Deploy button is top right"],
            app="admin_console",
            tags=["release", "dashboard"],
        )

        recalled = memory.recall_demonstrations("deploy dashboard release")
        context = memory.demonstration_context("deploy dashboard release", demonstrations=recalled)

        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["id"], saved["id"])
        self.assertIn("Run deploy helper", context)
        self.assertGreater(recalled[0]["confidence"], 0.0)
        self.assertIn("release", recalled[0]["tags"])
        self.assertEqual(recalled[0]["steps"][2]["action"], "browser_click")
        self.assertEqual(recalled[0]["steps"][2]["inputs"]["selector"], "#deploy")
        self.assertEqual(len(saved["screenshots"]), 1)
        self.assertTrue(Path(saved["screenshots"][0]["path"]).exists())
        self.assertEqual(saved["screenshots"][0]["caption"], "Deploy button is top right")

    def test_correct_demonstration_creates_successor_record(self):
        screenshot = Path(self.workspace.name) / "demo-shot.png"
        screenshot.write_text("fake image bytes", encoding="utf-8")
        original = memory.save_demonstration(
            goal="deploy release",
            summary="first pass",
            steps=["Open settings"],
            screenshots=[str(screenshot)],
            tags=["release"],
        )

        corrected = memory.correct_demonstration(
            original["id"],
            summary="corrected path",
            steps=[{"action": "read_file", "inputs": {"path": "README.md"}, "instructions": "Check release notes", "executable": True}],
        )

        self.assertNotEqual(corrected["id"], original["id"])
        self.assertEqual(corrected["correction_of"], original["id"])
        self.assertEqual(corrected["summary"], "corrected path")
        self.assertEqual(corrected["steps"][0]["action"], "read_file")
        self.assertEqual(len(corrected["screenshots"]), 1)

    def test_demonstrations_are_isolated_by_scope(self):
        memory.save_demonstration(
            goal="publish release",
            summary="scope a",
            steps=["Click publish"],
        )
        self.assertEqual(len(memory.recall_demonstrations("publish release")), 1)

        with mock.patch.dict(os.environ, {"PHANTOM_SCOPE": "tests::memory-demo-other"}, clear=False):
            memory.init()
            self.assertEqual(memory.recall_demonstrations("publish release"), [])

    def test_recall_prefers_more_reliable_demonstration(self):
        trusted = memory.save_demonstration(
            goal="deploy dashboard release",
            summary="trusted path",
            steps=["Open releases", "Click deploy"],
            tags=["release", "dashboard"],
        )
        flaky = memory.save_demonstration(
            goal="deploy dashboard release",
            summary="older flaky path",
            steps=["Open settings", "Click deploy"],
            tags=["release", "dashboard"],
        )

        memory.record_demonstration_feedback(trusted["id"], success=True, note="worked")
        memory.record_demonstration_feedback(trusted["id"], success=True, note="worked again")
        memory.record_demonstration_feedback(flaky["id"], success=False, note="selector drift")

        recalled = memory.recall_demonstrations("deploy dashboard release")

        self.assertEqual(recalled[0]["id"], trusted["id"])
        self.assertGreater(recalled[0]["reliability"], recalled[1]["reliability"])
        self.assertEqual(recalled[0]["last_replay_status"], "success")
        self.assertEqual(recalled[1]["last_replay_status"], "failure")

    def test_init_runs_schema_migrations(self):
        memory.init()

        with sqlite3.connect(memory.db_path()) as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='msg_dedupe'"
            ).fetchone()

        self.assertGreaterEqual(version, 4)
        self.assertEqual(table[0], "msg_dedupe")


if __name__ == "__main__":
    unittest.main()
