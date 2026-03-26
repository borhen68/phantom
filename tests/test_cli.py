import unittest
from unittest import mock

import phantom


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


if __name__ == "__main__":
    unittest.main()
