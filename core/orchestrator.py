"""
PHANTOM Orchestrator — the unique intelligence layer.

PHANTOM decomposes a goal, dispatches work to specialist agents, validates
reasoning with a critic, replans after failures, and synthesizes the final
answer.
"""
import ast
import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Any, Iterable

import memory as mem
from core.contracts import (
    AgentRunResult,
    ArtifactRef,
    CriticDecision,
    FinalReport,
    PlanValidationResult,
    RunMetrics,
    TaskExecutionReport,
    TaskOutcome,
    TaskResult,
    TaskSpec,
    ToolExecutionStatus,
    VerificationResult,
    assess_plan_quality,
    lesson_lines,
    normalize_plan_payload,
    renumber_tasks,
)
from core.extensions import extension_context, extension_summary
from core.errors import BudgetExceeded, CriticEscalation
from core.loop import run_agent, run_agent_result
from core.loop import _enforce_budget
from core.observability import TraceRecorder
from core.router import critic_model, execution_model, planning_model, synthesis_model
from core.skill_catalog import bundled_skill_context, bundled_skill_summary
from core.settings import (
    budget_settings,
    prompt_choice,
    prompt_text,
    prompt_user,
    procedure_autoplay_enabled,
    procedure_min_confidence,
    procedure_min_reliability,
    runtime_settings,
    scope_id,
    workspace_root,
)
from tools import _get_tools_with_skills, dispatch_structured


_WORKSPACE_IGNORED_DIRS = {
    ".git",
    ".phantom",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}


def make_critic(goal: str, metrics: RunMetrics):
    """Return a critic function scoped to the current goal."""

    def _critic(reasoning: str) -> CriticDecision:
        heuristic_decision = _maybe_allow_minimal_architecture_reasoning(goal, reasoning)
        if heuristic_decision is not None:
            metrics.note_critic_check(blocked=heuristic_decision.blocks_progress())
            return heuristic_decision
        response = run_agent(
            role="critic",
            model=critic_model(),
            system=(
                "You are a critic reviewing another AI agent's reasoning. "
                "Respond ONLY as JSON: "
                '{"action":"allow|revise|block","issue":"one short sentence","severity":"low|medium|high"}. '
                "Use allow when the reasoning is fine, revise when it should change course, "
                "and block when it is unsafe or clearly invalid."
            ),
            messages=[{
                "role": "user",
                "content": f"Goal: {goal}\n\nAgent reasoning:\n{reasoning}",
            }],
            tools=None,
            max_steps=1,
            metrics=metrics,
        )
        decision = CriticDecision.from_text(response)
        metrics.note_critic_check(blocked=decision.blocks_progress())
        return decision

    return _critic


def _strip_markdown_fences(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _workspace_profile(limit: int = 12) -> dict[str, Any]:
    root = workspace_root()
    try:
        entries = sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except OSError:
        entries = []

    display_entries = []
    for entry in entries[:limit]:
        name = entry.name + ("/" if entry.is_dir() else "")
        display_entries.append(name)

    markers = []
    for name in ("README.md", "pyproject.toml", "requirements.txt", "package.json", ".git"):
        if (root / name).exists():
            markers.append(name)

    python_files: list[str] = []
    truncated = False
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in _WORKSPACE_IGNORED_DIRS)
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                full_path = Path(dirpath) / filename
                try:
                    rel_path = full_path.relative_to(root)
                except ValueError:
                    rel_path = Path(filename)
                python_files.append(str(rel_path))
                if len(python_files) >= limit:
                    truncated = True
                    raise StopIteration
    except StopIteration:
        pass

    visible_entries = [
        entry for entry in entries
        if entry.name not in _WORKSPACE_IGNORED_DIRS and not entry.name.startswith(".")
    ]
    visible_dirs = [entry for entry in visible_entries if entry.is_dir()]
    minimal_single_file = (
        len(python_files) == 1
        and not truncated
        and not visible_dirs
    )

    return {
        "root": root,
        "display_entries": display_entries,
        "markers": markers,
        "python_files": python_files,
        "has_git": (root / ".git").exists(),
        "minimal_single_file": minimal_single_file,
        "truncated": truncated,
    }


def _workspace_summary(limit: int = 12) -> str:
    profile = _workspace_profile(limit=limit)
    root = profile["root"]
    display_entries = profile["display_entries"]
    markers = profile["markers"]
    python_files = profile["python_files"]
    git_line = "git repository detected" if profile["has_git"] else "plain workspace (no .git detected)"
    python_line = ", ".join(python_files) if python_files else "none detected"
    if profile["truncated"]:
        python_line += ", ..."

    lines = [
        f"CURRENT WORKSPACE ROOT: {root}",
        f"TOP-LEVEL ENTRIES: {', '.join(display_entries) if display_entries else '(empty or inaccessible)'}",
        f"WORKSPACE TYPE: {git_line}",
        f"WORKSPACE MARKERS: {', '.join(markers) if markers else 'none detected'}",
        f"PYTHON FILES (sample): {python_line}",
        (
            "GROUNDING: Assume the current workspace is already the primary target unless the user explicitly "
            "asks for a different repository or external resource."
        ),
        (
            "GROUNDING: For repository/codebase analysis, inspect local files first. Do not clone, pull, or fetch "
            "remote repositories unless the workspace is missing the needed content."
        ),
        (
            "GROUNDING: Avoid web search for local repo analysis unless local files clearly do not answer the question."
        ),
        (
            "GROUNDING: If .git is not present, do not plan git-status, branch, commit-history, or repository-metadata tasks."
        ),
    ]
    return "\n".join(lines)


