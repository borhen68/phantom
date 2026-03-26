import os
import unittest
from unittest import mock

from core.contracts import RunMetrics
from core.errors import BudgetExceeded
from core.loop import run_agent


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResponse:
    def __init__(self):
        self.content = [_FakeTextBlock("done")]
        self.stop_reason = "end_turn"
        self.usage = _FakeUsage()


class _FakeProvider:
    def create_messages(self, **kwargs):
        return _FakeResponse()


class BudgetTests(unittest.TestCase):
    def test_run_agent_honors_llm_call_budget(self):
        metrics = RunMetrics(
            goal="budget",
            parallel=False,
            planner_model="planner",
            execution_model="exec",
            critic_model="critic",
        )
        with mock.patch.dict(os.environ, {"PHANTOM_MAX_LLM_CALLS": "0"}, clear=False), \
             mock.patch("core.loop.client", return_value=_FakeProvider()):
            with self.assertRaises(BudgetExceeded):
                run_agent(
                    role="tester",
                    model="fake-model",
                    system="system",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    metrics=metrics,
                )

    def test_run_agent_honors_llm_rate_limit(self):
        metrics = RunMetrics(
            goal="budget",
            parallel=False,
            planner_model="planner",
            execution_model="exec",
            critic_model="critic",
        )
        metrics.note_llm_call()

        with mock.patch.dict(os.environ, {"PHANTOM_MAX_LLM_CALLS_PER_MINUTE": "1"}, clear=False), \
             mock.patch("core.loop.client", return_value=_FakeProvider()):
            with self.assertRaises(BudgetExceeded):
                run_agent(
                    role="tester",
                    model="fake-model",
                    system="system",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=None,
                    metrics=metrics,
                )


if __name__ == "__main__":
    unittest.main()
