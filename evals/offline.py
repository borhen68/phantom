"""Deterministic offline evals for PHANTOM engineering invariants."""

from __future__ import annotations

import os
import tempfile
import time
import hmac
import hashlib
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import memory
from core.contracts import (
    FinalReport,
    PlanValidationResult,
    RunMetrics,
    TaskOutcome,
    TaskResult,
    TaskSpec,
    assess_plan_quality,
    normalize_plan_payload,
)
from core.errors import BudgetExceeded, CriticEscalation
from core.observability import TraceRecorder, replay_trace
from core import orchestrator
from core.orchestrator import _build_waves
from core.loop import run_agent
from core.providers import FallbackProvider
from integrations.messaging import InboundMessage, MessagingService, verify_whatsapp_signature
from tools import dispatch


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    duration_ms: int
    detail: str


def _run_case(name, fn) -> EvalResult:
    started = time.time()
    try:
        detail = fn() or "ok"
        return EvalResult(name=name, passed=True, duration_ms=int((time.time() - started) * 1000), detail=detail)
    except AssertionError as exc:
        return EvalResult(name=name, passed=False, duration_ms=int((time.time() - started) * 1000), detail=str(exc))


def _case_plan_contracts() -> str:
    payload = [
        {"id": "alpha", "task": "inspect repo", "depends_on": ["ghost"], "parallel": "yes"},
        {"task": "write summary", "depends_on": ["alpha"], "parallel": False},
        "invalid-item",
    ]
    result = normalize_plan_payload(payload, "fallback goal")
    assert result.used_fallback, "invalid planner payload should mark fallback usage"
    assert len(result.tasks) == 2, "valid tasks should survive normalization"
    assert result.tasks[0].id == "t1", "invalid ids should be normalized"
    assert result.tasks[0].depends_on == (), "unknown dependencies should be removed"
    return "planner output normalization works"


def _case_final_report_parsing() -> str:
    report = FinalReport.from_text("All done\nOUTCOME: success\nLESSONS: [\"alpha\", \"beta\"]")
    assert report.outcome == "success", "outcome should parse"
    assert report.lessons == ("alpha", "beta"), "lessons should parse"
    return "final report parsing works"


def _case_final_report_json_contract() -> str:
    report = FinalReport.from_text('{"summary":"done","outcome":"success","lessons":["alpha"]}')
    assert report.summary == "done", "json summary should parse"
    assert report.outcome == "success", "json outcome should parse"
    assert report.lessons == ("alpha",), "json lessons should parse"
    return "final report json contract works"


def _case_wave_builder() -> str:
    tasks = normalize_plan_payload([
        {"id": "t1", "task": "prep", "parallel": False},
        {"id": "t2", "task": "worker a", "parallel": True},
        {"id": "t3", "task": "worker b", "parallel": True},
        {"id": "t4", "task": "merge", "depends_on": ["t2", "t3"], "parallel": False},
    ], "fallback").tasks
    waves = _build_waves(tasks)
    assert [[task.id for task in wave] for wave in waves] == [["t1"], ["t2", "t3"], ["t4"]], "waves should respect serial tasks"
    return "wave scheduling respects dependencies"


def _case_safety_guards() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            result, err = dispatch("shell", {"cmd": "git reset --hard"})
            assert err and "blocked" in result.lower(), "dangerous shell commands should be blocked"

            result, err = dispatch("write_file", {"path": "../escape.txt", "content": "nope"})
            assert err and "allowed roots" in result.lower(), "workspace escape should be blocked"

            code = "import subprocess\n\ndef run(inputs):\n    return 'bad'\n"
            result, err = dispatch("create_skill", {"name": "bad_skill", "description": "bad", "code": code})
            assert err and "blocked module" in result.lower(), "unsafe skill imports should be blocked"

    return "tool safety policy blocks dangerous actions"


def _case_safe_skill_runtime() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            file_path = Path(workspace) / "data.txt"
            file_path.write_text("hello", encoding="utf-8")

            code = "def run(inputs):\n    with open(inputs['path']) as f:\n        return f.read().upper()\n"
            result, err = dispatch("create_skill", {"name": "upper_reader", "description": "Read data", "code": code})
            assert not err, result

            result, err = dispatch("use_skill", {"name": "upper_reader", "inputs": {"path": str(file_path)}})
            assert not err and result == "HELLO", "safe skill should read workspace files"

    return "safe skills run inside the restricted runtime"