def _is_architecture_review_goal(goal: str) -> bool:
    normalized = goal.lower()
    signals = (
        "architecture",
        "main modules",
        "main module",
        "review this repository",
        "review this workspace",
        "summarize the main modules",
        "explain the architecture",
    )
    return any(signal in normalized for signal in signals)


def _simple_architecture_plan(goal: str) -> PlanValidationResult | None:
    profile = _workspace_profile(limit=8)
    if not _is_architecture_review_goal(goal):
        return None
    if not profile["minimal_single_file"]:
        return None
    python_files = profile["python_files"]
    if not python_files:
        return None
    source_file = python_files[0]
    tasks = (
        TaskSpec(
            id="t1",
            task=f"Inspect the only source file `{source_file}` and identify its entry point, imports, and main control flow.",
            depends_on=(),
            parallel=False,
        ),
        TaskSpec(
            id="t2",
            task=f"Summarize `{source_file}` as a single-file application, including its responsibilities, key functions, and data flow.",
            depends_on=("t1",),
            parallel=False,
        ),
        TaskSpec(
            id="t3",
            task="Write a concise architecture explanation that explicitly notes this workspace is a minimal single-file project rather than a multi-package repository.",
            depends_on=("t2",),
            parallel=False,
        ),
    )
    return PlanValidationResult(tasks=tasks, used_fallback=False)


def _maybe_allow_minimal_architecture_reasoning(goal: str, reasoning: str) -> CriticDecision | None:
    profile = _workspace_profile(limit=4)
    if not (_is_architecture_review_goal(goal) and profile["minimal_single_file"]):
        return None
    normalized = reasoning.lower()
    source_file = profile["python_files"][0].lower() if profile["python_files"] else ""
    mentions_file = bool(source_file and source_file in normalized)
    mentions_single_file = any(
        phrase in normalized
        for phrase in (
            "only one file",
            "single-file",
            "single file",
            "sole module",
            "no additional packages",
            "no other .py files",
            "no other python files",
            "only file discovered",
        )
    )
    mentions_entry_point = any(
        phrase in normalized
        for phrase in (
            "main()",
            "__main__",
            "entry point",
            "if __name__ ==",
            "game loop",
        )
    )
    if mentions_file and mentions_single_file and mentions_entry_point:
        return CriticDecision.allow()
    return None


def _local_single_file_analysis() -> dict[str, Any] | None:
    profile = _workspace_profile(limit=4)
    if not profile["minimal_single_file"] or not profile["python_files"]:
        return None
    source_file = profile["python_files"][0]
    source_path = workspace_root() / source_file
    try:
        code = source_path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None

    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    if tree is not None:
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.extend(f"{module}.{alias.name}".strip(".") for alias in node.names)
            elif isinstance(node, ast.FunctionDef):
                functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)

    normalized = code.lower()
    has_main_guard = "__name__" in normalized and "__main__" in normalized
    uses_input = "input(" in normalized
    uses_print = "print(" in normalized
    uses_random = any(name.split(".")[0] == "random" for name in imports)
    uses_loop = "while " in normalized or "for " in normalized

    responsibilities: list[str] = []
    if uses_random:
        responsibilities.append("generates randomized values")
    if uses_input:
        responsibilities.append("collects user input")
    if uses_print:
        responsibilities.append("prints interactive feedback")
    if uses_loop:
        responsibilities.append("runs a repeated control-flow loop")
    if has_main_guard:
        responsibilities.append("starts from a script entry-point guard")

    return {
        "source_file": source_file,
        "imports": tuple(imports),
        "functions": tuple(functions),
        "classes": tuple(classes),
        "has_main_guard": has_main_guard,
        "responsibilities": tuple(responsibilities),
    }


def _local_single_file_architecture_result(task: TaskSpec, goal: str) -> TaskResult | None:
    if not _is_architecture_review_goal(goal):
        return None
    analysis = _local_single_file_analysis()
    if analysis is None:
        return None

    source_file = analysis["source_file"]
    imports = analysis["imports"]
    functions = analysis["functions"]
    classes = analysis["classes"]
    responsibilities = analysis["responsibilities"]
    has_main_guard = analysis["has_main_guard"]
    task_text = task.task.lower()

    details = [
        f"`{source_file}` is the only Python source file in the workspace.",
        f"Imports: {', '.join(imports) if imports else 'none'}.",
        f"Functions: {', '.join(functions) if functions else 'none detected'}.",
    ]
    if classes:
        details.append(f"Classes: {', '.join(classes)}.")
    if has_main_guard:
        details.append("Execution starts through an `if __name__ == '__main__':` guard.")
    if responsibilities:
        details.append(f"Observed responsibilities: {', '.join(responsibilities)}.")

    if "inspect" in task_text or "identify" in task_text:
        summary = " ".join(details[:4])
    elif "summarize" in task_text or "responsibilities" in task_text or "data flow" in task_text:
        summary = (
            f"`{source_file}` is a single-file application. "
            f"It centers on {', '.join(functions) if functions else 'top-level script logic'}, "
            f"imports {', '.join(imports) if imports else 'no external modules'}, "
            f"and {'uses' if responsibilities else 'does not expose'} "
            f"{', '.join(responsibilities) if responsibilities else 'special runtime patterns'}."
        )
    else:
        summary = (
            f"The workspace has a minimal single-file architecture built around `{source_file}`. "
            f"It imports {', '.join(imports) if imports else 'only built-in behavior'}, "
            f"defines {', '.join(functions) if functions else 'script-level logic'}, "
            f"and {'uses' if has_main_guard else 'does not use'} a standard script entry point. "
            f"Responsibilities include {', '.join(responsibilities) if responsibilities else 'basic script execution'}."
        )

    return TaskResult(
        id=task.id,
        task=task.task,
        outcome=TaskOutcome.SUCCESS,
        result=summary,
        verification=VerificationResult(
            ok=True,
            summary="Local static analysis completed for the single-file workspace.",
        ),
        artifacts=(
            ArtifactRef(
                kind="file",
                label=source_file,
                path=str(workspace_root() / source_file),
            ),
        ),
        details={
            "analysis": {
                "source_file": source_file,
                "imports": list(imports),
                "functions": list(functions),
                "classes": list(classes),
                "has_main_guard": has_main_guard,
                "responsibilities": list(responsibilities),
            },
            "facts": [
                {"key": "python_files", "value": source_file, "confidence": 0.99},
                {"key": "workspace_architecture", "value": "minimal_single_file_python", "confidence": 0.95},
            ],
            "tool_calls": 0,
            "tool_errors": 0,
            "local_analysis": True,
        },
    )


