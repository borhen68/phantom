import unittest

from core.extensions import extension_context, extension_load_report, load_extensions, match_extensions


class ExtensionTests(unittest.TestCase):
    def test_load_extensions_discovers_repo_manifests(self):
        manifests = load_extensions()
        ids = {item.extension_id for item in manifests}
        self.assertIn("browser-operator", ids)
        self.assertIn("chief-of-staff", ids)
        self.assertIn("messaging", ids)

    def test_match_extensions_prefers_relevant_capabilities(self):
        matches = match_extensions("telegram whatsapp webhook pairing", limit=2)
        self.assertTrue(matches)
        self.assertEqual(matches[0].extension_id, "messaging")

    def test_extension_context_renders_relevant_extensions(self):
        context = extension_context("browser workflow verification", limit=2)
        self.assertIn("RELEVANT EXTENSIONS", context)
        self.assertIn("browser-operator", context)

    def test_extension_load_report_tracks_manifest_count(self):
        report = extension_load_report()
        self.assertGreaterEqual(report["count"], 3)
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
