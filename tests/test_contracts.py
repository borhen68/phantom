import unittest

from core.contracts import CriticDecision, FinalReport, TaskOutcome, TaskSpec, assess_plan_quality, normalize_plan_payload


class ContractTests(unittest.TestCase):
    def test_normalize_plan_payload_repairs_invalid_entries(self):
        payload = [
            {"id": "alpha", "task": "inspect repo", "depends_on": ["ghost"], "parallel": "yes"},
            {"task": "write summary", "depends_on": ["alpha"], "parallel": False},
            "bad",
        ]

        result = normalize_plan_payload(payload, "fallback goal")

        self.assertTrue(result.used_fallback)
        self.assertEqual([task.id for task in result.tasks], ["t1", "t2"])
        self.assertEqual(result.tasks[0].depends_on, ())
        self.assertEqual(result.tasks[1].depends_on, ())

    def test_final_report_parsing_is_stable(self):
        report = FinalReport.from_text("All done\nOUTCOME: success\nLESSONS: [\"alpha\", \"beta\"]")

        self.assertEqual(report.summary, "All done")
        self.assertEqual(report.outcome, "success")
        self.assertEqual(report.lessons, ("alpha", "beta"))

    def test_final_report_parses_json_contract(self):
        report = FinalReport.from_text('{"summary":"All done","outcome":"success","lessons":["alpha"]}')

        self.assertEqual(report.summary, "All done")
        self.assertEqual(report.outcome, "success")
        self.assertEqual(report.lessons, ("alpha",))

    def test_critic_decision_distinguishes_revise_from_block(self):
        revise = CriticDecision.from_text('{"action":"revise","issue":"be more specific","severity":"medium"}')
        block = CriticDecision.from_text('{"action":"block","issue":"dangerous file deletion","severity":"high"}')

        self.assertTrue(revise.requires_revision())
        self.assertFalse(revise.blocks_progress())
        self.assertTrue(block.requires_revision())
        self.assertTrue(block.blocks_progress())

    def test_plan_quality_flags_duplicated_goal_echo(self):
        tasks = (
            TaskSpec(id="t1", task="build the dashboard", depends_on=(), parallel=False),
            TaskSpec(id="t2", task="build the dashboard", depends_on=(), parallel=False),
        )

        report = assess_plan_quality("build the dashboard", tasks)

        self.assertLess(report.score, 60)
        self.assertIn("duplicate_tasks", report.issues)
        self.assertIn("tasks_repeat_goal", report.issues)

    def test_task_outcome_classifies_non_exception_failure_text(self):
        self.assertEqual(TaskOutcome.from_result_text("Task failed: boom"), TaskOutcome.FAILED)
        self.assertEqual(
            TaskOutcome.from_result_text("Human checkpoint declined write_file: foo"),
            TaskOutcome.CHECKPOINT_DECLINED,
        )
        self.assertEqual(
            TaskOutcome.from_result_text("Task blocked by critic: unsafe"),
            TaskOutcome.CRITIC_BLOCKED,
        )


if __name__ == "__main__":
    unittest.main()