def _case_memory_versioning() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::memory",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            memory.learn("project_dir", "/tmp/a")
            memory.learn("project_dir", "/tmp/b")
            facts = memory.recent_world_facts(limit=1)
            assert facts[0]["version"] == 2, "fact version should increment on conflict"
            assert facts[0]["conflicts"] == 1, "conflict count should increment"
    return "memory versioning tracks conflicts"


def _case_demonstration_learning() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        screenshot = Path(workspace) / "step-1.png"
        screenshot.write_text("fake screenshot", encoding="utf-8")
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::demonstrations",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            saved = memory.save_demonstration(
                goal="deploy release to dashboard",
                summary="Human demonstrated the deployment path",
                steps=["Open settings", "Click Deploy", "Confirm release"],
                screenshots=[str(screenshot)],
            )
            demos = memory.recall_demonstrations("deploy dashboard release")
            context = memory.demonstration_context("deploy dashboard release", demonstrations=demos)
            assert demos and demos[0]["id"] == saved["id"], "relevant demonstrations should be recalled"
            assert "Click Deploy" in context, "demonstration steps should appear in context"
            assert saved["screenshots"], "screenshots should be copied into PHANTOM storage"
    return "human demonstrations are stored and recalled"


def _case_chief_of_staff_memory() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::chief-of-staff",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            memory.save_person("Nadia", relationship="manager", notes="Approves release copy")
            memory.save_project("Launch", status="active", notes="Public release")
            memory.save_commitment(
                "Send launch summary",
                counterparty="Nadia",
                project="Launch",
                due_at="Friday",
                notes="Before the release meeting",
            )
            briefing = memory.chief_of_staff_briefing("launch summary for Nadia", limit=5)
            context = memory.chief_of_staff_context("launch summary for Nadia", limit=5)
            assert briefing["people"], "people should appear in briefing"
            assert briefing["projects"], "projects should appear in briefing"
            assert briefing["commitments"], "commitments should appear in briefing"
            assert "Send launch summary" in context, "context should include relevant commitments"
    return "chief-of-staff memory surfaces people, projects, and commitments"


def _case_signal_ingestion() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::signals",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            saved = memory.ingest_signal(
                "message",
                "We will send the launch summary before Friday.",
                source="telegram",
                title="Nadia follow-up",
                metadata={
                    "people": [{"name": "Nadia", "relationship": "manager"}],
                    "project": {"name": "Launch", "status": "active"},
                    "counterparty": "Nadia",
                    "due_at": "Friday",
                },
            )
            signals = memory.list_signals(limit=5)
            briefing = memory.chief_of_staff_briefing("launch summary for Nadia", limit=5)
            assert signals and signals[0]["id"] == saved["id"], "signals should be stored"
            assert briefing["signals"], "briefing should surface relevant signals"
            assert briefing["commitments"], "signal ingestion should extract commitments"
    return "signal ingestion stores raw signals and extracts chief-of-staff memory"


def _case_browser_demonstration_replay() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::browser-demo",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            demo = memory.save_demonstration(
                goal="check dashboard status",
                summary="browser demo",
                steps=[
                    {"action": "browser_goto", "inputs": {"url": "https://example.com"}, "instructions": "Open dashboard", "executable": True},
                    {"action": "browser_click", "inputs": {"selector": "#status"}, "instructions": "Open status panel", "executable": True},
                    {"action": "browser_extract_text", "inputs": {"selector": "h1", "name": "heading"}, "instructions": "Read heading", "executable": True},
                ],
            )
            mocked = {
                "final_url": "https://example.com/status",
                "title": "Status",
                "steps_executed": ["goto https://example.com", "click #status", "extract_text h1"],
                "extracted": [{"name": "heading", "selector": "h1", "text": "All systems operational"}],
                "screenshots": [],
            }
            with mock.patch("tools.browser_runtime.run_browser_workflow", return_value=mocked) as patched:
                result, err = dispatch("replay_demonstration", {"id": demo["id"], "execute": True})
            assert not err, result
            assert patched.call_count == 1, "contiguous browser steps should share one browser session"
            assert "All systems operational" in result, "browser replay should surface extracted text"
    return "browser demonstration replay batches browser actions"


def _case_provider_fallback() -> str:
    class _FailingProvider:
        def create_messages(self, **kwargs):
            raise RuntimeError("anthropic down")

    class _WorkingProvider:
        def create_messages(self, **kwargs):
            return "ok"

    provider = FallbackProvider(
        ["anthropic", "openai"],
        factories={
            "anthropic": _FailingProvider,
            "openai": _WorkingProvider,
        },
    )
    assert provider.create_messages(model="x") == "ok", "fallback chain should use the next provider"
    return "provider fallback works"


