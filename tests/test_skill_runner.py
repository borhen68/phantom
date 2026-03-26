import sys
import unittest
from unittest import mock

from tools.skill_runner import build_skill_command, build_skill_commands


class SkillRunnerTests(unittest.TestCase):
    def test_build_skill_command_prefers_bubblewrap_on_linux_when_available(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch("tools.skill_runner._command_available", side_effect=lambda args: args[0] == "bwrap"):
            command = build_skill_command("/tmp/skill_runner.py")

        self.assertEqual(command[:2], ["bwrap", "--die-with-parent"])
        self.assertEqual(command[-3:], [sys.executable, "-I", "/tmp/skill_runner.py"])

    def test_build_skill_commands_fall_back_from_nsjail_to_unshare_to_python(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch("tools.skill_runner._command_available", side_effect=lambda args: args[0] in {"nsjail", "unshare"}):
            commands = build_skill_commands("/tmp/skill_runner.py")

        self.assertEqual(commands[0][0], "nsjail")
        self.assertEqual(commands[1][:3], ["unshare", "--net", "--"])
        self.assertEqual(commands[2], [sys.executable, "-I", "/tmp/skill_runner.py"])

    def test_build_skill_command_falls_back_without_unshare(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch("tools.skill_runner._command_available", return_value=False):
            command = build_skill_command("/tmp/skill_runner.py")

        self.assertEqual(command, [sys.executable, "-I", "/tmp/skill_runner.py"])


if __name__ == "__main__":
    unittest.main()
