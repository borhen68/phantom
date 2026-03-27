import unittest
from unittest import mock

from core.skill_catalog import (
    assess_skill_support,
    bundled_skill_context,
    load_bundled_skills,
    match_bundled_skills,
)


class SkillCatalogTests(unittest.TestCase):
    def test_load_bundled_skills_returns_repo_catalog(self):
        skills = load_bundled_skills()
        names = {skill.name for skill in skills}
        self.assertIn("repository-review", names)
        self.assertIn("browser-operator", names)
        self.assertIn("chief-of-staff-briefing", names)
        self.assertIn("teach-and-replay", names)
        self.assertIn("messaging-operator", names)
        self.assertIn("signal-ingestion", names)
        self.assertIn("github", names)
        self.assertIn("slack", names)
        self.assertGreaterEqual(len(skills), 57)

    def test_match_bundled_skills_prefers_relevant_playbook(self):
        matches = match_bundled_skills("review this repository architecture", limit=2)
        self.assertTrue(matches)
        self.assertEqual(matches[0].name, "repository-review")

    def test_bundled_skill_context_renders_guidance(self):
        context = bundled_skill_context("prepare a project follow-up briefing for Nadia", limit=2)
        self.assertIn("BUNDLED PLAYBOOK GUIDANCE", context)
        self.assertIn("chief-of-staff-briefing", context)

    def test_frontmatter_and_resources_are_loaded(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        review = skills["repository-review"]
        self.assertIn("architecture", review.summary.lower())
        self.assertTrue(review.resources)
        self.assertIn("references:", review.resources[0])
        self.assertEqual(review.source, "phantom")

    def test_imported_openclaw_skills_are_tagged(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        github = skills["github"]
        self.assertEqual(github.source, "openclaw-compat")
        self.assertIn("GitHub", github.summary)

    def test_supported_imported_skill_is_shell_compatible(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        weather = skills["weather"]
        with mock.patch("core.skill_catalog.shutil.which", return_value="/usr/bin/curl"):
            support = assess_skill_support(weather)
        self.assertEqual(support.status, "shell-compatible")

    def test_missing_bin_blocks_imported_skill(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        github = skills["github"]

        def fake_which(name: str):
            return None if name == "gh" else f"/usr/bin/{name}"

        with mock.patch("core.skill_catalog.shutil.which", side_effect=fake_which):
            support = assess_skill_support(github)
        self.assertEqual(support.status, "blocked")
        self.assertIn("gh", support.missing_bins)

    def test_missing_runtime_surface_marks_skill_unsupported(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        bluebubbles = skills["bluebubbles"]
        support = assess_skill_support(bluebubbles)
        self.assertEqual(support.status, "unsupported")
        self.assertIn("channels.bluebubbles", support.missing_config)

    def test_slack_skill_is_blocked_without_token(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        slack = skills["slack"]
        with mock.patch.dict("os.environ", {}, clear=False):
            support = assess_skill_support(slack)
        self.assertEqual(support.status, "blocked")
        self.assertIn("PHANTOM_SLACK_BOT_TOKEN", support.missing_env)

    def test_discord_skill_is_blocked_without_token(self):
        skills = {skill.name: skill for skill in load_bundled_skills()}
        discord = skills["discord"]
        with mock.patch.dict("os.environ", {}, clear=False):
            support = assess_skill_support(discord)
        self.assertEqual(support.status, "blocked")
        self.assertIn("PHANTOM_DISCORD_BOT_TOKEN", support.missing_env)


if __name__ == "__main__":
    unittest.main()