def _case_messaging_dedupe_and_signature() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::messaging",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            seen = []
            service = MessagingService(
                run_goal=lambda **kwargs: seen.append(kwargs["goal"]) or {"summary": "ok", "outcome": "success"},
                telegram_sender=lambda conversation_id, text: None,
                max_workers=1,
            )
            message = InboundMessage(
                platform="telegram",
                message_id="dup-1",
                conversation_id="chat-1",
                sender_id="user-1",
                text="audit repo",
            )
            assert service.submit(message), "first message should be accepted"
            service.shutdown(wait=True)

            restarted = MessagingService(
                run_goal=lambda **kwargs: {"summary": "ok", "outcome": "success"},
                telegram_sender=lambda conversation_id, text: None,
                max_workers=1,
            )
            assert not restarted.submit(message), "duplicate message should be rejected after restart"
            restarted.shutdown(wait=True)

            body = b'{"entry":[{"changes":[]}]}'
            secret = "app-secret"
            sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            assert verify_whatsapp_signature(body, sig, secret), "valid WhatsApp signatures should verify"
            assert not verify_whatsapp_signature(body, "sha256=bad", secret), "bad WhatsApp signatures should fail"
    return "messaging dedupe and signature verification work"


def _case_demonstration_reliability_ranking() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::demo-ranking",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            trusted = memory.save_demonstration(
                goal="deploy dashboard release",
                summary="trusted path",
                steps=["Open releases", "Click deploy"],
                tags=["release", "dashboard"],
            )
            flaky = memory.save_demonstration(
                goal="deploy dashboard release",
                summary="flaky path",
                steps=["Open settings", "Click deploy"],
                tags=["release", "dashboard"],
            )
            memory.record_demonstration_feedback(trusted["id"], success=True, note="worked")
            memory.record_demonstration_feedback(trusted["id"], success=True, note="worked again")
            memory.record_demonstration_feedback(flaky["id"], success=False, note="drift")
            demos = memory.recall_demonstrations("deploy dashboard release")
            assert demos[0]["id"] == trusted["id"], "reliable demonstrations should rank first"
            assert demos[0]["reliability"] > demos[1]["reliability"], "reliability should influence ranking"
    return "demonstration reliability influences recall ranking"


def _case_skill_version_rollback() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::skills",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            memory.init()
            dispatch("create_skill", {
                "name": "echoer",
                "description": "v1",
                "code": "def run(inputs):\n    return 'v1'\n",
            })
            dispatch("create_skill", {
                "name": "echoer",
                "description": "v2",
                "code": "def run(inputs):\n    return 'v2'\n",
            })
            versions = memory.list_skill_versions("echoer")
            assert [item["version"] for item in versions[:2]] == [2, 1], "skill history should keep both versions"
            ok = memory.rollback_skill("echoer", 1)
            assert ok, "rollback should succeed"
            result, err = dispatch("use_skill", {"name": "echoer", "inputs": {}})
            assert not err and result == "v1", "rollback should restore prior code"
    return "skill version rollback works"


def _case_trace_replay() -> str:
    with tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::trace",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            recorder = TraceRecorder(goal="trace me")
            recorder.record("start", {"goal": "trace me"}, agent="orchestrator")
            recorder.record("done", {"outcome": "success"}, agent="orchestrator")
            events = replay_trace(recorder.trace_id)
            assert len(events) == 2, "trace replay should load written events"
            assert events[0]["trace_id"] == recorder.trace_id, "trace ids should match"
    return "trace replay works"


def _case_plan_quality() -> str:
    tasks = normalize_plan_payload([
        {"id": "t1", "task": "inspect the codebase and identify the bug", "parallel": False},
        {"id": "t2", "task": "implement the fix in the relevant module", "depends_on": ["t1"], "parallel": False},
        {"id": "t3", "task": "run regression tests and summarize the outcome", "depends_on": ["t2"], "parallel": False},
    ], "fix the bug").tasks
    report = assess_plan_quality("fix the bug", tasks)
    assert report.score >= 60, "good plans should clear the heuristic threshold"
    assert not report.issues, "good plans should not trip quality issues"
    return "planner quality heuristics accept sensible decompositions"


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeResponse:
    def __init__(self):
        self.content = [_FakeTextBlock("done")]
        self.stop_reason = "end_turn"
        self.usage = _FakeUsage()


class _FakeProvider:
    def create_messages(self, **kwargs):
        return _FakeResponse()


