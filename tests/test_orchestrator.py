import os
import tempfile
import unittest
from unittest import mock

from core.contracts import FinalReport, PlanValidationResult, TaskSpec
from core.errors import BudgetExceeded, CriticEscalation
from core import orchestrator


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.workspace.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "PHANTOM_HOME": self.home.name,
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_SCOPE": "tests::orchestrator",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_build_waves_respects_serial_tasks(self):
        tasks = [
            TaskSpec(id="t1", task="prep", depends_on=(), parallel=False),
            TaskSpec(id="t2", task="worker a", depends_on=(), parallel=True),
            TaskSpec(id="t3", task="worker b", depends_on=(), parallel=True),
            TaskSpec(id="t4", task="merge", depends_on=("t2", "t3"), parallel=False),
        ]

        waves = orchestrator._build_waves(tasks)

        self.assertEqual([[task.id for task in wave] for wave in waves], [["t1"], ["t2", "t3"], ["t4"]])

    def test_execute_task_includes_dependency_results(self):
        task = TaskSpec(id="t2", task="write the final report", depends_on=("t1",), parallel=False)

        with mock.patch("core.orchestrator.mem.world_context", return_value=""), \
             mock.patch("core.orchestrator.mem.demonstration_context", return_value="HUMAN DEMONSTRATIONS:\n  Summary: click deploy"), \
             mock.patch("core.orchestrator._get_tools_with_skills", return_value=[]), \
             mock.patch("core.orchestrator.run_agent", return_value="done") as run_agent:
            orchestrator.execute_task(
                task=task,
                goal="finish the project",
                context="PAST EXPERIENCE:\n  [success] similar goal",
                dependency_results={"t1": "Collected the research findings"},
                critic_fn=None,
            )

        system_prompt = run_agent.call_args.kwargs["system"]
        self.assertIn("SHARED CONTEXT", system_prompt)
        self.assertIn("HUMAN DEMONSTRATIONS", system_prompt)
        self.assertIn("DEPENDENCY RESULTS", system_prompt)
        self.assertIn("Collected the research findings", system_prompt)
        self.assertIn("WORKSPACE", system_prompt)
        self.assertIn("inspect local files first", system_prompt)

    def test_plan_prompt_is_grounded_in_current_workspace(self):
        with mock.patch("core.orchestrator.mem.list_skills", return_value=[]), \
             mock.patch("core.orchestrator.run_agent", return_value="[]") as run_agent:
            orchestrator.plan("analyze this repository", "No prior context.")

        prompt = run_agent.call_args.kwargs["messages"][0]["content"]
        self.assertIn("CURRENT WORKSPACE ROOT", prompt)
        self.assertIn("do not add clone/pull tasks", prompt.lower())

    def test_run_degrades_success_to_partial_after_task_failure(self):
        tasks = (
            TaskSpec(id="t1", task="good task", depends_on=(), parallel=False),
            TaskSpec(id="t2", task="bad task", depends_on=("t1",), parallel=False),
        )

        with mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch("core.orchestrator.execute_task", side_effect=["done", "Task failed: boom"]), \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="success", lessons=()),
             ):
            result = orchestrator.run("ship it", parallel=False)

        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["tasks_completed"], 2)
        self.assertEqual(result["metrics"]["task_failures"], 1)

    def test_run_halts_cleanly_on_budget_stop(self):
        tasks = (TaskSpec(id="t1", task="expensive task", depends_on=(), parallel=False),)

        with mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch("core.orchestrator.execute_task", side_effect=BudgetExceeded("Run exceeded max LLM calls (1).")):
            result = orchestrator.run("ship it", parallel=False)

        self.assertEqual(result["outcome"], "failure")
        self.assertIn("Run halted", result["summary"])
        self.assertIn("budget_exceeded", result["lessons"])

    def test_critic_escalation_triggers_replan_instead_of_run_halt(self):
        tasks = (
            TaskSpec(id="t1", task="risky task", depends_on=(), parallel=False),
            TaskSpec(id="t2", task="follow-up", depends_on=("t1",), parallel=False),
        )
        replacement = (TaskSpec(id="t3", task="safer task", depends_on=(), parallel=False),)

        with mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 side_effect=[CriticEscalation("Critic blocked progress 3 times: unsafe"), "done"],
             ), \
             mock.patch("core.orchestrator.replan", return_value=replacement) as replan, \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="partial", lessons=()),
             ):
            result = orchestrator.run("ship it", parallel=False)

        self.assertTrue(replan.called)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["tasks_completed"], 2)

    def test_run_stops_before_execution_when_plan_approval_declined(self):
        tasks = (TaskSpec(id="t1", task="safe task", depends_on=(), parallel=False),)

        with mock.patch.dict(os.environ, {"PHANTOM_CONFIRM_PLAN": "1"}, clear=False), \
             mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.prompt_user", return_value=False), \
             mock.patch("core.orchestrator.execute_task") as execute_task, \
             mock.patch("core.orchestrator.synthesize") as synthesize:
            result = orchestrator.run("ship it", parallel=False)

        execute_task.assert_not_called()
        synthesize.assert_not_called()
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(result["tasks_completed"], 0)
        self.assertIn("plan_declined", result["lessons"])

    def test_run_executes_after_plan_approval(self):
        tasks = (TaskSpec(id="t1", task="safe task", depends_on=(), parallel=False),)

        with mock.patch.dict(os.environ, {"PHANTOM_CONFIRM_PLAN": "1"}, clear=False), \
             mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.prompt_user", return_value=True), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch("core.orchestrator.execute_task", return_value="done") as execute_task, \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="success", lessons=()),
             ):
            result = orchestrator.run("ship it", parallel=False)

        execute_task.assert_called_once()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["tasks_completed"], 1)


if __name__ == "__main__":
    unittest.main()
