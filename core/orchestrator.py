"""
PHANTOM Orchestrator — the unique intelligence layer.

PHANTOM decomposes a goal, dispatches work to specialist agents, validates
reasoning with a critic, replans after failures, and synthesizes the final
answer.
"""
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import memory as mem
from core.contracts import (
    CriticDecision,
    FinalReport,
    RunMetrics,
    TaskOutcome,
    TaskResult,
    TaskSpec,
    assess_plan_quality,
    lesson_lines,
    normalize_plan_payload,
    renumber_tasks,
)
from core.errors import BudgetExceeded, CriticEscalation
from core.loop import run_agent
from core.observability import TraceRecorder
from core.router import critic_model, execution_model, planning_model, synthesis_model
from core.settings import budget_settings, prompt_user, runtime_settings, scope_id, workspace_root
from tools import _get_tools_with_skills


def make_critic(goal: str, metrics: RunMetrics):
    """Return a critic function scoped to the current goal."""

    def _critic(reasoning: str) -> CriticDecision:
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


def _workspace_summary(limit: int = 12) -> str:
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

    lines = [
        f"CURRENT WORKSPACE ROOT: {root}",
        f"TOP-LEVEL ENTRIES: {', '.join(display_entries) if display_entries else '(empty or inaccessible)'}",
        f"WORKSPACE MARKERS: {', '.join(markers) if markers else 'none detected'}",
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
    ]
    return "\n".join(lines)


def plan(goal: str, context: str, on_event=None, metrics: RunMetrics | None = None):
    """Return validated task contracts for the requested goal."""
    skills = mem.list_skills()
    skill_list = ", ".join(skill["name"] for skill in skills) if skills else "none yet"
    workspace = _workspace_summary()

    prompt = f"""Break this goal into concrete executable tasks.

GOAL: {goal}

CONTEXT:
{context}

WORKSPACE:
{workspace}

AVAILABLE SKILLS: {skill_list}

Respond ONLY with a JSON array. Each item:
  "id": "t1" (sequential),
  "task": "specific actionable description",
  "depends_on": [] or ["t1", "t2"],
  "parallel": true if can run alongside other tasks

Keep tasks focused. 3-7 tasks max. No fluff.
Prefer local inspection over remote fetches. If the current workspace already contains a project, do not add clone/pull tasks.
JSON only, no markdown fences."""

    result = run_agent(
        role="planner",
        model=planning_model(),
        system="You are a task planner. Output only valid JSON arrays.",
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_steps=2,
        on_event=on_event,
        metrics=metrics,
    )

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

    result = run_agent(
        role="planner",
        model=planning_model(),
        system="You are a replanning specialist. Return valid JSON only.",
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        max_steps=2,
        on_event=on_event,
        metrics=metrics,
    )

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


