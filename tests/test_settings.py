import os
import unittest
from unittest import mock

from core.settings import override_scope, redact_text, scope_id, secret_settings
from core.router import critic_model, execution_model, max_tokens_for_role, planning_model, synthesis_model


class SettingsTests(unittest.TestCase):
    def test_redact_text_removes_openai_secret(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai-test"}, clear=False):
            redacted = redact_text("token=sk-openai-test")

        self.assertEqual(redacted, "token=[REDACTED]")

    def test_override_scope_is_thread_safe_runtime_override(self):
        with mock.patch.dict(os.environ, {"PHANTOM_SCOPE": "env-scope"}, clear=False):
            self.assertEqual(scope_id(), "env-scope")
            with override_scope("message-scope"):
                self.assertEqual(scope_id(), "message-scope")
            self.assertEqual(scope_id(), "env-scope")

    def test_redact_text_removes_groq_secret(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test-secret"}, clear=False):
            redacted = redact_text("token=gsk-test-secret")

        self.assertEqual(redacted, "token=[REDACTED]")

    def test_secret_settings_loads_groq_key(self):
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test-secret"}, clear=False):
            secrets = secret_settings()

        self.assertEqual(secrets.groq_key, "gsk-test-secret")

    def test_role_models_can_be_overridden_by_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "PHANTOM_PLANNING_MODEL": "openai/gpt-oss-20b",
                "PHANTOM_EXECUTION_MODEL": "openai/gpt-oss-120b",
                "PHANTOM_CRITIC_MODEL": "openai/gpt-oss-120b",
                "PHANTOM_SYNTHESIS_MODEL": "openai/gpt-oss-20b",
            },
            clear=False,
        ):
            self.assertEqual(planning_model(), "openai/gpt-oss-20b")
            self.assertEqual(execution_model(), "openai/gpt-oss-120b")
            self.assertEqual(critic_model(), "openai/gpt-oss-120b")
            self.assertEqual(synthesis_model(), "openai/gpt-oss-20b")

    def test_max_tokens_for_role_uses_groq_friendly_defaults(self):
        with mock.patch.dict(os.environ, {"PHANTOM_PROVIDER": "groq"}, clear=False):
            self.assertEqual(max_tokens_for_role("planner", "openai/gpt-oss-120b"), 900)
            self.assertEqual(max_tokens_for_role("synthesizer", "openai/gpt-oss-120b"), 700)

    def test_max_tokens_for_role_honors_role_override(self):
        with mock.patch.dict(os.environ, {"PHANTOM_EXECUTOR_MAX_TOKENS": "1536"}, clear=False):
            self.assertEqual(max_tokens_for_role("executor", "openai/gpt-oss-120b"), 1536)


if __name__ == "__main__":
    unittest.main()
