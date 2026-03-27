import json
import unittest

from core.live_ui import LiveDashboard, _DASHBOARD_HTML


class LiveDashboardTests(unittest.TestCase):
    def test_snapshot_tracks_run_progress(self):
        dashboard = LiveDashboard()
        dashboard.publish("start", {"goal": "ship release", "trace_id": "abc123", "scope": "demo"})
        dashboard.publish("memory", {"episodes": 2, "demonstrations": 1})
        dashboard.publish("plan", {
            "tasks": ["Inspect repo", "Write summary"],
            "graph": [
                {"id": "t1", "depends_on": [], "parallel": False},
                {"id": "t2", "depends_on": ["t1"], "parallel": False},
            ],
        })
        dashboard.publish("executing", {"task": "Inspect repo", "task_id": "t1"})
        dashboard.publish("task_done", {"id": "t1", "task": "Inspect repo", "outcome": "success"})
        dashboard.publish("done", {"outcome": "success", "metrics": {"tool_calls": 2, "llm_calls": 3}})

        snapshot = dashboard.snapshot()
        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(snapshot["goal"], "ship release")
        self.assertEqual(snapshot["memory"]["episodes"], 2)
        self.assertEqual(snapshot["tasks"][0]["status"], "completed")
        self.assertEqual(snapshot["metrics"]["tool_calls"], 2)
        self.assertTrue(snapshot["history"])

    def test_dashboard_template_and_snapshot_are_json_safe(self):
        dashboard = LiveDashboard()
        dashboard.publish("start", {"goal": "observe run", "trace_id": "trace123", "scope": "scope123"})
        snapshot_raw = json.dumps(dashboard.snapshot())
        snapshot = json.loads(snapshot_raw)
        self.assertIn("PHANTOM Live Run", _DASHBOARD_HTML)
        self.assertEqual(snapshot["goal"], "observe run")
        self.assertEqual(snapshot["trace_id"], "trace123")


if __name__ == "__main__":
    unittest.main()
