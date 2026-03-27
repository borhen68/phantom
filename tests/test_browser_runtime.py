import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.browser_runtime import get_browser_session, list_browser_sessions, run_browser_workflow, summarize_browser_result


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        self.page.events.append(f"keyboard:{key}")


class _FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def click(self, timeout=None):
        self.page.events.append(f"click:{self.selector}")

    def fill(self, value, timeout=None):
        self.page.filled[self.selector] = value
        self.page.events.append(f"fill:{self.selector}={value}")

    def press(self, key, timeout=None):
        self.page.events.append(f"press:{self.selector}={key}")

    def wait_for(self, state="visible", timeout=None):
        if self.selector == "#missing":
            raise RuntimeError("selector missing")
        self.page.events.append(f"wait:{self.selector}:{state}")

    def inner_text(self, timeout=None):
        return self.page.text_by_selector.get(self.selector, self.page.body_text if self.selector == "body" else "")


class _FakePage:
    def __init__(self):
        self.url = ""
        self._title = "Untitled"
        self.keyboard = _FakeKeyboard(self)
        self.text_by_selector = {"h1": "Welcome back", "#status": "green"}
        self.body_text = "Dashboard home status green"
        self.events = []
        self.filled = {}

    def goto(self, url, wait_until="load", timeout=None):
        self.url = url
        self._title = "Example"
        self.events.append(f"goto:{url}:{wait_until}")

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def wait_for_timeout(self, ms):
        self.events.append(f"sleep:{ms}")

    def screenshot(self, path, full_page=True):
        Path(path).write_text("fake screenshot", encoding="utf-8")
        self.events.append(f"screenshot:{Path(path).name}:{full_page}")

    def title(self):
        return self._title


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.storage_states = []
        self.pages = [self.page]

    def new_page(self):
        self.pages = [self.page]
        return self.page

    def storage_state(self, path=None):
        payload = {"cookies": [{"name": "session", "value": "ok"}]}
        self.storage_states.append({"path": path, "payload": payload})
        if path:
            Path(path).write_text('{"cookies":[{"name":"session","value":"ok"}]}', encoding="utf-8")
        return payload


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()

    def new_context(self, **kwargs):
        self.context.kwargs = dict(kwargs)
        return self.context

    def close(self):
        return None


class _FakeBrowserType:
    def launch(self, headless=True):
        return _FakeBrowser()

    def connect_over_cdp(self, endpoint):
        browser = _FakeBrowser()
        browser.context.page.url = "https://example.com/live"
        browser.context.page._title = "Live Session"
        browser.contexts = [browser.context]
        browser.attached_endpoint = endpoint
        return browser


class _FakePlaywright:
    chromium = _FakeBrowserType()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DriftBrowserType(_FakeBrowserType):
    def connect_over_cdp(self, endpoint):
        browser = _FakeBrowser()
        browser.context.page.url = "https://example.com/settings"
        browser.context.page._title = "Settings"
        browser.context.page.body_text = "Settings page profile security"
        browser.context.page.text_by_selector["h1"] = "Settings"
        browser.contexts = [browser.context]
        browser.attached_endpoint = endpoint
        return browser


