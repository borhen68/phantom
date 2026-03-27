import json
import os
import tempfile
import time
import unittest
from unittest import mock

from core.doctor import doctor_report
from core.gateway import create_gateway


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.workspace.cleanup)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "PHANTOM_HOME": self.home.name,
                "PHANTOM_WORKSPACE": self.workspace.name,
                "PHANTOM_SCOPE": "tests::gateway",
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _gateway_fixture(self):
        fake_server = mock.Mock()
        fake_server.server_address = ("127.0.0.1", 18789)
        fake_thread = mock.Mock()
        fake_thread.start.return_value = None
        fake_thread.join.return_value = None
        return fake_server, fake_thread

    class _ImmediateExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)
            return mock.Mock()

        def shutdown(self, wait=False, cancel_futures=False):
            return None

    def test_doctor_report_returns_checks(self):
        report = doctor_report()

        self.assertIn(report["status"], {"pass", "warn", "fail"})
        self.assertTrue(any(item["name"] == "workspace" for item in report["checks"]))
        self.assertTrue(any(item["name"] == "extensions" for item in report["checks"]))
        self.assertTrue(any(item["name"] == "skill-compat" for item in report["checks"]))

    def test_doctor_warns_when_messaging_is_open(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "PHANTOM_MESSAGING_DM_POLICY": "open",
            },
            clear=False,
        ):
            report = doctor_report()
        messaging = next(item for item in report["checks"] if item["name"] == "messaging")
        self.assertEqual(messaging["status"], "warn")
        self.assertIn("open", messaging["detail"])

    def test_gateway_accepts_session_and_tracks_completion(self):
        def fake_run(goal: str, on_event=None, parallel=True):
            if on_event:
                on_event("start", {"goal": goal, "scope": "gateway::demo", "trace_id": "trace123"})
                on_event("done", {"outcome": "success", "summary": "review complete", "metrics": {"llm_calls": 1}})
            return {"outcome": "success", "summary": "review complete", "metrics": {"llm_calls": 1}}

        fake_server, fake_thread = self._gateway_fixture()
        with mock.patch("core.orchestrator.run", side_effect=fake_run), \
             mock.patch("core.gateway.ThreadingHTTPServer", return_value=fake_server), \
             mock.patch("core.gateway.threading.Thread", return_value=fake_thread):
            gateway = create_gateway(host="127.0.0.1", port=0, max_workers=1)
            self.addCleanup(gateway.stop)
            original_executor = gateway._executor
            gateway._executor = self._ImmediateExecutor()
            original_executor.shutdown(wait=False, cancel_futures=True)

            created = gateway.submit(
                goal="review this repository and explain the architecture",
                workspace=self.workspace.name,
                scope="gateway::demo",
                parallel=False,
            )
            session_id = created.session_id

            snapshot = gateway.get_session(session_id).snapshot()

            self.assertEqual(snapshot["status"], "success")
            self.assertEqual(snapshot["outcome"], "success")
            self.assertEqual(snapshot["summary"], "review complete")
            self.assertEqual(snapshot["metrics"]["llm_calls"], 1)
            self.assertEqual(gateway.address, ("127.0.0.1", 18789))


if __name__ == "__main__":
    unittest.main()