def plan(goal: str, context: str, on_event=None, metrics: RunMetrics | None = None):
    """Return validated task contracts for the requested goal."""
    shortcut = _simple_architecture_plan(goal)
    if shortcut is not None:
        quality = assess_plan_quality(goal, shortcut.tasks)
        if metrics is not None:
            metrics.tasks_planned = len(shortcut.tasks)
            metrics.planner_fallback = shortcut.used_fallback
            metrics.planner_quality_score = quality.score
            metrics.planner_quality_issues = quality.issues
        return shortcut

    skills = mem.list_skills()
    runtime_skill_list = ", ".join(skill["name"] for skill in skills) if skills else "none yet"
    bundled_skills = bundled_skill_summary(limit=8)
    extension_list = extension_summary(limit=8)
    workspace = _workspace_summary()

    prompt = f"""Break this goal into concrete executable tasks.

GOAL: {goal}

CONTEXT:
{context}

WORKSPACE:
{workspace}

RUNTIME EXECUTABLE SKILLS: {runtime_skill_list}

BUNDLED PLAYBOOKS:
{bundled_skills}

ENABLED EXTENSIONS:
{extension_list}

Respond ONLY with a JSON array. Each item:
  "id": "t1" (sequential),
  "task": "specific actionable description",
  "depends_on": [] or ["t1", "t2"],
  "parallel": true if can run alongside other tasks

Keep tasks focused. 3-7 tasks max. No fluff.
Prefer local inspection over remote fetches. If the current workspace already contains a project, do not add clone/pull tasks.
If .git is not present in the workspace markers, do not include git metadata, branch, commit history, or git status tasks.
If the workspace is a minimal single-file project, keep the plan minimal and avoid pretending it is a larger repository.
JSON only, no markdown fences."""

    result = run_agent_result(
        role="planner",
        model=planning_model(),
        system="You are a task planner. Output only valid JSON arrays.",
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_steps=2,
        on_event=on_event,
        metrics=metrics,
    ).final_text

    try:
        payload = json.loads(_strip_markdown_fences(result))
    except Exception:
        payload = None

    validation = normalize_plan_payload(payload, goal)
    quality = assess_plan_quality(goal, validation.tasks)
    if metrics is not None:
        metrics.tasks_planned = len(validation.tasks)
        metrics.planner_fallback = validation.used_fallback
        metrics.planner_quality_score = quality.score
        metrics.planner_quality_issues = quality.issues
    return validation


def replan(
    goal: str,
    context: str,
    completed_results: list[dict],
    pending_tasks: list[TaskSpec],
    next_task_index: int,
    on_event=None,
    metrics: RunMetrics | None = None,
) -> tuple[TaskSpec, ...]:
    """Ask the planner to repair the remaining plan after failures."""
    if not pending_tasks:
        return ()

    completed_text = "\n\n".join(
        f"[{result['id']}] {result['task']}\nResult: {result['result']}"
        for result in completed_results[-8:]
    ) or "No completed task results yet."
    pending_text = "\n".join(f"- {task.id}: {task.task}" for task in pending_tasks)
    workspace = _workspace_summary()

    prompt = f"""You are replanning an in-flight run.

GOAL: {goal}

SHARED CONTEXT:
{context}

WORKSPACE:
{workspace}

COMPLETED RESULTS:
{completed_text}

PENDING TASKS:
{pending_text}

Some completed results may say "Task blocked by critic: ...".
If so, avoid decomposing the remaining work through that blocked approach.
Do not add clone/pull/fetch tasks for the current workspace unless local inspection proved they are necessary.

Respond ONLY as JSON.
If the pending plan is still fine, respond:
{{"action":"continue","reason":"short reason"}}

If the plan should change, respond:
{{"action":"replan","tasks":[{{"id":"t1","task":"...", "depends_on":[], "parallel":false}}]}}
Only include tasks that still need to happen."""

    result = run_agent_result(
        role="planner",
        model=planning_model(),
        system="You are a replanning specialist. Return valid JSON only.",
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_steps=2,
        on_event=on_event,
        metrics=metrics,
    ).final_text

    try:
        payload = json.loads(_strip_markdown_fences(result))
    except Exception:
        return ()
    if not isinstance(payload, dict) or payload.get("action") != "replan":
        return ()

    validation = normalize_plan_payload(payload.get("tasks"), goal)
    tasks = renumber_tasks(validation.tasks, start_index=next_task_index)
    if metrics is not None and tasks:
        metrics.replans += 1
    return tasks


