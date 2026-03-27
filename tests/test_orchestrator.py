import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.contracts import (
    AgentRunResult,
    FinalReport,
    PlanValidationResult,
    ProcedureMatch,
    TaskOutcome,
    TaskResult,
    TaskSpec,
    ToolExecutionResult,
    ToolExecutionStatus,
    VerificationResult,
)
from core.errors import BudgetExceeded, CriticEscalation
from core import orchestrator
from core.orchestrator import RunPhase


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

    def test_build_waves_respects_previously_completed_dependencies(self):
        tasks = [
            TaskSpec(id="t3", task="inspect core", depends_on=("t1",), parallel=True),
            TaskSpec(id="t4", task="inspect docs", depends_on=("t1",), parallel=True),
            TaskSpec(id="t5", task="summarize", depends_on=("t3", "t4"), parallel=False),
        ]

        waves = orchestrator._build_waves(tasks, completed_ids={"t1"})

        self.assertEqual([[task.id for task in wave] for wave in waves], [["t3", "t4"], ["t5"]])

    def test_plan_shortcuts_minimal_single_file_architecture_review(self):
        Path(self.workspace.name, "puzzle_game.py").write_text(
            "import random\n\n"
            "def main():\n"
            "    return random.randint(1, 10)\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )

        with mock.patch("core.orchestrator.mem.list_skills", return_value=[]), \
             mock.patch("core.orchestrator.run_agent_result") as run_agent:
            validation = orchestrator.plan(
                "review this repository and explain the architecture",
                "No prior context.",
            )

        run_agent.assert_not_called()
        self.assertEqual([task.id for task in validation.tasks], ["t1", "t2", "t3"])
        self.assertTrue(all("git" not in task.task.lower() for task in validation.tasks))

    def test_critic_allows_verified_single_file_architecture_reasoning(self):
        Path(self.workspace.name, "puzzle_game.py").write_text(
            "def main():\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        metrics = orchestrator.RunMetrics(
            goal="review this repository and explain the architecture",
            parallel=False,
            planner_model="planner",
            execution_model="executor",
            critic_model="critic",
        )
        critic = orchestrator.make_critic("review this repository and explain the architecture", metrics)
        reasoning = (
            "Performed a recursive search for Python source files. Only one file was found: puzzle_game.py. "
            "No additional packages or modules exist. The script defines main() and uses "
            "if __name__ == '__main__' as the entry point."
        )

        with mock.patch("core.orchestrator.run_agent") as run_agent:
            decision = critic(reasoning)

        run_agent.assert_not_called()
        self.assertEqual(decision.action, "allow")
        self.assertEqual(metrics.critic_checks, 1)

    def test_execute_task_uses_local_single_file_architecture_analysis(self):
        Path(self.workspace.name, "puzzle_game.py").write_text(
            "import random\n\n"
            "def main():\n"
            "    guess = input('guess: ')\n"
            "    print(guess)\n"
            "    return random.randint(1, 10)\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        task = TaskSpec(
            id="t1",
            task="Inspect the only source file `puzzle_game.py` and identify its entry point, imports, and main control flow.",
            depends_on=(),
            parallel=False,
        )

        with mock.patch("core.orchestrator.run_agent_result") as run_agent:
            result = orchestrator.execute_task(
                task=task,
                goal="review this repository and explain the architecture",
                context="No prior context.",
                dependency_results={},
                critic_fn=None,
            )

        run_agent.assert_not_called()
        self.assertEqual(result.outcome, TaskOutcome.SUCCESS)
        self.assertIn("puzzle_game.py", result.result)
        self.assertTrue(result.details.get("local_analysis"))

    def test_execute_task_includes_dependency_results(self):
        task = TaskSpec(id="t2", task="write the final report", depends_on=("t1",), parallel=False)

        with mock.patch("core.orchestrator.mem.world_context", return_value=""), \
             mock.patch("core.orchestrator.mem.chief_of_staff_context", return_value="CHIEF OF STAFF MEMORY:\n  Commitments:\n    - Send launch summary"), \
             mock.patch("core.orchestrator.mem.procedure_matches", return_value=[]), \
             mock.patch("core.orchestrator.mem.procedure_context", return_value="MATCHED PROCEDURES:\n  demo #4"), \
             mock.patch("core.orchestrator.mem.demonstration_context", return_value="HUMAN DEMONSTRATIONS:\n  Summary: click deploy"), \
             mock.patch("core.orchestrator._get_tools_with_skills", return_value=[]), \
             mock.patch(
                 "core.orchestrator.run_agent_result",
                 return_value=AgentRunResult(final_text='{"summary":"done","outcome":"success","facts":[]}'),
             ) as run_agent:
            result = orchestrator.execute_task(
                task=task,
                goal="finish the project",
                context="PAST EXPERIENCE:\n  [success] similar goal",
                dependency_results={"t1": "Collected the research findings"},
                critic_fn=None,
            )

        system_prompt = run_agent.call_args.kwargs["system"]
        self.assertIn("SHARED CONTEXT", system_prompt)
        self.assertIn("CHIEF OF STAFF MEMORY", system_prompt)
        self.assertIn("MATCHED PROCEDURES", system_prompt)
        self.assertIn("HUMAN DEMONSTRATIONS", system_prompt)
        self.assertIn("DEPENDENCY RESULTS", system_prompt)
        self.assertIn("Collected the research findings", system_prompt)
        self.assertIn("WORKSPACE", system_prompt)
        self.assertIn("inspect local files first", system_prompt)
        self.assertEqual(result.outcome, TaskOutcome.SUCCESS)
        self.assertEqual(result.result, "done")

    def test_plan_prompt_is_grounded_in_current_workspace(self):
        with mock.patch("core.orchestrator.mem.list_skills", return_value=[]), \
             mock.patch("core.orchestrator.run_agent_result", return_value=AgentRunResult(final_text="[]")) as run_agent:
            orchestrator.plan("analyze this repository", "No prior context.")

        prompt = run_agent.call_args.kwargs["messages"][0]["content"]
        self.assertIn("CURRENT WORKSPACE ROOT", prompt)
        self.assertIn("do not add clone/pull tasks", prompt.lower())

    def test_execute_task_reuses_strong_procedure_match_before_executor(self):
        task = TaskSpec(id="t1", task="deploy dashboard release", depends_on=(), parallel=False)
        match = ProcedureMatch(
            demo_id=9,
            goal="deploy dashboard release",
            summary="Open releases and click deploy",
            confidence=0.92,
            reliability=0.84,
            executable_steps=2,
            total_steps=2,
            ready_for_replay=True,
            reasons=("goal:deploy,dashboard",),
        )
        replay_result = ToolExecutionResult(
            name="replay_demonstration",
            status=ToolExecutionStatus.SUCCESS,
            ok=True,
            summary="Replay demonstration #9: deploy dashboard release",
            output="Replay demonstration #9: deploy dashboard release\n  1. ok",
            verification=VerificationResult(ok=True, summary="replay completed"),
        )

        with mock.patch("core.orchestrator.mem.world_context", return_value=""), \
             mock.patch("core.orchestrator.mem.procedure_matches", return_value=[match]), \
             mock.patch("core.orchestrator.mem.procedure_context", return_value="MATCHED PROCEDURES:\n demo #9"), \
             mock.patch("core.orchestrator.mem.demonstration_context", return_value=""), \
             mock.patch("core.orchestrator.dispatch_structured", return_value=replay_result), \
             mock.patch("core.orchestrator.mem.record_tool"), \
             mock.patch("core.orchestrator.run_agent_result") as run_agent:
            result = orchestrator.execute_task(
                task=task,
                goal="deploy dashboard release",
                context="No prior context.",
                dependency_results={},
                critic_fn=None,
            )

        run_agent.assert_not_called()
        self.assertEqual(result.outcome, TaskOutcome.SUCCESS)
        self.assertEqual(result.tool_results[0].name, "replay_demonstration")
        self.assertIn("demo #9", result.result)

    def test_execute_task_falls_back_to_executor_after_failed_procedure_replay(self):
        task = TaskSpec(id="t1", task="deploy dashboard release", depends_on=(), parallel=False)
        match = ProcedureMatch(
            demo_id=9,
            goal="deploy dashboard release",
            summary="Open releases and click deploy",
            confidence=0.92,
            reliability=0.84,
            executable_steps=2,
            total_steps=2,
            ready_for_replay=True,
            reasons=("goal:deploy,dashboard",),
        )
        replay_result = ToolExecutionResult(
            name="replay_demonstration",
            status=ToolExecutionStatus.RUNTIME_ERROR,
            ok=False,
            summary="Replay demonstration #9 failed",
            output="Replay demonstration #9 failed",
            verification=VerificationResult(ok=False, summary="replay had errors or blocked steps"),
        )

        with mock.patch("core.orchestrator.mem.world_context", return_value=""), \
             mock.patch("core.orchestrator.mem.procedure_matches", return_value=[match]), \
             mock.patch("core.orchestrator.mem.procedure_context", return_value="MATCHED PROCEDURES:\n demo #9"), \
             mock.patch("core.orchestrator.mem.demonstration_context", return_value=""), \
             mock.patch("core.orchestrator.dispatch_structured", return_value=replay_result), \
             mock.patch("core.orchestrator.mem.record_tool"), \
             mock.patch("core.orchestrator._get_tools_with_skills", return_value=[]), \
             mock.patch(
                 "core.orchestrator.run_agent_result",
                 return_value=AgentRunResult(final_text='{"summary":"done","outcome":"success","facts":[]}'),
             ) as run_agent:
            result = orchestrator.execute_task(
                task=task,
                goal="deploy dashboard release",
                context="No prior context.",
                dependency_results={},
                critic_fn=None,
            )

        system_prompt = run_agent.call_args.kwargs["system"]
        self.assertIn("PROCEDURE ATTEMPT", system_prompt)
        self.assertEqual(result.outcome, TaskOutcome.SUCCESS)
        self.assertEqual(result.tool_results[0].name, "replay_demonstration")

    def test_run_degrades_success_to_partial_after_task_failure(self):
        tasks = (
            TaskSpec(id="t1", task="good task", depends_on=(), parallel=False),
            TaskSpec(id="t2", task="bad task", depends_on=("t1",), parallel=False),
        )

        with mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 side_effect=[
                     TaskResult(id="t1", task="good task", outcome=TaskOutcome.SUCCESS, result="done"),
                     TaskResult(id="t2", task="bad task", outcome=TaskOutcome.FAILED, result="boom"),
                 ],
             ), \
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
                 side_effect=[
                     CriticEscalation("Critic blocked progress 3 times: unsafe"),
                     TaskResult(id="t3", task="safer task", outcome=TaskOutcome.SUCCESS, result="done"),
                 ],
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
             mock.patch("core.orchestrator.prompt_choice", return_value="approve"), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 return_value=TaskResult(id="t1", task="safe task", outcome=TaskOutcome.SUCCESS, result="done"),
             ) as execute_task, \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="success", lessons=()),
             ):
            result = orchestrator.run("ship it", parallel=False)

        execute_task.assert_called_once()
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["tasks_completed"], 1)

    def test_run_can_revise_plan_before_execution(self):
        original = (TaskSpec(id="t1", task="risky task", depends_on=(), parallel=False),)
        revised = (TaskSpec(id="t1", task="safe task", depends_on=(), parallel=False),)

        with mock.patch.dict(os.environ, {"PHANTOM_CONFIRM_PLAN": "1"}, clear=False), \
             mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=original)), \
             mock.patch("core.orchestrator.prompt_choice", side_effect=["revise", "approve"]), \
             mock.patch("core.orchestrator.prompt_text", return_value="Use the safer local path"), \
             mock.patch("core.orchestrator.revise_plan", return_value=revised) as revise_plan, \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 return_value=TaskResult(id="t1", task="safe task", outcome=TaskOutcome.SUCCESS, result="done"),
             ) as execute_task, \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="success", lessons=()),
             ):
            result = orchestrator.run("ship it", parallel=False)

        revise_plan.assert_called_once()
        self.assertEqual(execute_task.call_args.args[0].task, "safe task")
        self.assertEqual(result["outcome"], "success")

    def test_run_session_tracks_terminal_phase(self):
        tasks = (TaskSpec(id="t1", task="safe task", depends_on=(), parallel=False),)

        with mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 return_value=TaskResult(id="t1", task="safe task", outcome=TaskOutcome.SUCCESS, result="done"),
             ), \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="success", lessons=()),
             ):
            session = orchestrator.RunSession("ship it", parallel=False)
            result = session.run()

        self.assertEqual(result["outcome"], "success")
        self.assertEqual(session.phase, RunPhase.DONE)


if __name__ == "__main__":
    unittest.main()
