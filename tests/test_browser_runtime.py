import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.browser_runtime import run_browser_workflow, summarize_browser_result


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

    def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()

    def new_context(self):
        return self.context

    def close(self):
        return None


class _FakeBrowserType:
    def launch(self, headless=True):
        return _FakeBrowser()


class _FakePlaywright:
    chromium = _FakeBrowserType()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


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


if __name__ == "__main__":
    unittest.main()
