import unittest

from core.contracts import (
    ArtifactRef,
    CriticDecision,
    FinalReport,
    ProcedureMatch,
    TaskOutcome,
    TaskExecutionReport,
    ToolExecutionStatus,
    TaskResult,
    TaskSpec,
    ToolExecutionResult,
    VerificationResult,
    assess_plan_quality,
    normalize_plan_payload,
)


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

    def test_task_outcome_infers_failure_from_structured_tool_results(self):
        tool_result = ToolExecutionResult(
            name="shell",
            status=ToolExecutionStatus.RUNTIME_ERROR,
            ok=False,
            summary="command failed",
            output="command failed",
        )

        self.assertEqual(TaskOutcome.infer("", (tool_result,)), TaskOutcome.FAILED)

    def test_task_execution_report_parses_executor_json(self):
        report = TaskExecutionReport.from_text(
            '{"summary":"completed the task","outcome":"success","facts":[{"key":"repo_root","value":"/tmp/demo","confidence":0.9}]}'
        )

        self.assertEqual(report.summary, "completed the task")
        self.assertEqual(report.outcome, TaskOutcome.SUCCESS)
        self.assertEqual(report.facts[0]["key"], "repo_root")

    def test_task_result_preserves_structured_execution_details(self):
        verification = VerificationResult(ok=True, summary="1/1 tool calls succeeded")
        tool_result = ToolExecutionResult(
            name="write_file",
            status=ToolExecutionStatus.SUCCESS,
            ok=True,
            summary="Wrote file",
            output="Wrote file",
            verification=verification,
            artifacts=(ArtifactRef(kind="file", label="write_file", path="notes.txt"),),
        )
        task_result = TaskResult(
            id="t1",
            task="write notes",
            outcome=TaskOutcome.SUCCESS,
            result="Saved the notes file.",
            tool_results=(tool_result,),
            verification=verification,
            artifacts=tool_result.artifacts,
        )

        payload = task_result.as_dict()

        self.assertEqual(payload["tool_results"][0]["name"], "write_file")
        self.assertEqual(payload["verification"]["summary"], "1/1 tool calls succeeded")
        self.assertEqual(payload["artifacts"][0]["path"], "notes.txt")

    def test_procedure_match_renders_replay_readiness(self):
        match = ProcedureMatch(
            demo_id=7,
            goal="deploy dashboard release",
            summary="Open releases and press deploy",
            confidence=0.91,
            reliability=0.83,
            executable_steps=2,
            total_steps=3,
            ready_for_replay=True,
            reasons=("goal:deploy,dashboard", "reliable:0.83"),
            app="admin_console",
            environment="staging",
            tags=("release", "dashboard"),
            last_replay_status="success",
        )

        rendered = match.render_for_executor()

        self.assertIn("demo #7", rendered)
        self.assertIn("ready_for_replay=yes", rendered)
        self.assertIn("executable_steps=2/3", rendered)


if __name__ == "__main__":
    unittest.main()