def revise_plan(
    goal: str,
    context: str,
    tasks: list[TaskSpec],
    feedback: str,
    on_event=None,
    metrics: RunMetrics | None = None,
) -> tuple[TaskSpec, ...]:
    """Ask the planner to revise a proposed plan using human feedback."""
    if not tasks:
        return ()

    workspace = _workspace_summary()
    current_plan = "\n".join(
        f"- {task.id}: {task.task} (deps={', '.join(task.depends_on) or 'none'}, parallel={task.parallel})"
        for task in tasks
    )
    prompt = f"""You are revising a proposed plan before execution.

GOAL: {goal}

SHARED CONTEXT:
{context}

WORKSPACE:
{workspace}

CURRENT PLAN:
{current_plan}

HUMAN FEEDBACK:
{feedback}

Revise the plan to satisfy the human feedback while keeping tasks concrete and executable.
Prefer local inspection over remote fetches for the current workspace.
Respond ONLY with a JSON array of tasks:
  "id": "t1"
  "task": "specific actionable description"
  "depends_on": [] or ["t1"]
  "parallel": true|false
JSON only, no markdown fences."""

    result = run_agent_result(
        role="planner",
        model=planning_model(),
        system="You revise task plans. Return valid JSON arrays only.",
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_steps=2,
        on_event=on_event,
        metrics=metrics,
    ).final_text

    try:
        payload = json.loads(_strip_markdown_fences(result))
    except Exception:
        payload = None

    validation = normalize_plan_payload(payload, goal)
    if metrics is not None:
        metrics.tasks_planned += len(validation.tasks)
    return validation.tasks


def _review_plan_with_human(
    goal: str,
    context: str,
    tasks: list[TaskSpec],
    emit,
    metrics: RunMetrics | None = None,
) -> tuple[list[TaskSpec] | None, bool]:
    """
    Return (possibly revised tasks, approved).
    When approved is False, execution should stop before acting.
    """
    revision_budget = max(0, budget_settings().max_replans)
    current_tasks = list(tasks)

    while True:
        emit("plan_approval_required", {
            "goal": goal,
            "tasks": [task.task for task in current_tasks],
            "task_count": len(current_tasks),
            "revision_budget": revision_budget,
        })
        choice = prompt_choice(
            "Plan review: [a]pprove, [r]equest changes, or [c]ancel? [a/r/C]: ",
            {
                "approve": ("a", "yes", "y"),
                "revise": ("r", "edit", "change"),
                "cancel": ("c", "n", "no"),
            },
            default="cancel",
        )
        if choice == "approve":
            emit("plan_approved", {"goal": goal, "task_count": len(current_tasks)})
            return current_tasks, True
        if choice == "cancel":
            emit("plan_declined", {"goal": goal, "task_count": len(current_tasks)})
            return None, False
        if revision_budget <= 0:
            emit("warn", {"message": "Plan revision limit reached; approve or cancel the plan."})
            continue
        feedback = prompt_text("What should change in the plan? ").strip()
        if not feedback:
            emit("warn", {"message": "No plan feedback provided; approve or cancel the plan."})
            continue
        emit("plan_revision_requested", {
            "goal": goal,
            "feedback": feedback,
            "task_count": len(current_tasks),
        })
        revised = revise_plan(goal, context, current_tasks, feedback, on_event=emit, metrics=metrics)
        if revised:
            revision_budget -= 1
            current_tasks = list(revised)
            emit("plan_revised", {
                "tasks": [task.task for task in current_tasks],
                "graph": [{"id": task.id, "depends_on": list(task.depends_on), "parallel": task.parallel} for task in current_tasks],
                "remaining_revisions": revision_budget,
            })
            continue
        emit("warn", {"message": "Planner could not produce a revised plan from the requested changes."})


def _task_execution_verification(tool_results) -> VerificationResult | None:
    results = tuple(tool_results or ())
    if not results:
        return None
    failures = [item for item in results if not item.ok]
    return VerificationResult(
        ok=not failures,
        summary=f"{len(results) - len(failures)}/{len(results)} tool calls succeeded",
        details={"tool_failures": len(failures)},
    )


def _task_artifacts(tool_results):
    return tuple(
        artifact
        for item in tuple(tool_results or ())
        for artifact in item.artifacts
    )


def _select_replayable_procedure(matches):
    if not procedure_autoplay_enabled():
        return None
    min_confidence = procedure_min_confidence()
    min_reliability = procedure_min_reliability()
    for match in matches:
        if (
            match.ready_for_replay
            and match.confidence >= min_confidence
            and match.reliability >= min_reliability
        ):
            return match
    return None