def _case_scope_isolation() -> str:
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        base_env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::scope-a",
        }
        with mock.patch.dict(os.environ, base_env, clear=False):
            memory.init()
            memory.learn("project_dir", "/tmp/a")
            assert memory.know("project_dir") == "/tmp/a", "fact should exist in first scope"

        with mock.patch.dict(os.environ, {**base_env, "PHANTOM_SCOPE": "eval::scope-b"}, clear=False):
            memory.init()
            assert memory.know("project_dir") is None, "facts should not bleed across scopes"
    return "scope isolation keeps world facts separate"


def _case_budget_rate_limit() -> str:
    metrics = RunMetrics(
        goal="eval budget",
        parallel=False,
        planner_model="planner",
        execution_model="exec",
        critic_model="critic",
    )
    metrics.note_llm_call()
    with mock.patch.dict(os.environ, {"PHANTOM_MAX_LLM_CALLS_PER_MINUTE": "1"}, clear=False), \
         mock.patch("core.loop.client", return_value=_FakeProvider()):
        try:
            run_agent(
                role="tester",
                model="fake-model",
                system="system",
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
                metrics=metrics,
            )
        except BudgetExceeded:
            return "rate limits stop excess LLM calls"
    raise AssertionError("rate limit should have raised BudgetExceeded")


def _case_partial_failure_handling() -> str:
    tasks = (TaskSpec(id="t1", task="ok", depends_on=(), parallel=False),)
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::partial",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch("core.orchestrator.execute_task", side_effect=BudgetExceeded("Run exceeded max LLM calls (1).")):
            result = orchestrator.run("partial failure", parallel=False)
            assert result["outcome"] == "failure", "run should halt cleanly when budgets are exceeded before any task finishes"
            assert "budget_exceeded" in result["lessons"], "budget stop should be surfaced in lessons"
    return "run halts cleanly on control-plane stop"


def _case_critic_replan_feedback() -> str:
    tasks = (
        TaskSpec(id="t1", task="unsafe path", depends_on=(), parallel=False),
        TaskSpec(id="t2", task="finish task", depends_on=("t1",), parallel=False),
    )
    replacement = (TaskSpec(id="t3", task="safe path", depends_on=(), parallel=False),)
    with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
        env = {
            "PHANTOM_WORKSPACE": workspace,
            "PHANTOM_HOME": home,
            "PHANTOM_SCOPE": "eval::critic",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("core.orchestrator.plan", return_value=PlanValidationResult(tasks=tasks)), \
             mock.patch("core.orchestrator.make_critic", return_value=None), \
             mock.patch(
                 "core.orchestrator.execute_task",
                 side_effect=[
                     CriticEscalation("Critic blocked progress 3 times: unsafe"),
                     TaskResult(id="t3", task="safe path", outcome=TaskOutcome.SUCCESS, result="done"),
                 ],
             ), \
             mock.patch("core.orchestrator.replan", return_value=replacement) as replan, \
             mock.patch(
                 "core.orchestrator.synthesize",
                 return_value=FinalReport(summary="summary", outcome="partial", lessons=()),
             ):
            result = orchestrator.run("critic replan", parallel=False)
            assert replan.called, "critic escalation should trigger replanning"
            assert result["tasks_completed"] == 2, "replacement tasks should execute after replanning"
    return "critic escalations feed back into replanning"


def run_offline_evals() -> dict:
    cases = [
        ("plan_contracts", _case_plan_contracts),
        ("final_report_parsing", _case_final_report_parsing),
        ("final_report_json_contract", _case_final_report_json_contract),
        ("wave_builder", _case_wave_builder),
        ("safety_guards", _case_safety_guards),
        ("safe_skill_runtime", _case_safe_skill_runtime),
        ("memory_versioning", _case_memory_versioning),
        ("chief_of_staff_memory", _case_chief_of_staff_memory),
        ("signal_ingestion", _case_signal_ingestion),
        ("demonstration_learning", _case_demonstration_learning),
        ("demonstration_reliability_ranking", _case_demonstration_reliability_ranking),
        ("browser_demonstration_replay", _case_browser_demonstration_replay),
        ("provider_fallback", _case_provider_fallback),
        ("messaging_dedupe_and_signature", _case_messaging_dedupe_and_signature),
        ("skill_version_rollback", _case_skill_version_rollback),
        ("trace_replay", _case_trace_replay),
        ("plan_quality", _case_plan_quality),
        ("scope_isolation", _case_scope_isolation),
        ("budget_rate_limit", _case_budget_rate_limit),
        ("partial_failure_handling", _case_partial_failure_handling),
        ("critic_replan_feedback", _case_critic_replan_feedback),
    ]
    results = [_run_case(name, fn) for name, fn in cases]
    return {
        "results": results,
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if not result.passed),
    }
