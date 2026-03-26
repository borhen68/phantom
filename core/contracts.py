"""Typed contracts and metrics for PHANTOM orchestration."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Iterable, Mapping

VALID_OUTCOMES = {"success", "failure", "partial"}
TASK_ID_RE = re.compile(r"^t\d+$")
CRITIC_ACTIONS = {"allow", "revise", "block"}
RATE_WINDOW_SECONDS = 60


class TaskOutcome(str, Enum):
    """Structured outcome for a single executed task."""
    SUCCESS = "success"
    FAILED = "failed"
    CRITIC_BLOCKED = "critic_blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    CHECKPOINT_DECLINED = "checkpoint_declined"

    def needs_replan(self) -> bool:
        """Return True if this outcome should trigger replanning."""
        return self in (
            TaskOutcome.FAILED,
            TaskOutcome.CRITIC_BLOCKED,
            TaskOutcome.CHECKPOINT_DECLINED,
        )

    @classmethod
    def from_result_text(cls, text: str) -> "TaskOutcome":
        """Infer a task outcome from executor text when no exception was raised."""
        normalized = str(text or "").strip().lower()
        if normalized.startswith("task blocked by critic:"):
            return cls.CRITIC_BLOCKED
        if normalized.startswith("task failed:"):
            return cls.FAILED
        if "checkpoint declined" in normalized or "human checkpoint declined" in normalized:
            return cls.CHECKPOINT_DECLINED
        if "budget exceeded" in normalized:
            return cls.BUDGET_EXCEEDED
        return cls.SUCCESS


@dataclass
class TaskResult:
    """Typed result for a single executed task."""
    id: str
    task: str
    outcome: TaskOutcome
    result: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "outcome": self.outcome.value,
            "result": self.result,
        }


@dataclass(frozen=True)
class TaskSpec:
    id: str
    task: str
    depends_on: tuple[str, ...] = ()
    parallel: bool = False

    @classmethod
    def fallback(cls, goal: str) -> "TaskSpec":
        return cls(id="t1", task=goal.strip() or "Complete the requested goal", depends_on=(), parallel=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "depends_on": list(self.depends_on),
            "parallel": self.parallel,
        }


@dataclass(frozen=True)
class PlanValidationResult:
    tasks: tuple[TaskSpec, ...]
    used_fallback: bool = False


@dataclass(frozen=True)
class PlanQualityReport:
    score: int
    issues: tuple[str, ...] = ()

    def passes(self, threshold: int = 60) -> bool:
        return self.score >= threshold


@dataclass(frozen=True)
class FinalReport:
    summary: str
    outcome: str
    lessons: tuple[str, ...] = ()

    @classmethod
    def from_text(cls, text: str) -> "FinalReport":
        summary = text.strip()
        outcome = "partial"
        lessons: tuple[str, ...] = ()

        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                summary = str(payload.get("summary", "")).strip() or summary
                candidate = str(payload.get("outcome", "partial")).strip().lower()
                if candidate in VALID_OUTCOMES:
                    outcome = candidate
                raw_lessons = payload.get("lessons", [])
                if isinstance(raw_lessons, list):
                    lessons = tuple(str(lesson).strip() for lesson in raw_lessons if str(lesson).strip())
                return cls(summary=summary, outcome=outcome, lessons=lessons)
        except Exception:
            pass

        if "OUTCOME:" in text:
            summary = text.split("OUTCOME:")[0].strip()
            outcome_line = text.split("OUTCOME:")[-1].splitlines()[0].strip().lower()
            if outcome_line in VALID_OUTCOMES:
                outcome = outcome_line

        if "LESSONS:" in text:
            try:
                raw_lessons = json.loads(text.split("LESSONS:")[-1].strip())
                if isinstance(raw_lessons, list):
                    lessons = tuple(str(lesson).strip() for lesson in raw_lessons if str(lesson).strip())
            except Exception:
                lessons = ()

        return cls(summary=summary, outcome=outcome, lessons=lessons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "outcome": self.outcome,
            "lessons": list(self.lessons),
        }


@dataclass(frozen=True)
class CriticDecision:
    action: str
    issue: str = ""
    severity: str = "low"

    @classmethod
    def allow(cls) -> "CriticDecision":
        return cls(action="allow", issue="", severity="low")

    @classmethod
    def from_text(cls, text: str) -> "CriticDecision":
        cleaned = text.strip()
        try:
            payload = json.loads(cleaned)
            if isinstance(payload, dict):
                action = str(payload.get("action", "allow")).strip().lower()
                issue = str(payload.get("issue", "")).strip()
                severity = str(payload.get("severity", "low")).strip().lower()
                if action in CRITIC_ACTIONS:
                    return cls(action=action, issue=issue, severity=severity)
        except Exception:
            pass
        if cleaned.startswith("ISSUE:"):
            return cls(action="revise", issue=cleaned[6:].strip(), severity="medium")
        return cls.allow()

    def requires_revision(self) -> bool:
        return self.action in {"revise", "block"} and bool(self.issue)

    def blocks_progress(self) -> bool:
        return self.action == "block" and bool(self.issue)


@dataclass
class RunMetrics:
    goal: str
    parallel: bool
    planner_model: str
    execution_model: str
    critic_model: str
    scope: str = ""
    trace_id: str = ""
    secret_provider: str = "missing"
    secret_audit_labels: tuple[str, ...] = ()
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    tasks_planned: int = 0
    tasks_completed: int = 0
    task_failures: int = 0
    waves: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    critic_checks: int = 0
    critic_blocks: int = 0
    planner_fallback: bool = False
    planner_quality_score: int = 0
    planner_quality_issues: tuple[str, ...] = ()
    replans: int = 0
    outcome: str = "partial"
    _llm_call_times: list[float] = field(default_factory=list, repr=False)
    _tool_call_times: list[float] = field(default_factory=list, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def _prune_rate_windows(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - RATE_WINDOW_SECONDS
        self._llm_call_times = [stamp for stamp in self._llm_call_times if stamp >= cutoff]
        self._tool_call_times = [stamp for stamp in self._tool_call_times if stamp >= cutoff]

    def note_llm_call(self) -> None:
        with self._lock:
            self.llm_calls += 1
            self._llm_call_times.append(time.time())
            self._prune_rate_windows()

    def note_tool_call(self, error: bool) -> None:
        with self._lock:
            self.tool_calls += 1
            self._tool_call_times.append(time.time())
            if error:
                self.tool_errors += 1
            self._prune_rate_windows()

    def note_token_usage(self, input_tokens: int = 0, output_tokens: int = 0, estimated_cost: float | None = None) -> None:
        with self._lock:
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)
            if estimated_cost is not None:
                if self.estimated_cost_usd is None:
                    self.estimated_cost_usd = 0.0
                self.estimated_cost_usd += estimated_cost

    def note_critic_check(self, blocked: bool) -> None:
        with self._lock:
            self.critic_checks += 1
            if blocked:
                self.critic_blocks += 1

    def finish(self, outcome: str, tasks_completed: int, task_failures: int) -> None:
        with self._lock:
            self.ended_at = time.time()
            self.outcome = outcome if outcome in VALID_OUTCOMES else "partial"
            self.tasks_completed = tasks_completed
            self.task_failures = task_failures

    def recent_llm_calls(self, window_seconds: int = RATE_WINDOW_SECONDS) -> int:
        with self._lock:
            self._prune_rate_windows()
            cutoff = time.time() - window_seconds
            return sum(1 for stamp in self._llm_call_times if stamp >= cutoff)

    def recent_tool_calls(self, window_seconds: int = RATE_WINDOW_SECONDS) -> int:
        with self._lock:
            self._prune_rate_windows()
            cutoff = time.time() - window_seconds
            return sum(1 for stamp in self._tool_call_times if stamp >= cutoff)

    @property
    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.time()
        return int((end - self.started_at) * 1000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "parallel": self.parallel,
            "planner_model": self.planner_model,
            "execution_model": self.execution_model,
            "critic_model": self.critic_model,
            "tasks_planned": self.tasks_planned,
            "tasks_completed": self.tasks_completed,
            "task_failures": self.task_failures,
            "waves": self.waves,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "critic_checks": self.critic_checks,
            "critic_blocks": self.critic_blocks,
            "planner_fallback": self.planner_fallback,
            "planner_quality_score": self.planner_quality_score,
            "planner_quality_issues": list(self.planner_quality_issues),
            "replans": self.replans,
            "scope": self.scope,
            "trace_id": self.trace_id,
            "secret_provider": self.secret_provider,
            "secret_audit_labels": list(self.secret_audit_labels),
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
        }


def normalize_plan_payload(payload: Any, goal: str) -> PlanValidationResult:
    if not isinstance(payload, list):
        return PlanValidationResult(tasks=(TaskSpec.fallback(goal),), used_fallback=True)

    used_fallback = False
    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()

    for index, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping):
            used_fallback = True
            continue

        task_text = str(item.get("task", "")).strip()
        if not task_text:
            used_fallback = True
            continue

        raw_id = str(item.get("id", "")).strip()
        task_id = raw_id if TASK_ID_RE.fullmatch(raw_id) and raw_id not in seen_ids else f"t{index}"
        if task_id in seen_ids:
            task_id = f"t{len(tasks) + 1}"
        if task_id != raw_id:
            used_fallback = True

        raw_dependencies = item.get("depends_on", [])
        if raw_dependencies is None:
            raw_dependencies = []
        if not isinstance(raw_dependencies, list):
            raw_dependencies = [raw_dependencies]
            used_fallback = True

        depends_on = tuple(str(dep).strip() for dep in raw_dependencies if str(dep).strip())
        task = TaskSpec(
            id=task_id,
            task=task_text,
            depends_on=depends_on,
            parallel=bool(item.get("parallel", False)),
        )
        tasks.append(task)
        seen_ids.add(task.id)

    if not tasks:
        return PlanValidationResult(tasks=(TaskSpec.fallback(goal),), used_fallback=True)

    valid_ids = {task.id for task in tasks}
    normalized: list[TaskSpec] = []
    for task in tasks:
        cleaned_dependencies = tuple(
            dep for dep in task.depends_on
            if dep in valid_ids and dep != task.id
        )
        if cleaned_dependencies != task.depends_on:
            used_fallback = True
        normalized.append(TaskSpec(
            id=task.id,
            task=task.task,
            depends_on=cleaned_dependencies,
            parallel=task.parallel,
        ))

    return PlanValidationResult(tasks=tuple(normalized), used_fallback=used_fallback)


def assess_plan_quality(goal: str, tasks: Iterable[TaskSpec]) -> PlanQualityReport:
    items = list(tasks)
    issues: list[str] = []
    score = 100
    goal_text = re.sub(r"\W+", " ", goal.lower()).strip()
    normalized_tasks = [re.sub(r"\W+", " ", task.task.lower()).strip() for task in items]

    if not 2 <= len(items) <= 7:
        issues.append("task_count_out_of_range")
        score -= 20
    if len(set(normalized_tasks)) != len(normalized_tasks):
        issues.append("duplicate_tasks")
        score -= 25
    if any(len(task.task.strip()) < 12 for task in items):
        issues.append("tasks_too_brief")
        score -= 10
    if normalized_tasks and all(task == goal_text for task in normalized_tasks):
        issues.append("tasks_repeat_goal")
        score -= 35
    if items and not any(task.depends_on for task in items) and len(items) > 4:
        issues.append("no_dependencies_in_large_plan")
        score -= 10
    if any(len(set(task.depends_on)) != len(task.depends_on) for task in items):
        issues.append("duplicate_dependencies")
        score -= 10

    return PlanQualityReport(score=max(0, score), issues=tuple(issues))


def renumber_tasks(tasks: Iterable[TaskSpec], start_index: int = 1) -> tuple[TaskSpec, ...]:
    ordered = list(tasks)
    id_map = {task.id: f"t{start_index + idx}" for idx, task in enumerate(ordered)}
    return tuple(
        TaskSpec(
            id=id_map[task.id],
            task=task.task,
            depends_on=tuple(id_map[dep] for dep in task.depends_on if dep in id_map),
            parallel=task.parallel,
        )
        for task in ordered
    )


def lesson_lines(lessons: Iterable[str] | str) -> list[str]:
    if isinstance(lessons, str):
        try:
            parsed = json.loads(lessons)
        except Exception:
            return []
        lessons = parsed
    return [str(lesson).strip() for lesson in lessons if str(lesson).strip()]