def _execute_procedure_match(match, on_event=None, metrics: RunMetrics | None = None):
    _enforce_budget(metrics)
    if on_event:
        on_event("procedure_selected", {
            "demo_id": match.demo_id,
            "goal": match.goal,
            "confidence": match.confidence,
            "reliability": match.reliability,
            "executable_steps": match.executable_steps,
            "total_steps": match.total_steps,
        })
        on_event("tool", {
            "name": "replay_demonstration",
            "inputs": {"id": match.demo_id, "execute": True, "allow_risky": False},
            "agent": "executor",
        })

    result = dispatch_structured("replay_demonstration", {
        "id": match.demo_id,
        "execute": True,
        "allow_risky": False,
    })
    mem.record_tool("replay_demonstration", failed=not result.ok)
    if metrics is not None:
        metrics.note_tool_call(error=not result.ok)
    if on_event:
        on_event("tool_result", {
            "name": result.name,
            "result": result.output,
            "error": not result.ok,
            "agent": "executor",
        })
    return result


def execute_task(
    task: TaskSpec,
    goal: str,
    context: str,
    dependency_results: dict,
    critic_fn,
    on_event=None,
    metrics: RunMetrics | None = None,
) -> TaskResult:
    """Run a single task with full tool access."""
    local_result = _local_single_file_architecture_result(task, goal)
    if local_result is not None:
        return local_result

    world = mem.world_context(task.task)
    staff_context = mem.chief_of_staff_context(task.task, limit=3)
    procedures = mem.procedure_matches(task.task, limit=2)
    procedure_context = mem.procedure_context(task.task, limit=2, matches=procedures)
    demonstrations = mem.demonstration_context(task.task, limit=2)
    playbook_context = bundled_skill_context(f"{goal}\n{task.task}", limit=2)
    relevant_extensions = extension_context(f"{goal}\n{task.task}", limit=2)
    shared_context = f"SHARED CONTEXT:\n{context}" if context else ""
    workspace_context = "WORKSPACE:\n" + _workspace_summary(limit=16)

    dependency_text = ""
    if dependency_results:
        lines = [f"[{task_id}] {result}" for task_id, result in dependency_results.items()]
        dependency_text = "DEPENDENCY RESULTS:\n" + "\n\n".join(lines)

    selected_procedure = _select_replayable_procedure(procedures)
    procedure_attempt = None
    if selected_procedure is not None:
        procedure_attempt = _execute_procedure_match(selected_procedure, on_event=on_event, metrics=metrics)
        if procedure_attempt.ok and (procedure_attempt.verification is None or procedure_attempt.verification.ok):
            tool_results = (procedure_attempt,)
            return TaskResult(
                id=task.id,
                task=task.task,
                outcome=TaskOutcome.SUCCESS,
                result=f"Reused learned procedure demo #{selected_procedure.demo_id}: {selected_procedure.summary}",
                tool_results=tool_results,
                verification=_task_execution_verification(tool_results),
                artifacts=_task_artifacts(tool_results),
                details={
                    "procedure": selected_procedure.as_dict(),
                    "procedure_replay": "reused",
                    "tool_calls": len(tool_results),
                    "tool_errors": 0,
                    "facts": [],
                },
            )
        if procedure_attempt.status is ToolExecutionStatus.CHECKPOINT_DECLINED:
            tool_results = (procedure_attempt,)
            return TaskResult(
                id=task.id,
                task=task.task,
                outcome=TaskOutcome.CHECKPOINT_DECLINED,
                result=procedure_attempt.summary,
                tool_results=tool_results,
                verification=_task_execution_verification(tool_results),
                artifacts=_task_artifacts(tool_results),
                details={
                    "procedure": selected_procedure.as_dict(),
                    "procedure_replay": "checkpoint_declined",
                    "tool_calls": len(tool_results),
                    "tool_errors": 1,
                    "facts": [],
                },
            )

    sections = [
        f"OVERALL GOAL: {goal}",
        f"YOUR TASK: {task.task}",
        shared_context,
        workspace_context,
        world,
        staff_context,
        playbook_context,
        relevant_extensions,
        procedure_context,
        demonstrations,
        dependency_text,
        (
            "PROCEDURE ATTEMPT:\n"
            f"A direct replay of matched procedure demo #{selected_procedure.demo_id} was attempted first but did not fully complete.\n"
            f"Replay result:\n{procedure_attempt.render()}\n"
            if selected_procedure is not None and procedure_attempt is not None
            else ""
        ),
        (
            "Use tools to accomplish your task. Be precise and efficient.\n"
            "Assume the current workspace is already available locally unless the user explicitly asked for another repository.\n"
            "If a matched procedure is highly relevant, prefer reusing it instead of reinventing the workflow.\n"
            "If a human-taught demonstration looks relevant, you may inspect it with "
            "list_demonstrations/explain_demonstration and replay executable steps with replay_demonstration.\n"
            "For browser-based workflows, prefer browser_workflow or replayed browser demonstration steps "
            "instead of brittle shell scraping.\n"
            "For repository analysis, inspect local files first and avoid unnecessary web search or remote fetches.\n"
            "When done, respond ONLY as JSON with this schema:\n"
            '{"summary":"what you completed","outcome":"success|failed|critic_blocked|budget_exceeded|checkpoint_declined","facts":[{"key":"optional_fact_key","value":"optional_fact_value","confidence":0.8}]}\n'
            "Use facts only for durable reusable discoveries.\n"
            "If you create a skill, explain what it does.\n"
            "If you discover a reusable fact, use the remember tool."
        ),
    ]
    system = "You are an executor agent working on part of a larger goal.\n\n" + "\n\n".join(
        section for section in sections if section
    )

    agent_result = run_agent_result(
        role="executor",
        model=execution_model(),
        system=system,
        messages=[{"role": "user", "content": f"Execute this task: {task.task}"}],
        tools=_get_tools_with_skills(),
        max_steps=15,
        on_event=on_event,
        critic_fn=critic_fn,
        metrics=metrics,
    )
    report = TaskExecutionReport.from_text(agent_result.final_text)
    tool_results = (
        (procedure_attempt,) + agent_result.tool_results
        if procedure_attempt is not None
        else agent_result.tool_results
    )
    return TaskResult(
        id=task.id,
        task=task.task,
        outcome=report.outcome,
        result=report.summary,
        tool_results=tool_results,
        verification=_task_execution_verification(tool_results),
        artifacts=_task_artifacts(tool_results),
        details={
            "stop_reason": agent_result.stop_reason,
            "steps": agent_result.steps,
            "tool_calls": len(tool_results),
            "tool_errors": sum(1 for item in tool_results if not item.ok),
            "facts": [dict(item) for item in report.facts],
            **({"procedure": selected_procedure.as_dict()} if selected_procedure is not None else {}),
            **({"procedure_replay": procedure_attempt.status.value} if procedure_attempt is not None else {}),
        },
    )