class _DriftPlaywright(_FakePlaywright):
    chromium = _DriftBrowserType()


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.addCleanup(self.home.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_HOME": self.home.name,
            "PHANTOM_SCOPE": "tests::browser",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_run_browser_workflow_executes_and_captures_artifacts(self):
        result = run_browser_workflow(
            [
                {"action": "goto", "url": "https://example.com"},
                {"action": "fill", "selector": "#email", "value": "user@example.com"},
                {"action": "click", "selector": "#submit"},
                {"action": "extract_text", "selector": "h1", "name": "heading"},
                {"action": "assert_text", "selector": "h1", "expected": "Welcome"},
                {"action": "screenshot", "name": "after_submit"},
            ],
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertEqual(result["final_url"], "https://example.com")
        self.assertEqual(result["title"], "Example")
        self.assertTrue(result["ok"])
        self.assertEqual(result["extracted"][0]["name"], "heading")
        self.assertEqual(result["extracted"][0]["text"], "Welcome back")
        self.assertEqual(len(result["step_results"]), 6)
        self.assertTrue(all(step["ok"] for step in result["step_results"]))
        self.assertGreaterEqual(len(result["screenshots"]), 2)
        for path in result["screenshots"]:
            self.assertTrue(Path(path).exists())

        summary = summarize_browser_result(result)
        self.assertIn("Browser workflow complete.", summary)
        self.assertIn("heading=Welcome back", summary)
        self.assertIn("Verification:", summary)

    def test_run_browser_workflow_reports_drift_on_verification_failure(self):
        result = run_browser_workflow(
            [
                {"action": "goto", "url": "https://example.com"},
                {"action": "click", "selector": "#submit", "verify_selector": "#missing"},
            ],
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertFalse(result["ok"])
        self.assertIn("missing", result["error"])
        self.assertIsNotNone(result["drift_report"])
        self.assertTrue(result["drift_report"]["suspected"])
        self.assertTrue(result["drift_report"]["screenshot"])

        summary = summarize_browser_result(result)
        self.assertIn("UI drift suspected", summary)

    def test_run_browser_workflow_persists_and_resumes_session(self):
        first = run_browser_workflow(
            [
                {"action": "goto", "url": "https://example.com/dashboard"},
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="dashboard-session",
            persist_session=True,
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(first["ok"])
        self.assertEqual(first["session_id"], "dashboard-session")
        self.assertTrue(first["session_saved"])
        session = get_browser_session("dashboard-session")
        self.assertIsNotNone(session)
        self.assertEqual(session["last_url"], "https://example.com/dashboard")
        self.assertTrue(Path(session["storage_state_path"]).exists())
        self.assertTrue(list_browser_sessions())

        second = run_browser_workflow(
            [
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="dashboard-session",
            resume_session=True,
            resume_last_page=True,
            persist_session=True,
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(second["ok"])
        self.assertTrue(second["session_resumed"])
        self.assertIn("resume https://example.com/dashboard", second["steps_executed"][0])
        summary = summarize_browser_result(second)
        self.assertIn("Session: dashboard-session", summary)
        self.assertIn("resumed", summary)
        self.assertIn("Session verification:", summary)

    def test_run_browser_workflow_can_attach_to_live_browser_endpoint(self):
        result = run_browser_workflow(
            [
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="live-browser",
            attach_endpoint="http://127.0.0.1:9222",
            resume_session=True,
            resume_last_page=True,
            persist_session=True,
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["session_attached"])
        self.assertEqual(result["final_url"], "https://example.com/live")
        self.assertIn("attach https://example.com/live", result["steps_executed"][0])
        saved = get_browser_session("live-browser")
        self.assertEqual(saved["attach_endpoint"], "http://127.0.0.1:9222")
        summary = summarize_browser_result(result)
        self.assertIn("attached", summary)
        self.assertIn("Session verification:", summary)

    def test_run_browser_workflow_detects_resume_drift_before_steps(self):
        first = run_browser_workflow(
            [
                {"action": "goto", "url": "https://example.com/dashboard"},
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="drift-session",
            persist_session=True,
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(first["ok"])

        second = run_browser_workflow(
            [
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="drift-session",
            resume_session=True,
            resume_last_page=True,
            persist_session=True,
            auto_reanchor=False,
            attach_endpoint="http://127.0.0.1:9222",
            sync_playwright_factory=lambda: _DriftPlaywright(),
        )

        self.assertFalse(second["ok"])
        self.assertIn("session verification failed", second["error"])
        self.assertTrue(second["session_attached"])
        self.assertEqual(second["step_results"], [])
        self.assertIsNotNone(second["drift_report"])
        self.assertEqual(second["drift_report"]["action"], "resume_verification")
        self.assertTrue(second["screenshots"])
        summary = summarize_browser_result(second)
        self.assertIn("Session verification:", summary)
        self.assertIn("UI drift suspected", summary)
        self.assertIn("Recovery hint", summary)

    def test_run_browser_workflow_reanchors_resumed_session_to_expected_url(self):
        first = run_browser_workflow(
            [
                {"action": "goto", "url": "https://example.com/dashboard"},
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="recover-session",
            persist_session=True,
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(first["ok"])

        second = run_browser_workflow(
            [
                {"action": "extract_text", "selector": "h1", "name": "heading"},
            ],
            session_id="recover-session",
            resume_session=True,
            resume_last_page=True,
            persist_session=True,
            attach_endpoint="http://127.0.0.1:9222",
            sync_playwright_factory=lambda: _DriftPlaywright(),
        )

        self.assertTrue(second["ok"])
        self.assertIn("reanchor https://example.com/dashboard", second["steps_executed"])
        self.assertIn("recovered via https://example.com/dashboard", second["session_verification"])
        summary = summarize_browser_result(second)
        self.assertIn("Session verification:", summary)

    def test_run_browser_workflow_reanchors_selector_with_fallback(self):
        result = run_browser_workflow(
            [
                {
                    "action": "click",
                    "selector": "#missing",
                    "fallback_selectors": ["#submit"],
                    "verify_selector": "#missing",
                    "fallback_verify_selectors": ["#status"],
                },
            ],
            sync_playwright_factory=lambda: _FakePlaywright(),
        )

        self.assertTrue(result["ok"])
        self.assertIn("click #submit (re-anchored)", result["steps_executed"][0])
        self.assertIn("re-anchored", result["step_results"][0]["detail"])
        summary = summarize_browser_result(result)
        self.assertIn("re-anchored", summary)


if __name__ == "__main__":
    unittest.main()
