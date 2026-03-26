import json
import os
import sys
import types
import unittest
from unittest import mock

from core.providers import FallbackProvider, OpenAIProvider, _OpenAIResponse, retry_delay_seconds


class _FakeUsage:
    prompt_tokens = 12
    completion_tokens = 7


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, tool_id, name, arguments):
        self.id = tool_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self):
        self.usage = _FakeUsage()
        self.choices = [_FakeChoice(_FakeMessage(content="ok"))]


class _RecorderCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeCompletion()


class _RecorderChat:
    def __init__(self):
        self.completions = _RecorderCompletions()


class _RecorderClient:
    def __init__(self):
        self.chat = _RecorderChat()


class ProviderTests(unittest.TestCase):
    def test_retry_delay_uses_rate_limit_hint_when_present(self):
        exc = RuntimeError("Rate limit exceeded. Please try again in 9.24s.")
        self.assertEqual(retry_delay_seconds(exc, attempt=1, base_backoff=1.0), 9.74)

    def test_openai_provider_accepts_groq_key_and_base_url(self):
        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        with mock.patch.dict(
            os.environ,
            {
                "GROQ_API_KEY": "gsk-test-secret",
                "PHANTOM_OPENAI_BASE_URL": "https://api.groq.com/openai/v1",
            },
            clear=False,
        ), mock.patch.dict(sys.modules, {"openai": fake_module}):
            provider = OpenAIProvider()

        self.assertIsInstance(provider, OpenAIProvider)
        self.assertEqual(captured["api_key"], "gsk-test-secret")
        self.assertEqual(captured["base_url"], "https://api.groq.com/openai/v1")

    def test_openai_translation_preserves_assistant_tool_calls(self):
        provider = OpenAIProvider.__new__(OpenAIProvider)
        provider.timeout_seconds = 30
        provider.max_retries = 0
        provider.retry_backoff_seconds = 0.0
        provider.client = _RecorderClient()

        tool_use_block = _OpenAIResponse._ContentBlock(
            "tool_use",
            id="call_123",
            name="read_file",
            input={"path": "README.md"},
        )

        provider.create_messages(
            model="gpt-4o-mini",
            system="You are helpful.",
            messages=[
                {"role": "user", "content": "inspect repo"},
                {"role": "assistant", "content": [tool_use_block]},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call_123",
                    "content": "file contents",
                    "is_error": False,
                }]},
            ],
            tools=[{
                "name": "read_file",
                "description": "read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }],
        )

        messages = provider.client.chat.completions.kwargs["messages"]
        assistant_messages = [message for message in messages if message["role"] == "assistant"]
        tool_messages = [message for message in messages if message["role"] == "tool"]

        self.assertEqual(len(assistant_messages), 1)
        self.assertEqual(assistant_messages[0]["tool_calls"][0]["id"], "call_123")
        self.assertEqual(
            json.loads(assistant_messages[0]["tool_calls"][0]["function"]["arguments"]),
            {"path": "README.md"},
        )
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_123")

    def test_fallback_provider_uses_next_provider_after_runtime_failure(self):
        class _FailingProvider:
            def create_messages(self, **kwargs):
                raise RuntimeError("anthropic down")

        class _WorkingProvider:
            def create_messages(self, **kwargs):
                return "ok"

        provider = FallbackProvider(
            ["anthropic", "openai"],
            factories={
                "anthropic": _FailingProvider,
                "openai": _WorkingProvider,
            },
        )

        self.assertEqual(provider.create_messages(model="x"), "ok")

    def test_fallback_provider_skips_unconfigured_provider(self):
        class _MissingProvider:
            def __init__(self):
                raise EnvironmentError("missing key")

        class _WorkingProvider:
            def create_messages(self, **kwargs):
                return "ok"

        provider = FallbackProvider(
            ["anthropic", "openai"],
            factories={
                "anthropic": _MissingProvider,
                "openai": _WorkingProvider,
            },
        )

        self.assertEqual(provider.create_messages(model="x"), "ok")


if __name__ == "__main__":
    unittest.main()