def _render_task_result_for_synthesis(result: TaskResult | dict) -> str:
    if isinstance(result, TaskResult):
        return result.render_for_synthesis()
    if isinstance(result, dict):
        raw_outcome = result.get("outcome", TaskOutcome.SUCCESS)
        if isinstance(raw_outcome, TaskOutcome):
            outcome = raw_outcome
        else:
            try:
                outcome = TaskOutcome(str(raw_outcome))
            except ValueError:
                outcome = TaskOutcome.infer(str(result.get("result", "")))
        task_result = TaskResult(
            id=str(result.get("id", "")),
            task=str(result.get("task", "")),
            outcome=outcome,
            result=str(result.get("result", "")),
        )
        return task_result.render_for_synthesis()
    return str(result)


def synthesize(goal: str, task_results: list[TaskResult] | list[dict], on_event=None, metrics: RunMetrics | None = None) -> FinalReport:
    """Combine task results into a final coherent answer."""
    results_text = "\n\n".join(_render_task_result_for_synthesis(result) for result in task_results)

    system = """You are a synthesis agent. Given a goal and the results of multiple executed tasks,
produce a clear, concise final answer.

Respond ONLY as JSON with this schema:
{"summary":"final answer for the user","outcome":"success|failure|partial","lessons":["lesson1","lesson2"]}"""

    result = run_agent_result(
        role="synthesizer",
        model=synthesis_model(),
        system=system,
        messages=[{
            "role": "user",
            "content": f"GOAL: {goal}\n\nTASK RESULTS:\n{results_text}\n\nSynthesize a final answer.",
        }],
        tools=None,
        max_steps=3,
        on_event=on_event,
        metrics=metrics,
    ).final_text
    return FinalReport.from_text(result)


class RunPhase(str, Enum):
    STARTING = "starting"
    CONTEXT = "context"
    PLANNING = "planning"
    APPROVAL = "approval"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    SYNTHESIZING = "synthesizing"
    HALTED = "halted"
    DONE = "done"
    ERROR = "error"


