import unittest
from types import SimpleNamespace
from unittest import mock

import phantom
from core.settings import prompt_choice


class CliTests(unittest.TestCase):
    def test_resolve_goal_returns_explicit_goal(self):
        self.assertEqual(phantom.resolve_goal("audit repo"), "audit repo")

    def test_resolve_goal_prompts_interactively_when_missing(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True

        with mock.patch("phantom.sys.stdin", fake_stdin), \
             mock.patch("builtins.input", return_value="summarize the workspace"):
            self.assertEqual(phantom.resolve_goal(None), "summarize the workspace")

    def test_resolve_goal_returns_none_for_non_tty(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False

        with mock.patch("phantom.sys.stdin", fake_stdin):
            self.assertIsNone(phantom.resolve_goal(None))

    def test_prompt_choice_maps_short_aliases(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True

        with mock.patch("core.settings.sys.stdin", fake_stdin), \
             mock.patch("builtins.input", return_value="r"):
            choice = prompt_choice(
                "Plan review",
                {"approve": ("a",), "revise": ("r",), "cancel": ("c",)},
                default="cancel",
            )

        self.assertEqual(choice, "revise")

    def test_interactive_chat_routes_menu_commands(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        args = SimpleNamespace()

        with mock.patch("phantom.sys.stdin", fake_stdin), \
             mock.patch("phantom.show_chat_menu"), \
             mock.patch("phantom.show_memory") as show_memory, \
             mock.patch("builtins.input", side_effect=["2", "0"]):
            phantom.interactive_chat(args)

        show_memory.assert_called_once()

    def test_interactive_chat_treats_plain_text_as_goal(self):
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        args = SimpleNamespace()

        with mock.patch("phantom.sys.stdin", fake_stdin), \
             mock.patch("phantom.show_chat_menu"), \
             mock.patch("phantom.run_goal_command") as run_goal_command, \
             mock.patch("builtins.input", side_effect=["review this repository", "0"]):
            phantom.interactive_chat(args)

        run_goal_command.assert_called_once_with("review this repository", args)

    def test_handle_task_done_displays_non_success_outcome(self):
        with mock.patch.object(phantom.console, "print") as console_print:
            phantom.handle("task_done", {"task": "Inspect repo", "outcome": "failed"})

        rendered = console_print.call_args.args[0]
        self.assertIn("failed", rendered)
        self.assertIn("✗", rendered)


if __name__ == "__main__":
    unittest.main()
