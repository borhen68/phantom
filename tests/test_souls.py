import unittest
from unittest import mock

from core.loop import run_agent
from core.souls import soul_for, system_with_soul


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


class SoulTests(unittest.TestCase):
    def test_planner_soul_has_self_written_identity(self):
        shade = soul_for("planner")

        self.assertEqual(shade.name, "Shade")
        self.assertIn("I am Shade", shade.self_written)
        self.assertIn("planner soul", shade.title)

    def test_system_with_soul_prefixes_identity(self):
        system = system_with_soul("executor", "Use tools carefully.")

        self.assertIn("You are Forge", system)
        self.assertIn("I am Forge", system)
        self.assertIn("Use tools carefully.", system)

    def test_run_agent_emits_soul_intro_for_non_critic_roles(self):
        events = []

        with mock.patch("core.loop.client", return_value=_FakeProvider()):
            run_agent(
                role="planner",
                model="fake-model",
                system="Output json only.",
                messages=[{"role": "user", "content": "Plan the release"}],
                on_event=lambda event_type, data: events.append((event_type, data)),
                max_steps=1,
            )

        soul_events = [payload for event_type, payload in events if event_type == "soul"]
        self.assertEqual(len(soul_events), 1)
        self.assertEqual(soul_events[0]["name"], "Shade")
        self.assertIn("I am Shade", soul_events[0]["intro"])

    def test_run_agent_skips_soul_intro_for_critic_calls(self):
        events = []

        with mock.patch("core.loop.client", return_value=_FakeProvider()):
            run_agent(
                role="critic",
                model="fake-model",
                system="Return json.",
                messages=[{"role": "user", "content": "Review reasoning"}],
                on_event=lambda event_type, data: events.append((event_type, data)),
                max_steps=1,
            )

        self.assertFalse(any(event_type == "soul" for event_type, _ in events))


if __name__ == "__main__":
    unittest.main()
