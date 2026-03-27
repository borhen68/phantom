import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from core.contracts import ToolExecutionResult, ToolExecutionStatus
from core.loop import run_agent_result
from tools import dispatch_structured


class StructuredResultsTests(unittest.TestCase):
    class _FakeHTTPResponse:
        def __init__(self, payload: str, content_type: str = "application/json; charset=utf-8"):
            self._payload = payload.encode("utf-8")
            self.headers = {"Content-Type": content_type}

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.workspace.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "PHANTOM_HOME": self.home.name,
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_SCOPE": "tests::structured",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_dispatch_structured_returns_artifacts_for_write_file(self):
        result = dispatch_structured("write_file", {"path": "notes.txt", "content": "hello"})

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "write_file")
        self.assertEqual(result.artifacts[0].path, "notes.txt")
        self.assertIn("Wrote", result.summary)

    def test_run_agent_result_collects_tool_results(self):
        responses = [
            SimpleNamespace(
                content=[
                    SimpleNamespace(type="tool_use", name="read_file", input={"path": "README.md"}, id="tool-1"),
                ],
                stop_reason="tool_use",
            ),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="done")],
                stop_reason="end_turn",
            ),
        ]

        class FakeClient:
            def __init__(self, queued):
                self.queued = list(queued)

            def create_messages(self, **kwargs):
                return self.queued.pop(0)

        fake_client = FakeClient(responses)
        tool_result = ToolExecutionResult(
            name="read_file",
            status=ToolExecutionStatus.SUCCESS,
            ok=True,
            summary="Read README.md",
            output="Project overview",
        )

        with mock.patch("core.loop.client", return_value=fake_client), \
             mock.patch("core.loop.usage_from_response", return_value=SimpleNamespace(input_tokens=0, output_tokens=0)), \
             mock.patch("core.loop.dispatch_structured", return_value=tool_result), \
             mock.patch("core.loop.mem.record_tool"):
            result = run_agent_result(
                role="executor",
                model="test-model",
                system="test system",
                messages=[{"role": "user", "content": "Read the project"}],
                tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
                max_steps=3,
            )

        self.assertEqual(result.final_text, "done")
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(result.tool_results[0].name, "read_file")

    def test_dispatch_structured_supports_chief_of_staff_tools(self):
        person = dispatch_structured("remember_person", {
            "name": "Nadia",
            "relationship": "manager",
            "notes": "Approves launches",
        })
        project = dispatch_structured("remember_project", {
            "name": "Launch",
            "status": "active",
            "notes": "Public release",
        })
        commitment = dispatch_structured("remember_commitment", {
            "title": "Send launch summary",
            "counterparty": "Nadia",
            "project": "Launch",
            "due_at": "Friday",
        })
        briefing = dispatch_structured("chief_of_staff_briefing", {"query": "launch summary for Nadia"})

        self.assertTrue(person.ok)
        self.assertTrue(project.ok)
        self.assertTrue(commitment.ok)
        self.assertTrue(briefing.ok)
        self.assertIn("Commitments", briefing.output)

    def test_dispatch_structured_supports_signal_ingestion_tools(self):
        ingested = dispatch_structured("ingest_signal", {
            "kind": "message",
            "source": "telegram",
            "title": "Nadia follow-up",
            "content": "We will send the launch summary before Friday.",
            "metadata": {
                "people": [{"name": "Nadia", "relationship": "manager"}],
                "project": {"name": "Launch", "status": "active"},
                "counterparty": "Nadia",
                "due_at": "Friday",
            },
        })
        listed = dispatch_structured("list_signals", {"kind": "message"})

        self.assertTrue(ingested.ok)
        self.assertTrue(listed.ok)
        self.assertIn("Saved signal", ingested.output)
        self.assertIn("Signals:", listed.output)

    def test_dispatch_structured_supports_github_cli_tool(self):
        completed = mock.Mock(returncode=0, stdout="Logged in to GitHub\n", stderr="")
        with mock.patch("tools.shutil.which", return_value="/usr/bin/gh"), \
             mock.patch("tools.subprocess.run", return_value=completed):
            result = dispatch_structured("github_cli", {"action": "auth_status"})

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "github_cli")
        self.assertEqual(result.data["action"], "auth_status")
        self.assertEqual(result.artifacts[0].kind, "github")

    def test_dispatch_structured_supports_tmux_session_tool(self):
        completed = mock.Mock(returncode=0, stdout="shared: 1 windows\nworker: 1 windows\n", stderr="")
        with mock.patch("tools.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("tools.subprocess.run", return_value=completed):
            result = dispatch_structured("tmux_session", {"action": "list_sessions"})

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "tmux_session")
        self.assertEqual(result.data["action"], "list_sessions")
        self.assertEqual(result.artifacts[0].kind, "tmux")

    def test_dispatch_structured_supports_browser_session_tool(self):
        create = dispatch_structured("browser_session", {
            "action": "create",
            "session_id": "dashboard-session",
            "browser": "chromium",
            "headless": True,
        })
        attach = dispatch_structured("browser_session", {
            "action": "attach",
            "session_id": "dashboard-session",
            "attach_endpoint": "http://127.0.0.1:9222",
        })
        listed = dispatch_structured("browser_session", {"action": "list"})
        inspected = dispatch_structured("browser_session", {"action": "inspect", "session_id": "dashboard-session"})

        self.assertTrue(create.ok)
        self.assertTrue(attach.ok)
        self.assertTrue(listed.ok)
        self.assertTrue(inspected.ok)
        self.assertEqual(create.name, "browser_session")
        self.assertEqual(create.artifacts[0].kind, "browser_session")
        self.assertIn("dashboard-session", listed.output)
        self.assertIn("9222", inspected.output)

    def test_dispatch_structured_supports_browser_workflow_session_flags(self):
        payload = {
            "ok": True,
            "final_url": "https://example.com/dashboard",
            "title": "Dashboard",
            "screenshots": [],
            "step_results": [],
            "session_verification": "matched previous visual state",
        }
        with mock.patch("tools._execute_browser_workflow", return_value=(payload, "Browser workflow complete.", False)):
            result = dispatch_structured("browser_workflow", {
                "steps": [{"action": "extract_text", "selector": "h1"}],
                "session_id": "dashboard-session",
                "resume_session": True,
                "resume_last_page": True,
                "persist_session": True,
                "verify_resumed_state": True,
                "auto_reanchor": True,
                "attach_endpoint": "http://127.0.0.1:9222",
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "browser_workflow")
        self.assertTrue(result.data["resume_session"])
        self.assertTrue(result.data["verify_resumed_state"])
        self.assertTrue(result.data["auto_reanchor"])
        self.assertEqual(result.data["attach_endpoint"], "http://127.0.0.1:9222")

    def test_dispatch_structured_supports_slack_channel_tool(self):
        response = self._FakeHTTPResponse('{"ok": true, "channel": "C123", "ts": "1712023032.1234"}')
        with mock.patch.dict(os.environ, {"PHANTOM_SLACK_BOT_TOKEN": "token"}, clear=False), \
             mock.patch("tools.urllib.request.urlopen", return_value=response):
            result = dispatch_structured("slack_channel", {
                "action": "send_message",
                "to": "channel:C123",
                "content": "Hello from PHANTOM",
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "slack_channel")
        self.assertEqual(result.data["action"], "send_message")
        self.assertEqual(result.artifacts[0].kind, "slack")

    def test_dispatch_structured_supports_discord_channel_tool(self):
        response = self._FakeHTTPResponse('{"id": "987", "channel_id": "123", "content": "Hello from PHANTOM"}')
        with mock.patch.dict(os.environ, {"PHANTOM_DISCORD_BOT_TOKEN": "token"}, clear=False), \
             mock.patch("tools.urllib.request.urlopen", return_value=response):
            result = dispatch_structured("discord_channel", {
                "action": "send",
                "to": "channel:123",
                "message": "Hello from PHANTOM",
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "discord_channel")
        self.assertEqual(result.data["action"], "send")
        self.assertEqual(result.artifacts[0].kind, "discord")


if __name__ == "__main__":
    unittest.main()