@dataclass
class RunSession:
    goal: str
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    parallel: bool = True
    settings: object = field(init=False)
    recorder: TraceRecorder = field(init=False)
    metrics: RunMetrics = field(init=False)
    phase: RunPhase = field(default=RunPhase.STARTING, init=False)
    context: str = field(default="No prior context.", init=False)
    task_results: list[TaskResult] = field(default_factory=list, init=False)
    completed: dict[str, str] = field(default_factory=dict, init=False)
    task_failures: int = field(default=0, init=False)
    replan_budget: int = field(default=0, init=False)
    next_task_index: int = field(default=1, init=False)
    critic: object | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        mem.init()
        self.settings = runtime_settings()
        self.recorder = TraceRecorder(goal=self.goal)
        self.metrics = RunMetrics(
            goal=self.goal,
            parallel=self.parallel,
            planner_model=planning_model(),
            execution_model=execution_model(),
            critic_model=critic_model(),
            scope=scope_id(),
            trace_id=self.recorder.trace_id,
            secret_provider=self.settings.secrets.provider,
            secret_audit_labels=self.settings.secrets.audit_labels,
        )
        self.replan_budget = budget_settings().max_replans

    def emit(self, event_type: str, data: dict) -> None:
        self.recorder.record(event_type, data, agent=data.get("agent"))
        if self.on_event:
            self.on_event(event_type, data)

    def _load_context(self) -> None:
        self.phase = RunPhase.CONTEXT
        episodes = mem.recall(self.goal)
        world = mem.world_context(self.goal)
        staff_context = mem.chief_of_staff_context(self.goal, limit=4)
        demonstrations = mem.recall_demonstrations(self.goal, limit=2)
        procedures = mem.procedure_matches(self.goal, limit=2)
        procedure_context = mem.procedure_context(self.goal, limit=2, matches=procedures)
        demo_context = mem.demonstration_context(self.goal, limit=2, demonstrations=demonstrations)
        health = mem.tool_health()
        risky = [tool for tool, stats in health.items() if stats["fail_rate"] > 0.3]

        context_parts = []
        if episodes:
            episode_text = "\n".join(
                f"  [{episode['outcome']}] {episode['goal']}: "
                f"{'; '.join(lesson_lines(episode.get('lessons', '[]')))}"
                for episode in episodes
            )
            context_parts.append(f"PAST EXPERIENCE:\n{episode_text}")
        if world:
            context_parts.append(world)
        if staff_context:
            context_parts.append(staff_context)
        if procedure_context:
            context_parts.append(procedure_context)
        if demo_context:
            context_parts.append(demo_context)
        if risky:
            context_parts.append(f"TOOL WARNINGS — high failure rate: {', '.join(risky)}")

        self.context = "\n\n".join(context_parts) or "No prior context."
        self.emit("memory", {
            "episodes": len(episodes),
            "demonstrations": len(demonstrations),
            "demo_ids": [demo["id"] for demo in demonstrations],
            "best_demo_confidence": max((demo.get("confidence", 0.0) for demo in demonstrations), default=0.0),
            "context_size": len(self.context),
        })
        if staff_context:
            briefing = mem.chief_of_staff_briefing(self.goal, limit=3)
            self.emit("briefing", {
                "people": len(briefing["people"]),
                "projects": len(briefing["projects"]),
                "commitments": len(briefing["commitments"]),
                "signals": len(briefing.get("signals", [])),
            })
        if procedures:
            self.emit("procedures", {
                "matches": [item.as_dict() for item in procedures],
            })

    def _planning_phase(self) -> list[TaskSpec]:
        self.phase = RunPhase.PLANNING
        self.emit("planning", {})
        plan_result = plan(self.goal, self.context, on_event=self.emit, metrics=self.metrics)
        tasks = list(plan_result.tasks)
        self.emit("plan_quality", {
            "score": self.metrics.planner_quality_score,
            "issues": list(self.metrics.planner_quality_issues),
        })
        self.emit("plan", {
            "tasks": [task.task for task in tasks],
            "graph": [{"id": task.id, "depends_on": list(task.depends_on), "parallel": task.parallel} for task in tasks],
        })
        return tasks

    def _approval_phase(self, tasks: list[TaskSpec]) -> list[TaskSpec] | None:
        if not self.settings.checkpoints.confirm_plan:
            return tasks
        self.phase = RunPhase.APPROVAL
        reviewed_tasks, approved = _review_plan_with_human(self.goal, self.context, tasks, self.emit, self.metrics)
        if not approved:
            return None
        if reviewed_tasks is not None:
            return list(reviewed_tasks)
        return tasks

    def _safe_execute_task(self, task: TaskSpec) -> TaskResult:
        dependencies = {
            dep_id: self.completed[dep_id]
            for dep_id in task.depends_on
            if dep_id in self.completed
        }
        self.emit("executing", {"task": task.task, "task_id": task.id})
        try:
            return execute_task(task, self.goal, self.context, dependencies, self.critic, self.emit, self.metrics)
        except BudgetExceeded:
            raise
        except CriticEscalation as exc:
            return TaskResult(
                id=task.id,
                task=task.task,
                outcome=TaskOutcome.CRITIC_BLOCKED,
                result=f"Task blocked by critic: {exc}",
            )
        except Exception as exc:
            return TaskResult(
                id=task.id,
                task=task.task,
                outcome=TaskOutcome.FAILED,
                result=f"Task failed: {exc}",
            )

    def _record_task_result(self, result: TaskResult) -> None:
        if result.outcome.needs_replan():
            self.task_failures += 1
        self.completed[result.id] = result.result
        self.task_results.append(result)
        for fact in result.details.get("facts", []):
            mem.learn(
                str(fact.get("key", "")).strip(),
                str(fact.get("value", "")).strip(),
                confidence=float(fact.get("confidence", 0.8)),
                source=f"task:{result.id}",
            )
        self.emit("task_done", {
            "id": result.id,
            "task": result.task,
            "outcome": result.outcome.value,
            "tool_calls": len(result.tool_results),
            "tool_errors": sum(1 for item in result.tool_results if not item.ok),
        })

    def _run_wave(self, current_wave: list[TaskSpec]) -> None:
        self.phase = RunPhase.EXECUTING
        self.metrics.waves += 1
        self.emit("wave", {"tasks": [task.task for task in current_wave]})
        max_workers = min(len(current_wave), budget_settings().max_parallelism)
        if self.parallel and max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(self._safe_execute_task, task): task for task in current_wave}
                for future in as_completed(futures):
                    self._record_task_result(future.result())
        else:
            for task in current_wave:
                self._record_task_result(self._safe_execute_task(task))

    def _execution_phase(self, tasks: list[TaskSpec]) -> FinalReport:
        self.critic = make_critic(self.goal, self.metrics)
        self.next_task_index = len(tasks) + 1
        remaining = list(tasks)

        try:
            while remaining:
                current_wave = _build_waves(
                    remaining,
                    completed_ids=self.completed.keys(),
                    emit_fn=self.emit,
                )[0]
                self._run_wave(current_wave)
                completed_ids = {task.id for task in current_wave}
                remaining = [task for task in remaining if task.id not in completed_ids]

                wave_results = [result for result in self.task_results if result.id in completed_ids]
                if self.replan_budget > 0 and any(result.outcome.needs_replan() for result in wave_results):
                    self.phase = RunPhase.REPLANNING
                    self.emit("replanning", {
                        "reason": "task failure or blocker detected",
                        "remaining_tasks": len(remaining),
                    })
                    replacement = replan(
                        self.goal,
                        self.context,
                        [result.as_dict() for result in self.task_results],
                        remaining,
                        self.next_task_index,
                        on_event=self.emit,
                        metrics=self.metrics,
                    )
                    if replacement:
                        self.replan_budget -= 1
                        self.metrics.tasks_planned += len(replacement)
                        self.next_task_index += len(replacement)
                        remaining = list(replacement)
                        self.emit("replan", {
                            "tasks": [task.task for task in remaining],
                            "graph": [{"id": task.id, "depends_on": list(task.depends_on), "parallel": task.parallel} for task in remaining],
                        })
        except (BudgetExceeded, CriticEscalation) as exc:
            self.phase = RunPhase.HALTED
            self.emit("halted", {"reason": str(exc)})
            lesson = "budget_exceeded" if isinstance(exc, BudgetExceeded) else "critic_escalation"
            return FinalReport(
                summary=f"Run halted: {exc}",
                outcome="partial" if self.task_results else "failure",
                lessons=(lesson,),
            )

        self.phase = RunPhase.SYNTHESIZING
        self.emit("synthesizing", {})
        try:
            return synthesize(self.goal, self.task_results, on_event=self.emit, metrics=self.metrics)
        except (BudgetExceeded, CriticEscalation) as exc:
            return FinalReport(
                summary=f"Execution completed but synthesis failed: {exc}",
                outcome="partial" if self.task_results else "failure",
                lessons=("synthesis_failure",),
            )
        except Exception as exc:
            return FinalReport(
                summary=f"Execution completed but synthesis failed: {exc}",
                outcome="partial" if self.task_results else "failure",
                lessons=("synthesis_error",),
            )

    def _finalize(self, final: FinalReport, *, tasks_completed: int | None = None, task_failures: int | None = None) -> dict:
        completed = len(self.task_results) if tasks_completed is None else tasks_completed
        failures = self.task_failures if task_failures is None else task_failures

        if failures and final.outcome == "success":
            final = FinalReport(
                summary=final.summary,
                outcome="partial",
                lessons=tuple(final.lessons) + (f"task_failures:{failures}",),
            )

        self.phase = RunPhase.DONE if final.outcome != "failure" or completed else RunPhase.ERROR
        self.metrics.finish(final.outcome, completed, failures)
        mem.save_episode(self.goal, final.outcome, final.summary, list(final.lessons))
        mem.save_run(self.goal, final.summary, self.metrics.as_dict())
        for lesson in final.lessons:
            if ":" in lesson:
                key, value = lesson.split(":", 1)
                mem.learn(key.strip(), value.strip(), confidence=0.8)

        self.emit("done", {
            "outcome": final.outcome,
            "summary": final.summary,
            "lessons": list(final.lessons),
            "tasks": completed,
            "metrics": self.metrics.as_dict(),
        })
        return {**final.as_dict(), "tasks_completed": completed, "metrics": self.metrics.as_dict()}

    def run(self) -> dict:
        self.emit("start", {"goal": self.goal, "trace_id": self.recorder.trace_id, "scope": self.metrics.scope})
        self._load_context()

        try:
            tasks = self._planning_phase()
        except (BudgetExceeded, CriticEscalation) as exc:
            final = FinalReport(summary=str(exc), outcome="failure", lessons=("planner_failure",))
            return self._finalize(final, tasks_completed=0, task_failures=1)
        except Exception as exc:
            tb = traceback.format_exc()
            self.emit("planning_error", {"error": str(exc), "traceback": tb})
            final = FinalReport(summary=f"Planning failed: {exc}", outcome="failure", lessons=("planning_error",))
            return self._finalize(final, tasks_completed=0, task_failures=1)

        reviewed_tasks = self._approval_phase(tasks)
        if reviewed_tasks is None:
            final = FinalReport(
                summary="Execution cancelled before any actions were taken: plan approval was declined or unavailable.",
                outcome="failure",
                lessons=("plan_declined",),
            )
            return self._finalize(final, tasks_completed=0, task_failures=0)

        final = self._execution_phase(reviewed_tasks)
        return self._finalize(final)


