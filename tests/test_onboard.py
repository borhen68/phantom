import tempfile
import unittest
from pathlib import Path

from core.onboard import OnboardConfig, onboard_env_text, provider_env_lines, write_onboard_env


class OnboardTests(unittest.TestCase):
    def test_provider_env_lines_for_groq_include_base_url(self):
        lines = provider_env_lines("groq")
        self.assertTrue(any("GROQ_API_KEY" in line for line in lines))
        self.assertTrue(any("PHANTOM_OPENAI_BASE_URL" in line for line in lines))

    def test_onboard_env_text_contains_core_runtime_settings(self):
        config = OnboardConfig(
            workspace="/tmp/demo",
            provider="openai",
            confirm_plan=True,
            messaging_policy="pairing",
        )
        text = onboard_env_text(config)
        self.assertIn('export PHANTOM_WORKSPACE="/tmp/demo"', text)
        self.assertIn("export PHANTOM_CONFIRM_PLAN=1", text)
        self.assertIn("export PHANTOM_MESSAGING_DM_POLICY=pairing", text)
        self.assertIn("export OPENAI_API_KEY=\"replace_me\"", text)

    def test_write_onboard_env_persists_file(self):
        config = OnboardConfig(
            workspace="/tmp/demo",
            provider="",
            confirm_plan=False,
            messaging_policy="closed",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            path = write_onboard_env(Path(tempdir) / ".phantom.env", config)
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8")
            self.assertIn("export PHANTOM_CONFIRM_PLAN=0", content)
            self.assertIn("export PHANTOM_MESSAGING_DM_POLICY=closed", content)


if __name__ == "__main__":
    unittest.main()