def execute_task(
    task: TaskSpec,
    goal: str,
    context: str,
    dependency_results: dict,
    critic_fn,
    on_event=None,
    metrics: RunMetrics | None = None,
) -> str:
    """Run a single task with full tool access."""
    world = mem.world_context(task.task)
    demonstrations = mem.demonstration_context(task.task, limit=2)
    shared_context = f"SHARED CONTEXT:\n{context}" if context else ""
    workspace_context = "WORKSPACE:\n" + _workspace_summary(limit=16)

    dependency_text = ""
    if dependency_results:
        lines = [f"[{task_id}] {result}" for task_id, result in dependency_results.items()]
        dependency_text = "DEPENDENCY RESULTS:\n" + "\n\n".join(lines)

    sections = [
        f"OVERALL GOAL: {goal}",
        f"YOUR TASK: {task.task}",
        shared_context,
        workspace_context,
        world,
        demonstrations,
        dependency_text,
        (
            "Use tools to accomplish your task. Be precise and efficient.\n"
            "Assume the current workspace is already available locally unless the user explicitly asked for another repository.\n"
            "If a human-taught demonstration looks relevant, you may inspect it with "
            "list_demonstrations/explain_demonstration and replay executable steps with replay_demonstration.\n"
            "For browser-based workflows, prefer browser_workflow or replayed browser demonstration steps "
            "instead of brittle shell scraping.\n"
            "For repository analysis, inspect local files first and avoid unnecessary web search or remote fetches.\n"
            "When done, summarize what you did and any important findings.\n"
            "If you create a skill, explain what it does.\n"
            "If you discover a reusable fact, use the remember tool."
        ),
    ]
    system = "You are an executor agent working on part of a larger goal.\n\n" + "\n\n".join(
        section for section in sections if section
    )

    return run_agent(
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


def synthesize(goal: str, task_results: list[dict], on_event=None, metrics: RunMetrics | None = None) -> FinalReport:
    """Combine task results into a final coherent answer."""
    results_text = "\n\n".join(
        f"[{result['id']}] {result['task']}\nResult: {result['result']}"
        for result in task_results
    )

    system = """You are a synthesis agent. Given a goal and the results of multiple executed tasks,
produce a clear, concise final answer.

Respond ONLY as JSON with this schema:
{"summary":"final answer for the user","outcome":"success|failure|partial","lessons":["lesson1","lesson2"]}"""

    result = run_agent(
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
    )
    return FinalReport.from_text(result)


def run(goal: str, on_event=None, parallel=True) -> dict:
    """
    Full PHANTOM orchestration loop.
    Returns: outcome, summary, lessons, tasks_completed
    """
    mem.init()
    settings = runtime_settings()
    recorder = TraceRecorder(goal=goal)
    metrics = RunMetrics(
        goal=goal,
        parallel=parallel,
        planner_model=planning_model(),
        execution_model=execution_model(),
        critic_model=critic_model(),
        scope=scope_id(),
        trace_id=recorder.trace_id,
        secret_provider=settings.secrets.provider,
        secret_audit_labels=settings.secrets.audit_labels,
    )

    def emit(event_type, data):
        recorder.record(event_type, data, agent=data.get("agent"))
        if on_event:
            on_event(event_type, data)

    emit("start", {"goal": goal, "trace_id": recorder.trace_id, "scope": metrics.scope})

    episodes = mem.recall(goal)
    world = mem.world_context(goal)
    demonstrations = mem.recall_demonstrations(goal, limit=2)
    demo_context = mem.demonstration_context(goal, limit=2, demonstrations=demonstrations)
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
    if demo_context:
        context_parts.append(demo_context)
    if risky:
        context_parts.append(f"TOOL WARNINGS — high failure rate: {', '.join(risky)}")

    context = "\n\n".join(context_parts) or "No prior context."
    emit("memory", {
        "episodes": len(episodes),
        "demonstrations": len(demonstrations),
        "demo_ids": [demo["id"] for demo in demonstrations],
        "best_demo_confidence": max((demo.get("confidence", 0.0) for demo in demonstrations), default=0.0),
        "context_size": len(context),
    })

    try:
        emit("planning", {})
        plan_result = plan(goal, context, on_event=emit, metrics=metrics)
        tasks = list(plan_result.tasks)
        emit("plan_quality", {
            "score": metrics.planner_quality_score,
            "issues": list(metrics.planner_quality_issues),
        })
    except (BudgetExceeded, CriticEscalation) as exc:
        final = FinalReport(summary=str(exc), outcome="failure", lessons=("planner_failure",))
        metrics.finish(final.outcome, 0, 1)
        mem.save_episode(goal, final.outcome, final.summary, list(final.lessons))
        mem.save_run(goal, final.summary, metrics.as_dict())
        emit("done", {
            "outcome": final.outcome,
            "summary": final.summary,
            "lessons": list(final.lessons),
            "tasks": 0,
            "metrics": metrics.as_dict(),
        })
        return {**final.as_dict(), "tasks_completed": 0, "metrics": metrics.as_dict()}
    except Exception as exc:
        tb = traceback.format_exc()
        emit("planning_error", {"error": str(exc), "traceback": tb})
        final = FinalReport(summary=f"Planning failed: {exc}", outcome="failure", lessons=("planning_error",))
        metrics.finish(final.outcome, 0, 1)
        mem.save_episode(goal, final.outcome, final.summary, list(final.lessons))
        mem.save_run(goal, final.summary, metrics.as_dict())
        emit("done", {
            "outcome": final.outcome,
            "summary": final.summary,
            "lessons": list(final.lessons),
            "tasks": 0,
            "metrics": metrics.as_dict(),
        })
        return {**final.as_dict(), "tasks_completed": 0, "metrics": metrics.as_dict()}

    emit("plan", {
        "tasks": [task.task for task in tasks],
        "graph": [{"id": task.id, "depends_on": list(task.depends_on), "parallel": task.parallel} for task in tasks],
    })

    if settings.checkpoints.confirm_plan:
        emit("plan_approval_required", {
            "goal": goal,
            "tasks": [task.task for task in tasks],
            "task_count": len(tasks),
        })
        approved = prompt_user(f"Approve PHANTOM plan with {len(tasks)} task(s) for goal '{goal[:80]}'?")
        if not approved:
            emit("plan_declined", {"goal": goal, "task_count": len(tasks)})
            final = FinalReport(
                summary="Execution cancelled before any actions were taken: plan approval was declined or unavailable.",
                outcome="failure",
                lessons=("plan_declined",),
            )
            metrics.finish(final.outcome, 0, 0)
            mem.save_episode(goal, final.outcome, final.summary, list(final.lessons))
            mem.save_run(goal, final.summary, metrics.as_dict())
            emit("done", {
                "outcome": final.outcome,
                "summary": final.summary,
                "lessons": list(final.lessons),
                "tasks": 0,
                "metrics": metrics.as_dict(),
            })
            return {**final.as_dict(), "tasks_completed": 0, "metrics": metrics.as_dict()}
        emit("plan_approved", {"goal": goal, "task_count": len(tasks)})

    critic = make_critic(goal, metrics)
    task_results: list[TaskResult] = []
    completed = {}
    task_failures = 0
    next_task_index = len(tasks) + 1
    remaining = list(tasks)
    replan_budget = budget_settings().max_replans

    def run_wave(current_wave: list[TaskSpec]):
        nonlocal task_failures
        metrics.waves += 1
        emit("wave", {"tasks": [task.task for task in current_wave]})
        max_workers = min(len(current_wave), budget_settings().max_parallelism)

        def _make_task_result(task: TaskSpec, text: str, outcome: TaskOutcome) -> TaskResult:
            return TaskResult(id=task.id, task=task.task, outcome=outcome, result=text)

        if parallel and max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {}
                for task in current_wave:
                    dependencies = {
                        dep_id: completed[dep_id]
                        for dep_id in task.depends_on
                        if dep_id in completed
                    }
                    emit("executing", {"task": task.task, "task_id": task.id})
                    future = pool.submit(
                        execute_task,
                        task,
                        goal,
                        context,
                        dependencies,
                        critic,
                        emit,
                        metrics,
                    )
                    futures[future] = task

                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        text = future.result()
                        outcome = TaskOutcome.from_result_text(text)
                    except BudgetExceeded:
                        raise
                    except CriticEscalation as exc:
                        text = f"Task blocked by critic: {exc}"
                        outcome = TaskOutcome.CRITIC_BLOCKED
                    except Exception as exc:
                        text = f"Task failed: {exc}"
                        outcome = TaskOutcome.FAILED
                    if outcome.needs_replan():
                        task_failures += 1
                    tr = _make_task_result(task, text, outcome)
                    completed[task.id] = tr.result
                    task_results.append(tr)
                    emit("task_done", {"id": task.id, "task": task.task, "outcome": outcome.value})
        else:
            for task in current_wave:
                dependencies = {
                    dep_id: completed[dep_id]
                    for dep_id in task.depends_on
                    if dep_id in completed
                }
                emit("executing", {"task": task.task, "task_id": task.id})
                try:
                    text = execute_task(task, goal, context, dependencies, critic, emit, metrics)
                    outcome = TaskOutcome.from_result_text(text)
                except BudgetExceeded:
                    raise
                except CriticEscalation as exc:
                    text = f"Task blocked by critic: {exc}"
                    outcome = TaskOutcome.CRITIC_BLOCKED
                except Exception as exc:
                    text = f"Task failed: {exc}"
                    outcome = TaskOutcome.FAILED
                if outcome.needs_replan():
                    task_failures += 1
                tr = _make_task_result(task, text, outcome)
                completed[task.id] = tr.result
                task_results.append(tr)
                emit("task_done", {"id": task.id, "task": task.task, "outcome": outcome.value})

    execution_stop = None
    try:
        while remaining:
            current_wave = _build_waves(remaining, emit_fn=emit)[0]
            run_wave(current_wave)
            completed_ids = {task.id for task in current_wave}
            remaining = [task for task in remaining if task.id not in completed_ids]

            wave_results = [tr for tr in task_results if tr.id in completed_ids]
            if replan_budget > 0 and any(tr.outcome.needs_replan() for tr in wave_results):
                emit("replanning", {"reason": "task failure or blocker detected", "remaining_tasks": len(remaining)})
                replacement = replan(
                    goal,
                    context,
                    [tr.as_dict() for tr in task_results],
                    remaining,
                    next_task_index,
                    on_event=emit,
                    metrics=metrics,
                )
                if replacement:
                    replan_budget -= 1
                    metrics.tasks_planned += len(replacement)
                    next_task_index += len(replacement)
                    remaining = list(replacement)
                    emit("replan", {
                        "tasks": [task.task for task in remaining],
                        "graph": [{"id": task.id, "depends_on": list(task.depends_on), "parallel": task.parallel} for task in remaining],
                    })
    except (BudgetExceeded, CriticEscalation) as exc:
        execution_stop = exc
        emit("halted", {"reason": str(exc)})

    if execution_stop is not None:
        lesson = "budget_exceeded" if isinstance(execution_stop, BudgetExceeded) else "critic_escalation"
        final = FinalReport(
            summary=f"Run halted: {execution_stop}",
            outcome="partial" if task_results else "failure",
            lessons=(lesson,),
        )
    else:
        emit("synthesizing", {})
        try:
            final = synthesize(goal, [tr.as_dict() for tr in task_results], on_event=emit, metrics=metrics)
        except (BudgetExceeded, CriticEscalation) as exc:
            final = FinalReport(
                summary=f"Execution completed but synthesis failed: {exc}",
                outcome="partial" if task_results else "failure",
                lessons=("synthesis_failure",),
            )
        except Exception as exc:
            final = FinalReport(
                summary=f"Execution completed but synthesis failed: {exc}",
                outcome="partial" if task_results else "failure",
                lessons=("synthesis_error",),
            )

    if task_failures and final.outcome == "success":
        final = FinalReport(
            summary=final.summary,
            outcome="partial",
            lessons=tuple(final.lessons) + (f"task_failures:{task_failures}",),
        )

    metrics.finish(final.outcome, len(task_results), task_failures)
    mem.save_episode(goal, final.outcome, final.summary, list(final.lessons))
    mem.save_run(goal, final.summary, metrics.as_dict())
    for lesson in final.lessons:
        if ":" in lesson:
            key, value = lesson.split(":", 1)
            mem.learn(key.strip(), value.strip(), confidence=0.8)

    emit("done", {
        "outcome": final.outcome,
        "summary": final.summary,
        "lessons": list(final.lessons),
        "tasks": len(task_results),
        "metrics": metrics.as_dict(),
    })

    return {**final.as_dict(), "tasks_completed": len(task_results), "metrics": metrics.as_dict()}



def _build_waves(tasks: list[TaskSpec], emit_fn=None) -> list[list[TaskSpec]]:
    """Topologically sort tasks into waves while respecting serial tasks."""
    completed_ids = set()
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