def run(goal: str, on_event=None, parallel=True) -> dict:
    """
    Full PHANTOM orchestration loop.
    Returns: outcome, summary, lessons, tasks_completed
    """
    return RunSession(goal=goal, on_event=on_event, parallel=parallel).run()



def _build_waves(tasks: list[TaskSpec], completed_ids: Iterable[str] | None = None, emit_fn=None) -> list[list[TaskSpec]]:
    """Topologically sort tasks into waves while respecting serial tasks."""
    completed_ids = set(completed_ids or ())
    remaining = list(tasks)
    waves = []

    while remaining:
        ready = [task for task in remaining if all(dep in completed_ids for dep in task.depends_on)]
        if not ready:
            # No task is ready — dependency cycle or unsatisfiable graph. Warn and break deadlock.
            if emit_fn is not None:
                stuck_ids = [task.id for task in remaining]
                emit_fn("warn", {
                    "message": "Dependency cycle or unsatisfiable graph detected; falling back to sequential order.",
                    "stuck_tasks": stuck_ids,
                })
            ready = [remaining[0]]

        first_serial = next((task for task in ready if not task.parallel), None)
        wave = [first_serial] if first_serial else ready

        waves.append(wave)
        completed_ids.update(task.id for task in wave)
        remaining = [task for task in remaining if task.id not in completed_ids]

    return waves
