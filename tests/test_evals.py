import unittest

from evals.offline import run_offline_evals


class EvalTests(unittest.TestCase):
    def test_offline_evals_all_pass(self):
        summary = run_offline_evals()

        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["passed"], summary["total"])


if __name__ == "__main__":
    unittest.main()
