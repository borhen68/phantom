#!/usr/bin/env python3
"""
PHANTOM — Autonomous AI Agent
More powerful than OpenClaw, Nanobot, and Claude Code combined.

Usage:
  python phantom.py "your goal"
  python phantom.py --no-parallel "goal"  # sequential mode
  python phantom.py --memory              # inspect memory
  python phantom.py --skills              # list created skills
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

from core.souls import soul_for
from core.settings import prompt_choice

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich import box
except ModuleNotFoundError:
    def _strip_markup(text):
        return re.sub(r"\[/?[^\]]+\]", "", str(text))


    class Console:
        def print(self, *args, **kwargs):
            print(" ".join(_strip_markup(arg) for arg in args))


    class Panel:
        def __init__(self, renderable, title=None, subtitle=None, border_style=None):
            self.renderable = renderable
            self.title = title
            self.subtitle = subtitle

        def __str__(self):
            lines = []
            if self.title:
                lines.append(_strip_markup(self.title))
            lines.append(_strip_markup(self.renderable))
            if self.subtitle:
                lines.append(_strip_markup(self.subtitle))
            return "\n".join(lines)


    class Rule:
        def __init__(self, style=None):
            self.style = style

        def __str__(self):
            return "-" * 60


    class Table:
        def __init__(self, box=None, show_header=True, header_style=None):
            self.columns = []
            self.rows = []
            self.show_header = show_header

        def add_column(self, name, **kwargs):
            self.columns.append(_strip_markup(name))

        def add_row(self, *values):
            self.rows.append([_strip_markup(value) for value in values])

        def __str__(self):
            lines = []
            if self.show_header and self.columns:
                lines.append(" | ".join(self.columns))
            lines.extend(" | ".join(row) for row in self.rows)
            return "\n".join(lines)


    class box:
        SIMPLE = None

console = Console()
_current_agent = "phantom"


def resolve_goal(goal: str | None) -> str | None:
    text = str(goal or "").strip()
    if text:
        return text
    if not sys.stdin or not sys.stdin.isatty():
        return None
    console.print("[cyan]What do you want PHANTOM to do?[/cyan]")
    try:
        entered = input("> ").strip()
    except EOFError:
        return None
    return entered or None


def _stdin_is_tty() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _fmt_ts(value) -> str:
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _agent_label(agent: str) -> str:
    soul = soul_for(agent)
    if soul.role == agent:
        return soul.name
    return agent


def _agent_color(agent: str) -> str:
    return soul_for(agent).color


def handle(event_type: str, data: dict):
    global _current_agent
    agent = data.get("agent", _current_agent)
    color = _agent_color(agent)
    label = _agent_label(agent)

    if event_type == "start":
        console.print()
        console.print(Panel(
            f"[bold white]{data['goal']}[/bold white]",
            title="[cyan bold]PHANTOM[/cyan bold]",
            subtitle=(
                "[dim]autonomous · self-improving · multi-agent[/dim]"
                f"\n[dim]trace={data.get('trace_id', 'n/a')} · scope={data.get('scope', 'default')}[/dim]"
            ),
            border_style="cyan"
        ))

    elif event_type == "memory":
        n = data.get("episodes", 0)
        demos = data.get("demonstrations", 0)
        parts = []
        if n:
            parts.append(f"{n} relevant memories")
        if demos:
            best = data.get("best_demo_confidence", 0.0)
            parts.append(f"{demos} human demonstrations (best {best:.2f})")
        if parts:
            console.print(f"[dim]↺  {' + '.join(parts)} loaded[/dim]")

    elif event_type == "briefing":
        console.print(
            "[dim]↺ chief-of-staff memory:[/dim] "
            f"[dim]{data.get('people', 0)} people · {data.get('projects', 0)} projects · {data.get('commitments', 0)} commitments · {data.get('signals', 0)} signals[/dim]"
        )

    elif event_type == "procedures":
        matches = data.get("matches", [])
        if matches:
            console.print("[dim]↺ matched procedures:[/dim]")
            for match in matches[:3]:
                readiness = f"{match.get('executable_steps', 0)}/{match.get('total_steps', 0)}"
                console.print(
                    "  [dim]"
                    f"demo #{match.get('demo_id')} · {match.get('goal', '')[:52]} · "
                    f"confidence={match.get('confidence', 0.0):.2f} · "
                    f"reliability={match.get('reliability', 0.0):.2f} · "
                    f"replay={readiness}"
                    "[/dim]"
                )

    elif event_type == "procedure_selected":
        readiness = f"{data.get('executable_steps', 0)}/{data.get('total_steps', 0)}"
        console.print(
            "[cyan]★ Reusing learned procedure:[/cyan] "
            f"[dim]demo #{data.get('demo_id')} · confidence={data.get('confidence', 0.0):.2f} · "
            f"reliability={data.get('reliability', 0.0):.2f} · replay={readiness}[/dim]"
        )

    elif event_type == "planning":
        console.print(f"\n[blue]◈ {label} is planning...[/blue]")

    elif event_type == "plan":
        tasks = data.get("tasks", [])
        console.print(f"[blue]◈ Plan:[/blue] {len(tasks)} tasks")
        for i, t in enumerate(tasks, 1):
            console.print(f"  [dim]{i}. {t[:80]}[/dim]")
        graph = data.get("graph", [])
        if graph:
            console.print("[dim]Dependency graph:[/dim]")
            for node in graph:
                deps = ", ".join(node.get("depends_on", [])) or "none"
                mode = "parallel" if node.get("parallel") else "serial"
                console.print(f"  [dim]{node['id']} → deps={deps} · {mode}[/dim]")

    elif event_type == "plan_approval_required":
        revisions = data.get("revision_budget")
        extra = f" · edits left={revisions}" if revisions is not None else ""
        console.print(f"[yellow]◈ Waiting for plan approval:[/yellow] [dim]{data.get('task_count', 0)} tasks{extra}[/dim]")

    elif event_type == "plan_approved":
        console.print(f"[green]✓ Plan approved[/green] [dim]starting execution[/dim]")

    elif event_type == "plan_declined":
        console.print(f"[yellow]■ Plan declined[/yellow] [dim]no actions were taken[/dim]")

    elif event_type == "plan_revision_requested":
        console.print(f"[yellow]↺ Requested plan changes:[/yellow] [dim]{data.get('feedback', '')[:120]}[/dim]")

    elif event_type == "plan_revised":
        tasks = data.get("tasks", [])
        console.print(f"[yellow]↺ Revised plan:[/yellow] [dim]{len(tasks)} tasks[/dim]")
        for i, t in enumerate(tasks, 1):
            console.print(f"  [dim]{i}. {t[:80]}[/dim]")

    elif event_type == "wave":
        tasks = data.get("tasks", [])
        if len(tasks) > 1:
            console.print(f"\n[cyan]⟳  Parallel wave: {len(tasks)} tasks[/cyan]")

    elif event_type == "executing":
        console.print(f"\n[green]▶ {label} executing:[/green] [dim]{data.get('task','')[:70]}[/dim]")

    elif event_type == "task_done":
        outcome = data.get("outcome", "success")
        icon = {
            "success": "✓",
            "failed": "✗",
            "critic_blocked": "■",
            "budget_exceeded": "■",
            "checkpoint_declined": "■",
        }.get(outcome, "•")
        color = {
            "success": "green",
            "failed": "red",
            "critic_blocked": "yellow",
            "budget_exceeded": "yellow",
            "checkpoint_declined": "yellow",
        }.get(outcome, "white")
        suffix = "" if outcome == "success" else f" [dim]({outcome})[/dim]"
        console.print(f"[{color}]{icon}[/{color}] [dim]{data.get('task','')[:70]}[/dim]{suffix}")

    elif event_type == "synthesizing":
        console.print(f"\n[magenta]◈ {label} is synthesizing results...[/magenta]")

    elif event_type == "step":
        pass  # Too noisy for CLI

    elif event_type == "usage":
        pass  # captured in metrics summary

    elif event_type == "replanning":
        console.print(f"\n[yellow]↺ {label} is replanning:[/yellow] [dim]{data.get('reason', '')}[/dim]")

    elif event_type == "replan":
        tasks = data.get("tasks", [])
        console.print(f"[yellow]↺ New plan:[/yellow] {len(tasks)} tasks")
        for i, t in enumerate(tasks, 1):
            console.print(f"  [dim]{i}. {t[:80]}[/dim]")

    elif event_type == "plan_quality":
        score = data.get("score", 0)
        issues = ", ".join(data.get("issues", [])) or "none"
        color = "green" if score >= 80 else "yellow" if score >= 60 else "red"
        console.print(f"[{color}]◈ Plan quality: {score}/100[/{color}] [dim]issues={issues}[/dim]")

    elif event_type == "halted":
        console.print(f"\n[red]■ Halted:[/red] [dim]{data.get('reason', '')}[/dim]")

    elif event_type == "warn":
        console.print(f"[yellow]⚠ warn:[/yellow] [dim]{data.get('message', '')}[/dim]")

    elif event_type == "planning_error":
        console.print(f"[red]✗ planning_error:[/red] [dim]{data.get('error', '')}[/dim]")

    elif event_type == "soul":
        intro = data.get("intro", "").strip()
        if intro:
            console.print(f"[{color}]{label}[/{color}] [dim]{intro[:160]}[/dim]")

    elif event_type == "text":
        text = data.get("text", "")
        if agent == "synthesizer" and text.lstrip().startswith("{"):
            return
        # Filter out internal markers
        for marker in ["OUTCOME:", "LESSONS:"]:
            if marker in text:
                text = text.split(marker)[0]
        text = text.strip()
        if text and len(text) > 10:
            console.print(f"[{color}][{label}][/{color}] {text[:300]}")

    elif event_type == "critic":
        console.print(f"[yellow]🔍 {label}: {data['issue'][:120]}[/yellow]")

    elif event_type == "tool":
        name = data.get("name", "")
        inp = json.dumps(data.get("inputs", {}))[:60]
        icon = "⚙" if name in ("shell", "write_file") else "↗" if name == "web_search" else "★" if "skill" in name else "•"
        console.print(f"  [dim]{icon} {name}[/dim] [dim]{inp}[/dim]")

    elif event_type == "tool_result":
        ok = not data.get("error", False)
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        preview = data.get("result", "")[:100].replace("\n", " ")
        console.print(f"    {icon} [dim]{preview}[/dim]")

    elif event_type == "done":
        outcome = data.get("outcome", "unknown")
        summary = data.get("summary", "")
        lessons = data.get("lessons", [])
        tasks_n = data.get("tasks", 0)
        metrics = data.get("metrics", {})

        console.print()
        console.print(Rule(style="dim"))

        colors = {"success": "green", "failure": "red", "partial": "yellow"}
        c = colors.get(outcome, "white")
        console.print(f"[{c}]● {outcome.upper()}[/{c}]  [{tasks_n} tasks completed]\n")

        # Clean summary
        clean = summary
        for m in ["OUTCOME:", "LESSONS:"]:
            clean = clean.split(m)[0]
        if clean.strip():
            console.print(clean.strip()[:600])

        if metrics:
            console.print()
            console.print(
                "[dim]"
                f"duration={metrics.get('duration_ms', 0)}ms · "
                f"waves={metrics.get('waves', 0)} · "
                f"llm={metrics.get('llm_calls', 0)} · "
                f"tokens={metrics.get('input_tokens', 0)}/{metrics.get('output_tokens', 0)} in/out · "
                f"tools={metrics.get('tool_calls', 0)} ({metrics.get('tool_errors', 0)} errors) · "
                f"critic={metrics.get('critic_blocks', 0)}/{metrics.get('critic_checks', 0)} blocks · "
                f"plan_quality={metrics.get('planner_quality_score', 0)}/100"
                "[/dim]"
            )
            if metrics.get("estimated_cost_usd") is not None:
                console.print(f"[dim]estimated_cost=${metrics['estimated_cost_usd']:.4f}[/dim]")

        if lessons:
            console.print()
            console.print("[dim]Saved to memory:[/dim]")
            for l in lessons:
                console.print(f"  [dim]• {l}[/dim]")
        console.print()


def show_memory():
    import memory as mem
    mem.init()
    console.print()
    console.print(Panel("[bold]PHANTOM Memory[/bold]", border_style="cyan"))

    # Episodes
    db = mem.db_path()
    if not db.exists():
        console.print("[dim]No memory yet.[/dim]"); return

    rows = mem.recent_episodes(limit=10)

    if rows:
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("Outcome", style="dim", width=8)
        t.add_column("Goal")
        for r in rows:
            c = {"success": "green", "failure": "red", "partial": "yellow"}.get(r["outcome"], "white")
            t.add_row(f"[{c}]{r['outcome']}[/{c}]", r["goal"][:70])
        console.print(t)

    # Tool health
    health = mem.tool_health()
    if health:
        console.print("[dim]Tool health:[/dim]")
        for tool, s in health.items():
            c = "red" if s["fail_rate"] > 0.3 else "green"
            console.print(f"  {tool}: {s['calls']} calls, [{c}]{s['fail_rate']:.0%} failures[/{c}]")

    runs = mem.recent_runs()
    if runs:
        console.print("\n[dim]Recent runs:[/dim]")
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("Outcome", width=8)
        t.add_column("Tasks", justify="right")
        t.add_column("Tools", justify="right")
        t.add_column("Time", justify="right")
        for run in runs:
            c = {"success": "green", "failure": "red", "partial": "yellow"}.get(run["outcome"], "white")
            t.add_row(
                f"[{c}]{run['outcome']}[/{c}]",
                f"{run['tasks_completed']}/{run['tasks_planned']}",
                f"{run['tool_calls']}",
                f"{run['duration_ms']}ms",
            )
        console.print(t)

    # World model
    facts = mem.recent_world_facts(limit=8)
    if facts:
        console.print("\n[dim]World model (recent):[/dim]")
        for f in facts:
            console.print(
                f"  [dim]{f['key']}[/dim]: {f['value'][:60]} "
                f"[dim](v{f['version']}, conflicts={f['conflicts']})[/dim]"
            )

    demos = mem.recent_demonstrations(limit=5)
    if demos:
        console.print("\n[dim]Human demonstrations:[/dim]")
        for demo in demos:
            console.print(
                f"  [dim]#{demo['id']}[/dim] {demo['goal'][:60]} "
                f"[dim]({len(demo['steps'])} steps, {len(demo['screenshots'])} screenshots)[/dim]"
            )


def show_demonstrations():
    import memory as mem

    mem.init()
    demos = mem.recent_demonstrations(limit=20)
    if not demos:
        console.print("[dim]No saved human demonstrations yet.[/dim]")
        return
    console.print()
    console.print(Panel("[bold]PHANTOM Demonstrations[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("ID", justify="right")
    t.add_column("Goal")
    t.add_column("App")
    t.add_column("Steps", justify="right")
    t.add_column("Exec", justify="right")
    t.add_column("Shots", justify="right")
    t.add_column("Uses", justify="right")
    t.add_column("Reliab", justify="right")
    t.add_column("Last")
    for demo in demos:
        executable = sum(1 for step in demo["steps"] if step.get("executable"))
        t.add_row(
            str(demo["id"]),
            demo["goal"][:52],
            demo.get("app", "")[:16],
            str(len(demo["steps"])),
            str(executable),
            str(len(demo["screenshots"])),
            str(demo.get("uses", 0)),
            f"{demo.get('reliability', 0.0):.2f}",
            str(demo.get("last_replay_status") or "")[:10],
        )
    console.print(t)


def show_people():
    import memory as mem

    mem.init()
    people = mem.list_people(limit=20)
    if not people:
        console.print("[dim]No people remembered yet.[/dim]")
        return
    console.print()
    console.print(Panel("[bold]PHANTOM People[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Name")
    t.add_column("Relationship")
    t.add_column("Aliases")
    t.add_column("Notes")
    for person in people:
        t.add_row(
            person["name"][:24],
            str(person.get("relationship") or "")[:20],
            ", ".join(person.get("aliases", []))[:24],
            str(person.get("notes") or "")[:44],
        )
    console.print(t)


def show_projects():
    import memory as mem

    mem.init()
    projects = mem.list_projects(limit=20)
    if not projects:
        console.print("[dim]No projects remembered yet.[/dim]")
        return
    console.print()
    console.print(Panel("[bold]PHANTOM Projects[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Name")
    t.add_column("Status")
    t.add_column("Tags")
    t.add_column("Notes")
    for project in projects:
        t.add_row(
            project["name"][:24],
            str(project.get("status") or "")[:16],
            ", ".join(project.get("tags", []))[:24],
            str(project.get("notes") or "")[:44],
        )
    console.print(t)


def show_commitments(status: str = ""):
    import memory as mem

    mem.init()
    commitments = mem.list_commitments(limit=20, status=status)
    if not commitments:
        console.print("[dim]No commitments remembered yet.[/dim]")
        return
    console.print()
    console.print(Panel("[bold]PHANTOM Commitments[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("ID", justify="right")
    t.add_column("Title")
    t.add_column("Counterparty")
    t.add_column("Project")
    t.add_column("Due")
    t.add_column("Status")
    for item in commitments:
        t.add_row(
            str(item["id"]),
            item["title"][:32],
            str(item.get("counterparty") or "")[:18],
            str(item.get("project") or "")[:18],
            str(item.get("due_at") or "")[:12],
            str(item.get("status") or "")[:12],
        )
    console.print(t)


def show_signals(kind: str = "", source: str = ""):
    import memory as mem

    mem.init()
    signals = mem.list_signals(limit=20, kind=kind, source=source)
    if not signals:
        console.print("[dim]No ingested signals yet.[/dim]")
        return
    console.print()
    console.print(Panel("[bold]PHANTOM Signals[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("ID", justify="right")
    t.add_column("Kind")
    t.add_column("Source")
    t.add_column("Title")
    t.add_column("Extracted")
    for item in signals:
        extracted = item.get("extracted", {})
        counts = (
            f"p={len(extracted.get('people', []))} "
            f"proj={len(extracted.get('projects', []))} "
            f"c={len(extracted.get('commitments', []))}"
        )
        t.add_row(
            str(item["id"]),
            str(item.get("kind") or "")[:14],
            str(item.get("source") or "")[:16],
            str(item.get("title") or item.get("content") or "")[:44],
            counts,
        )
    console.print(t)


def show_briefing(query: str):
    import memory as mem

    mem.init()
    briefing = mem.chief_of_staff_briefing(query, limit=5)
    console.print()
    console.print(Panel(f"[bold]Briefing: {query or 'current scope'}[/bold]", border_style="cyan"))
    if briefing["people"]:
        console.print("[dim]People:[/dim]")
        for item in briefing["people"]:
            console.print(f"  [dim]-[/dim] {item['name']} {('(' + item['relationship'] + ')') if item.get('relationship') else ''} {str(item.get('notes') or '')[:80]}".strip())
    if briefing["projects"]:
        console.print("[dim]Projects:[/dim]")
        for item in briefing["projects"]:
            console.print(f"  [dim]-[/dim] {item['name']} {('[' + item['status'] + ']') if item.get('status') else ''} {str(item.get('notes') or '')[:80]}".strip())
    if briefing["commitments"]:
        console.print("[dim]Commitments:[/dim]")
        for item in briefing["commitments"]:
            extras = ", ".join(part for part in [
                f"to {item['counterparty']}" if item.get("counterparty") else "",
                f"project={item['project']}" if item.get("project") else "",
                f"due={item['due_at']}" if item.get("due_at") else "",
                f"status={item['status']}" if item.get("status") else "",
            ] if part)
            console.print(f"  [dim]-[/dim] {item['title']} {('(' + extras + ')') if extras else ''}".strip())
    if briefing.get("signals"):
        console.print("[dim]Signals:[/dim]")
        for item in briefing["signals"]:
            label = str(item.get("title") or item.get("content") or "")[:80]
            console.print(f"  [dim]-[/dim] [{item.get('kind')}] {label} via {item.get('source')}")
    if not briefing["people"] and not briefing["projects"] and not briefing["commitments"] and not briefing.get("signals"):
        console.print("[dim]No chief-of-staff memory matches yet.[/dim]")


def show_demonstration_detail(demo_id: int):
    import memory as mem

    mem.init()
    demo = mem.get_demonstration(int(demo_id))
    if not demo:
        console.print(f"[red]Demonstration #{demo_id} not found.[/red]")
        return
    console.print()
    console.print(Panel(mem.format_demonstration(demo), title="[cyan bold]Demonstration[/cyan bold]", border_style="cyan"))


def show_demonstration_matches(query: str):
    import memory as mem

    mem.init()
    matches = mem.procedure_matches(query, limit=8)
    if not matches:
        console.print("[dim]No matching demonstrations found.[/dim]")
        return
    console.print()
    console.print(Panel(f"[bold]Matches For: {query}[/bold]", border_style="cyan"))
    for match in matches:
        readiness = f"{match.executable_steps}/{match.total_steps}"
        console.print(
            f"[cyan]#{match.demo_id}[/cyan] {match.goal} "
            f"[dim]confidence={match.confidence:.2f} "
            f"reliability={match.reliability:.2f} "
            f"replay={readiness} "
            f"reasons={', '.join(match.reasons) or 'none'}[/dim]"
        )


def replay_demonstration_cli(demo_id: int, execute: bool, allow_risky: bool):
    from tools import dispatch

    result, err = dispatch("replay_demonstration", {
        "id": int(demo_id),
        "execute": execute,
        "allow_risky": allow_risky,
    })
    color = "red" if err else "green"
    console.print(f"[{color}]{result}[/{color}]")


def _split_assignment(value: str, flag_name: str) -> tuple[str, str]:
    left, sep, right = str(value).partition("=")
    if not sep:
        raise ValueError(f"{flag_name} values must use selector=value.")
    left = left.strip()
    right = right.strip()
    if not left or not right:
        raise ValueError(f"{flag_name} values must use non-empty selector=value.")
    return left, right


def _parse_teach_step_json(values: list[str]) -> list[dict]:
    steps = []
    for raw in values or []:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("--teach-step-json values must decode to JSON objects.")
        steps.append(payload)
    return steps


def _build_teach_steps(args) -> list:
    steps: list = [step for step in args.teach_step if str(step).strip()]
    steps.extend(_parse_teach_step_json(args.teach_step_json))
    for command in args.teach_shell:
        steps.append({
            "action": "shell",
            "title": f"Run shell command: {command}",
            "instructions": f"Run shell command: {command}",
            "target": command,
            "inputs": {"cmd": command},
            "risk": "medium",
            "expected": "",
            "executable": True,
        })
    for path in args.teach_read_file:
        steps.append({
            "action": "read_file",
            "title": f"Read file {path}",
            "instructions": f"Read file {path}",
            "target": path,
            "inputs": {"path": path},
            "risk": "low",
            "executable": True,
        })
    for query in args.teach_web_search:
        steps.append({
            "action": "web_search",
            "title": f"Search for {query}",
            "instructions": f"Search the web for {query}",
            "target": query,
            "inputs": {"query": query},
            "risk": "low",
            "executable": True,
        })
    for item in args.teach_remember:
        key, sep, value = str(item).partition("=")
        if not sep:
            raise ValueError("--teach-remember values must use key=value.")
        steps.append({
            "action": "remember",
            "title": f"Remember {key.strip()}",
            "instructions": f"Save fact {key.strip()}",
            "target": key.strip(),
            "inputs": {"key": key.strip(), "value": value.strip()},
            "risk": "low",
            "expected": value.strip(),
            "executable": True,
        })
    for url in args.teach_browser_goto:
        steps.append({
            "action": "browser_goto",
            "title": f"Open {url}",
            "instructions": f"Open browser page {url}",
            "target": url,
            "inputs": {"url": url},
            "risk": "low",
            "expected": "",
            "executable": True,
        })
    for selector in args.teach_browser_click:
        selector = str(selector).strip()
        steps.append({
            "action": "browser_click",
            "title": f"Click {selector}",
            "instructions": f"Click browser element {selector}",
            "target": selector,
            "inputs": {"selector": selector},
            "risk": "low",
            "expected": "",
            "executable": True,
        })
    for item in args.teach_browser_fill:
        selector, value = _split_assignment(item, "--teach-browser-fill")
        steps.append({
            "action": "browser_fill",
            "title": f"Fill {selector}",
            "instructions": f"Fill browser field {selector}",
            "target": selector,
            "inputs": {"selector": selector, "value": value},
            "risk": "medium",
            "expected": value,
            "executable": True,
        })
    for item in args.teach_browser_press:
        selector, sep, key = str(item).partition("=")
        selector = selector.strip()
        key = key.strip()
        inputs = {}
        target = ""
        title = ""
        if sep:
            if not selector or not key:
                raise ValueError("--teach-browser-press values must use selector=key or just key.")
            inputs = {"selector": selector, "key": key}
            target = selector
            title = f"Press {key} on {selector}"
        else:
            key = selector
            if not key:
                raise ValueError("--teach-browser-press requires a key or selector=key.")
            inputs = {"key": key}
            title = f"Press {key}"
        steps.append({
            "action": "browser_press",
            "title": title,
            "instructions": title,
            "target": target,
            "inputs": inputs,
            "risk": "medium",
            "expected": "",
            "executable": True,
        })
    for item in args.teach_browser_wait:
        raw = str(item).strip()
        inputs = {}
        title = ""
        target = ""
        if raw.startswith("url:"):
            url_contains = raw[4:].strip()
            if not url_contains:
                raise ValueError("--teach-browser-wait url: values must include text after url:.")
            inputs = {"url_contains": url_contains}
            target = url_contains
            title = f"Wait for URL containing {url_contains}"
        elif raw.startswith("url_contains="):
            _, _, url_contains = raw.partition("=")
            url_contains = url_contains.strip()
            if not url_contains:
                raise ValueError("--teach-browser-wait url_contains= values must include text.")
            inputs = {"url_contains": url_contains}
            target = url_contains
            title = f"Wait for URL containing {url_contains}"
        else:
            if not raw:
                raise ValueError("--teach-browser-wait requires a selector or url:<text>.")
            inputs = {"selector": raw}
            target = raw
            title = f"Wait for {raw}"
        steps.append({
            "action": "browser_wait_for",
            "title": title,
            "instructions": title,
            "target": target,
            "inputs": inputs,
            "risk": "low",
            "expected": "",
            "executable": True,
        })
    for item in args.teach_browser_extract:
        selector, _, name = str(item).partition("::")
        selector = selector.strip()
        name = name.strip()
        if not selector:
            raise ValueError("--teach-browser-extract requires selector or selector::name.")
        steps.append({
            "action": "browser_extract_text",
            "title": f"Extract text from {selector}",
            "instructions": f"Extract text from browser element {selector}",
            "target": selector,
            "inputs": {"selector": selector, **({"name": name} if name else {})},
            "risk": "low",
            "expected": "",
            "executable": True,
        })
    for item in args.teach_browser_assert:
        selector, expected = _split_assignment(item, "--teach-browser-assert")
        steps.append({
            "action": "browser_assert_text",
            "title": f"Assert text in {selector}",
            "instructions": f"Assert browser element {selector} contains expected text",
            "target": selector,
            "inputs": {"selector": selector, "expected": expected},
            "risk": "low",
            "expected": expected,
            "executable": True,
        })
    for label in args.teach_browser_screenshot:
        label = str(label).strip()
        steps.append({
            "action": "browser_screenshot",
            "title": f"Capture screenshot {label or 'page state'}",
            "instructions": f"Capture browser screenshot {label or 'page state'}",
            "target": label,
            "inputs": {"name": label or "browser_state", "full_page": True},
            "risk": "low",
            "expected": "",
            "executable": True,
        })
    return steps


def show_skills():
    from core.skill_catalog import assess_skill_support, load_bundled_skills
    import memory as mem

    mem.init()
    bundled = load_bundled_skills()
    runtime_skills = mem.list_skills()
    console.print()
    console.print(Panel("[bold]PHANTOM Skills[/bold]", border_style="cyan"))
    if bundled:
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("Bundled Playbook", style="cyan")
        t.add_column("Source", style="magenta")
        t.add_column("Support", style="green")
        t.add_column("Summary")
        for skill in bundled:
            support = assess_skill_support(skill)
            t.add_row(skill.name, skill.source, support.status, skill.summary[:70])
        console.print(t)
        console.print()
    if runtime_skills:
        t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
        t.add_column("Executable Skill", style="cyan")
        t.add_column("Description")
        t.add_column("Uses", justify="right")
        t.add_column("Version", justify="right")
        for skill in runtime_skills:
            t.add_row(skill["name"], skill["description"][:50], str(skill["uses"]), str(skill.get("current_version", 1)))
        console.print(t)
        return
    console.print("[dim]No runtime-created executable skills yet. PHANTOM will create them as needed.[/dim]")


def show_skill_history(name: str):
    import memory as mem

    mem.init()
    versions = mem.list_skill_versions(name)
    if not versions:
        console.print(f"[dim]No saved versions for skill '{name}'.[/dim]")
        return
    console.print()
    console.print(Panel(f"[bold]Skill History: {name}[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Version", justify="right")
    t.add_column("Description")
    for item in versions:
        t.add_row(str(item["version"]), item["description"][:60])
    console.print(t)


def show_replay(trace_id: str):
    from core.observability import replay_trace

    try:
        events = replay_trace(trace_id)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    console.print()
    console.print(Panel(f"[bold]Replay: {trace_id}[/bold]", border_style="cyan"))
    for event in events:
        agent = event.get("agent") or "orchestrator"
        label = _agent_label(agent)
        event_type = event.get("event_type")
        payload = event.get("payload", {})
        preview = json.dumps(payload)[:140]
        console.print(f"[dim]{label}[/dim] {event_type}: {preview}")


def show_evals():
    from evals.offline import run_offline_evals

    summary = run_offline_evals()
    console.print()
    console.print(Panel("[bold]PHANTOM Offline Evals[/bold]", border_style="cyan"))
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Eval")
    t.add_column("Status")
    t.add_column("Time", justify="right")
    t.add_column("Detail")
    for result in summary["results"]:
        status = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        t.add_row(result.name, status, f"{result.duration_ms}ms", result.detail[:80])
    console.print(t)
    color = "green" if summary["failed"] == 0 else "red"
    console.print(f"\n[{color}]{summary['passed']}/{summary['total']} evals passed[/{color}]")


def show_doctor():
    from core.doctor import doctor_report

    report = doctor_report()
    color = {"pass": "green", "warn": "yellow", "fail": "red"}.get(report["status"], "yellow")
    console.print()
    console.print(Panel("[bold]PHANTOM Doctor[/bold]", border_style=color))
    console.print(
        f"[dim]scope:[/dim] {report['scope']}\n"
        f"[dim]workspace:[/dim] {report['workspace']}\n"
        f"[dim]home:[/dim] {report['home']}"
    )
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Check", style="cyan")
    t.add_column("Status")
    t.add_column("Detail")
    for item in report["checks"]:
        item_color = {"pass": "green", "warn": "yellow", "fail": "red"}.get(item["status"], "white")
        t.add_row(item["name"], f"[{item_color}]{item['status']}[/{item_color}]", item["detail"][:100])
    console.print(t)
    console.print(f"\n[{color}]Overall: {report['status'].upper()}[/{color}]")


def show_pairings():
    from integrations.messaging import list_pairing_requests

    requests = list_pairing_requests(limit=50)
    console.print()
    console.print(Panel("[bold]Pending Messaging Pairings[/bold]", border_style="cyan"))
    if not requests:
        console.print("[dim]No pending pairing requests.[/dim]")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Platform", style="cyan")
    t.add_column("Code")
    t.add_column("Sender")
    t.add_column("Conversation")
    t.add_column("Requested", justify="right")
    for item in requests:
        requested = _fmt_ts(item.get("requested_at", 0))
        t.add_row(
            str(item.get("platform") or ""),
            str(item.get("code") or ""),
            str(item.get("sender_name") or item.get("sender_id") or ""),
            str(item.get("conversation_id") or ""),
            requested,
        )
    console.print(t)


def show_allowlist():
    from integrations.messaging import list_allowed_senders

    senders = list_allowed_senders(limit=50)
    console.print()
    console.print(Panel("[bold]Messaging Allowlist[/bold]", border_style="cyan"))
    if not senders:
        console.print("[dim]No approved messaging senders yet.[/dim]")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Platform", style="cyan")
    t.add_column("Sender")
    t.add_column("Conversation")
    t.add_column("Approved", justify="right")
    t.add_column("Source")
    for item in senders:
        approved = _fmt_ts(item.get("approved_at", 0))
        t.add_row(
            str(item.get("platform") or ""),
            str(item.get("sender_name") or item.get("sender_id") or ""),
            str(item.get("conversation_id") or ""),
            approved,
            str(item.get("source") or ""),
        )
    console.print(t)


def approve_pairing_cli(platform: str, code: str):
    from integrations.messaging import approve_pairing, send_pairing_approval_notice

    approved = approve_pairing(platform, code)
    if not approved:
        console.print(f"[red]No pending pairing found for {platform} code {code}.[/red]")
        return
    console.print(
        f"[green]Approved {approved['platform']} sender:[/green] "
        f"{approved.get('sender_name') or approved.get('sender_id')}"
    )
    try:
        send_pairing_approval_notice(approved)
        console.print("[dim]Sent approval notice to the user.[/dim]")
    except Exception as exc:
        console.print(f"[yellow]Approved, but could not send approval notice automatically:[/yellow] {exc}")


def show_extensions():
    from core.extensions import load_extensions

    manifests = load_extensions()
    console.print()
    console.print(Panel("[bold]PHANTOM Extensions[/bold]", border_style="cyan"))
    if not manifests:
        console.print("[dim]No extension manifests found.[/dim]")
        return
    t = Table(box=box.SIMPLE, show_header=True, header_style="dim")
    t.add_column("Extension", style="cyan")
    t.add_column("Capabilities")
    t.add_column("Enabled")
    t.add_column("Description")
    for item in manifests:
        t.add_row(
            item.extension_id,
            ", ".join(item.capabilities[:4]) or "none",
            "yes" if item.enabled_by_default else "no",
            item.description[:60] or item.title,
        )
    console.print(t)


def run_onboard():
    from core.onboard import OnboardConfig, onboard_env_text, write_onboard_env

    workspace_default = str(Path.cwd().resolve())
    console.print()
    console.print(Panel(
        (
            "[bold]PHANTOM Onboard[/bold]\n"
            "We’ll create a simple local setup so PHANTOM is easier to run consistently.\n"
            "This writes placeholders, not real secrets."
        ),
        border_style="cyan",
    ))
    workspace = _prompt_chat_value(f"Workspace path [{workspace_default}]: ").strip() or workspace_default
    provider_choice = prompt_choice(
        "Provider: [g]roq, [o]penai, [a]nthropic, or [s]kip for now? [g/o/a/S]: ",
        {
            "groq": ("g",),
            "openai": ("o",),
            "anthropic": ("a",),
            "skip": ("s",),
        },
        default="skip",
    )
    confirm_plan_choice = prompt_choice(
        "Require plan approval before execution? [Y/n]: ",
        {
            "yes": ("y",),
            "no": ("n",),
        },
        default="yes",
    )
    messaging_choice = prompt_choice(
        "Messaging DM policy: [p]airing, [o]pen, or [c]losed? [P/o/c]: ",
        {
            "pairing": ("p",),
            "open": ("o",),
            "closed": ("c",),
        },
        default="pairing",
    )
    config = OnboardConfig(
        workspace=workspace,
        provider="" if provider_choice == "skip" else provider_choice,
        confirm_plan=confirm_plan_choice == "yes",
        messaging_policy=messaging_choice,
    )
    env_path = Path.cwd() / ".phantom.env"
    console.print()
    console.print(Panel(onboard_env_text(config), title="[cyan bold].phantom.env preview[/cyan bold]", border_style="cyan"))
    write_choice = prompt_choice(
        f"Write this file to {env_path}? [Y/n]: ",
        {
            "yes": ("y",),
            "no": ("n",),
        },
        default="yes",
    )
    if write_choice == "yes":
        saved = write_onboard_env(env_path, config)
        console.print(f"[green]Wrote onboarding env file:[/green] {saved}")
        console.print("[dim]Load it with:[/dim] source .phantom.env")
    else:
        console.print("[dim]Skipped writing the file.[/dim]")
    console.print()
    console.print("[bold]Recommended next commands[/bold]")
    console.print("  source .phantom.env")
    console.print("  python3 phantom.py --doctor")
    console.print("  python3 phantom.py")
    console.print("  python3 phantom.py --approve-plan \"review this repository and explain the architecture\"")


def show_chat_menu():
    console.print()
    console.print(Panel(
        (
            "[bold]Type a task in plain language[/bold] and PHANTOM will run it.\n\n"
            "[dim]Quick actions:[/dim]\n"
            "  [cyan]1[/cyan] Run a task\n"
            "  [cyan]2[/cyan] Memory\n"
            "  [cyan]3[/cyan] People\n"
            "  [cyan]4[/cyan] Projects\n"
            "  [cyan]5[/cyan] Commitments\n"
            "  [cyan]6[/cyan] Signals\n"
            "  [cyan]7[/cyan] Briefing\n"
            "  [cyan]8[/cyan] Demonstrations\n"
            "  [cyan]9[/cyan] Skills\n"
            "  [cyan]10[/cyan] Evals\n"
            "  [cyan]11[/cyan] Doctor\n"
            "  [cyan]12[/cyan] Pairings\n"
            "  [cyan]13[/cyan] Allowlist\n"
            "  [cyan]14[/cyan] Extensions\n"
            "  [cyan]0[/cyan] Exit\n\n"
            "[dim]Slash commands also work:[/dim] [dim]/memory /people /projects /commitments /signals /brief /demos /skills /evals /doctor /pairings /allowlist /extensions /exit[/dim]"
        ),
        title="[cyan bold]PHANTOM Chat[/cyan bold]",
        border_style="cyan",
    ))


def _prompt_chat_value(message: str) -> str:
    try:
        return input(message).strip()
    except EOFError:
        return ""


def run_goal_command(goal: str, args):
    from core.orchestrator import run

    dashboard = None
    try:
        if getattr(args, "live_ui", False):
            from core.live_ui import LiveDashboard

            dashboard = LiveDashboard().start(host=args.live_ui_host, port=args.live_ui_port)
            console.print(
                f"[cyan]Live activity page:[/cyan] [underline]{dashboard.url}[/underline] "
                "[dim](open this in your browser while the run is active)[/dim]"
            )

        def emit(event_type: str, data: dict):
            handle(event_type, data)
            if dashboard is not None:
                dashboard.publish(event_type, data)

        return run(goal=goal, on_event=emit, parallel=not args.no_parallel)
    finally:
        if dashboard is not None:
            dashboard.stop()


def interactive_chat(args):
    if not _stdin_is_tty():
        return None

    show_chat_menu()
    while True:
        raw = _prompt_chat_value("\nphantom> ").strip()
        if not raw:
            continue
        command = raw.lower()
        if command in {"0", "exit", "quit", "/exit", "/quit"}:
            console.print("[dim]PHANTOM chat closed.[/dim]")
            return None
        if command in {"?", "help", "menu", "/help", "/menu"}:
            show_chat_menu()
            continue
        if command in {"1", "/run", "run"}:
            goal = _prompt_chat_value("What should PHANTOM do? ").strip()
            if goal:
                run_goal_command(goal, args)
            continue
        if command in {"2", "/memory", "memory"}:
            show_memory()
            continue
        if command in {"3", "/people", "people"}:
            show_people()
            continue
        if command in {"4", "/projects", "projects"}:
            show_projects()
            continue
        if command in {"5", "/commitments", "commitments"}:
            status = _prompt_chat_value("Filter commitments by status (optional): ").strip()
            show_commitments(status)
            continue
        if command in {"6", "/signals", "signals"}:
            kind = _prompt_chat_value("Signal kind filter (optional): ").strip()
            source = _prompt_chat_value("Signal source filter (optional): ").strip()
            show_signals(kind, source)
            continue
        if command.startswith("/brief "):
            show_briefing(raw.split(" ", 1)[1].strip())
            continue
        if command in {"7", "/brief", "brief"}:
            topic = _prompt_chat_value("Briefing topic (optional): ").strip()
            show_briefing(topic)
            continue
        if command in {"8", "/demos", "/demonstrations", "demos", "demonstrations"}:
            show_demonstrations()
            continue
        if command in {"9", "/skills", "skills"}:
            show_skills()
            continue
        if command in {"10", "/evals", "evals"}:
            show_evals()
            continue
        if command in {"11", "/doctor", "doctor"}:
            show_doctor()
            continue
        if command in {"12", "/pairings", "pairings"}:
            show_pairings()
            continue
        if command in {"13", "/allowlist", "allowlist"}:
            show_allowlist()
            continue
        if command in {"14", "/extensions", "extensions"}:
            show_extensions()
            continue

        run_goal_command(raw, args)


def main():
    parser = argparse.ArgumentParser(
        description="PHANTOM — autonomous multi-agent Python framework"
    )
    parser.add_argument("goal", nargs="?", help="Goal to accomplish")
    parser.add_argument("--no-parallel", action="store_true", help="Run tasks sequentially")
    parser.add_argument("--memory", action="store_true", help="Show memory stats")
    parser.add_argument("--onboard", action="store_true", help="Interactive PHANTOM setup wizard")
    parser.add_argument("--extensions", action="store_true", help="List discovered extension manifests")
    parser.add_argument("--people", action="store_true", help="List remembered people")
    parser.add_argument("--projects", action="store_true", help="List remembered projects")
    parser.add_argument("--commitments", action="store_true", help="List remembered commitments")
    parser.add_argument("--commitment-status", help="Filter listed commitments by status")
    parser.add_argument("--signals", action="store_true", help="List ingested raw signals")
    parser.add_argument("--signal-kind", help="Signal kind for ingestion or filtering, like message, email, meeting, or doc")
    parser.add_argument("--signal-source", help="Signal source for ingestion or filtering")
    parser.add_argument("--signal-title", help="Short signal title for ingestion")
    parser.add_argument("--signal-metadata", help="JSON object with extraction hints for ingestion")
    parser.add_argument("--signal-happened-at", help="Human-readable happened_at timestamp for ingestion")
    parser.add_argument("--ingest-signal", help="Store a raw signal and extract people/projects/commitments")
    parser.add_argument("--brief", nargs="?", const="", help="Show chief-of-staff briefing for a topic")
    parser.add_argument("--demonstrations", action="store_true", help="List saved human demonstrations")
    parser.add_argument("--skills", action="store_true", help="List created skills")
    parser.add_argument("--evals", action="store_true", help="Run offline engineering evals")
    parser.add_argument("--doctor", action="store_true", help="Check PHANTOM runtime configuration and environment")
    parser.add_argument("--confirm", action="store_true", help="Require human approval for the plan and risky tool actions")
    parser.add_argument("--approve-plan", action="store_true", help="Show the plan first and require approval before execution")
    parser.add_argument("--replay", help="Replay a previous trace by id")
    parser.add_argument("--skill-history", help="Show version history for a skill")
    parser.add_argument("--rollback-skill", nargs=2, metavar=("NAME", "VERSION"), help="Roll back a skill to a previous version")
    parser.add_argument("--teach", metavar="GOAL", help="Save a human demonstration for a goal")
    parser.add_argument("--teach-summary", help="Short summary of what the human demonstrated")
    parser.add_argument("--teach-step", action="append", default=[], help="One demonstrated step; repeat for multiple steps")
    parser.add_argument("--teach-step-json", action="append", default=[], help="Structured step as JSON object")
    parser.add_argument("--teach-shell", action="append", default=[], help="Executable shell step to include in the demonstration")
    parser.add_argument("--teach-read-file", action="append", default=[], help="Executable read_file step to include in the demonstration")
    parser.add_argument("--teach-web-search", action="append", default=[], help="Executable web_search step to include in the demonstration")
    parser.add_argument("--teach-remember", action="append", default=[], help="Executable remember step in key=value form")
    parser.add_argument("--teach-browser-goto", action="append", default=[], help="Executable browser step: open a URL")
    parser.add_argument("--teach-browser-click", action="append", default=[], help="Executable browser step: click a CSS/text selector")
    parser.add_argument("--teach-browser-fill", action="append", default=[], help="Executable browser step: selector=value")
    parser.add_argument("--teach-browser-press", action="append", default=[], help="Executable browser step: selector=key or just key")
    parser.add_argument("--teach-browser-wait", action="append", default=[], help="Executable browser step: selector or url:<text>")
    parser.add_argument("--teach-browser-extract", action="append", default=[], help="Executable browser step: selector or selector::name")
    parser.add_argument("--teach-browser-assert", action="append", default=[], help="Executable browser step: selector=expected text")
    parser.add_argument("--teach-browser-screenshot", action="append", default=[], help="Executable browser step: capture a screenshot with an optional label")
    parser.add_argument("--teach-screenshot", action="append", default=[], help="Path to a screenshot asset; repeat for multiple files")
    parser.add_argument("--teach-app", help="Application or system the demonstration belongs to")
    parser.add_argument("--teach-environment", help="Environment like staging, production, admin console, etc.")
    parser.add_argument("--teach-tag", action="append", default=[], help="Tag the demonstration for better matching")
    parser.add_argument("--teach-permission", action="append", default=[], help="Permission or prerequisite needed for this workflow")
    parser.add_argument("--correct-demonstration", type=int, help="Create a corrected successor for an existing demonstration")
    parser.add_argument("--match-demonstrations", help="Show demonstrations matching a query")
    parser.add_argument("--explain-demonstration", type=int, help="Show full details for a demonstration")
    parser.add_argument("--replay-demonstration", type=int, help="Replay a demonstration by id")
    parser.add_argument("--execute-demonstration", action="store_true", help="Actually execute replayable demonstration steps")
    parser.add_argument("--allow-risky-replay", action="store_true", help="Allow high-risk steps during demonstration replay")
    parser.add_argument("--add-person", help="Remember a person/contact by name")
    parser.add_argument("--person-relationship", help="Relationship label for --add-person")
    parser.add_argument("--person-notes", help="Notes for --add-person")
    parser.add_argument("--person-alias", action="append", default=[], help="Alias for --add-person; repeat as needed")
    parser.add_argument("--add-project", help="Remember a project by name")
    parser.add_argument("--project-status", help="Status for --add-project")
    parser.add_argument("--project-notes", help="Notes for --add-project")
    parser.add_argument("--project-tag", action="append", default=[], help="Tag for --add-project; repeat as needed")
    parser.add_argument("--add-commitment", help="Remember a commitment or promise")
    parser.add_argument("--commitment-owner", help="Owner for --add-commitment")
    parser.add_argument("--commitment-counterparty", help="Counterparty for --add-commitment")
    parser.add_argument("--commitment-project", help="Project for --add-commitment")
    parser.add_argument("--commitment-due", help="Due date/string for --add-commitment")
    parser.add_argument("--commitment-notes", help="Notes for --add-commitment")
    parser.add_argument("--scope", help="Override the memory/workspace scope for this run")
    parser.add_argument("--workspace", help="Set the workspace root used for safety and scoped memory")
    parser.add_argument("--pairings", action="store_true", help="List pending messaging pairing requests")
    parser.add_argument("--allowlist", action="store_true", help="List approved messaging senders")
    parser.add_argument("--approve-pairing", nargs=2, metavar=("PLATFORM", "CODE"), help="Approve a pending messaging pairing request")
    parser.add_argument("--serve-messaging", action="store_true", help="Run Telegram and WhatsApp webhook server")
    parser.add_argument("--messaging-host", default="0.0.0.0", help="Host for messaging webhooks")
    parser.add_argument("--messaging-port", type=int, default=8080, help="Port for messaging webhooks")
    parser.add_argument("--messaging-workers", type=int, help="Background worker count for inbound messages")
    parser.add_argument("--set-telegram-webhook", metavar="URL", help="Register the Telegram webhook URL with the Bot API")
    parser.add_argument("--serve-gateway", action="store_true", help="Run the persistent PHANTOM HTTP control plane")
    parser.add_argument("--gateway-host", default="127.0.0.1", help="Host for the PHANTOM gateway")
    parser.add_argument("--gateway-port", type=int, default=8787, help="Port for the PHANTOM gateway")
    parser.add_argument("--gateway-workers", type=int, default=4, help="Background session worker count for the gateway")
    parser.add_argument("--live-ui", action="store_true", help="Serve a live activity page showing what PHANTOM is doing during the run")
    parser.add_argument("--live-ui-host", default="127.0.0.1", help="Host for the live activity page")
    parser.add_argument("--live-ui-port", type=int, default=0, help="Port for the live activity page (0 picks a free port)")
    parser.add_argument("--max-llm-calls", type=int, help="Per-run LLM call budget")
    parser.add_argument("--max-tool-calls", type=int, help="Per-run tool call budget")
    parser.add_argument("--max-llm-calls-per-minute", type=int, help="Per-run LLM rate limit")
    parser.add_argument("--max-tool-calls-per-minute", type=int, help="Per-run tool rate limit")
    parser.add_argument("--api-timeout", type=int, help="Provider request timeout in seconds")
    parser.add_argument("--provider-retries", type=int, help="Number of provider retries before failing the run")
    parser.add_argument("--max-input-tokens", type=int, help="Per-run input token budget")
    parser.add_argument("--max-output-tokens", type=int, help="Per-run output token budget")
    parser.add_argument("--max-cost-usd", type=float, help="Per-run estimated cost budget when a price table is configured")
    parser.add_argument("--stop-file", help="Abort the run if this file exists")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts for admin commands")
    args = parser.parse_args()

    if args.confirm:
        os.environ["PHANTOM_CONFIRM"] = "1"
    if args.approve_plan:
        os.environ["PHANTOM_CONFIRM_PLAN"] = "1"
    if args.scope:
        os.environ["PHANTOM_SCOPE"] = args.scope
    if args.workspace:
        os.environ["PHANTOM_WORKSPACE"] = args.workspace
    if args.messaging_workers is not None:
        os.environ["PHANTOM_MESSAGING_MAX_WORKERS"] = str(args.messaging_workers)
    if args.max_llm_calls is not None:
        os.environ["PHANTOM_MAX_LLM_CALLS"] = str(args.max_llm_calls)
    if args.max_tool_calls is not None:
        os.environ["PHANTOM_MAX_TOOL_CALLS"] = str(args.max_tool_calls)
    if args.max_llm_calls_per_minute is not None:
        os.environ["PHANTOM_MAX_LLM_CALLS_PER_MINUTE"] = str(args.max_llm_calls_per_minute)
    if args.max_tool_calls_per_minute is not None:
        os.environ["PHANTOM_MAX_TOOL_CALLS_PER_MINUTE"] = str(args.max_tool_calls_per_minute)
    if args.api_timeout is not None:
        os.environ["PHANTOM_API_TIMEOUT_SECONDS"] = str(args.api_timeout)
    if args.provider_retries is not None:
        os.environ["PHANTOM_PROVIDER_RETRIES"] = str(args.provider_retries)
    if args.max_input_tokens is not None:
        os.environ["PHANTOM_MAX_INPUT_TOKENS"] = str(args.max_input_tokens)
    if args.max_output_tokens is not None:
        os.environ["PHANTOM_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)
    if args.max_cost_usd is not None:
        os.environ["PHANTOM_MAX_COST_USD"] = str(args.max_cost_usd)
    if args.stop_file:
        os.environ["PHANTOM_STOP_FILE"] = args.stop_file

    if args.memory:
        show_memory(); return
    if args.onboard:
        run_onboard(); return
    if args.extensions:
        show_extensions(); return
    if args.people:
        show_people(); return
    if args.projects:
        show_projects(); return
    if args.commitments:
        show_commitments(args.commitment_status or ""); return
    if args.signals:
        show_signals(args.signal_kind or "", args.signal_source or ""); return
    if args.pairings:
        show_pairings(); return
    if args.allowlist:
        show_allowlist(); return
    if args.approve_pairing:
        approve_pairing_cli(args.approve_pairing[0], args.approve_pairing[1]); return
    if args.brief is not None:
        show_briefing(args.brief); return
    if args.demonstrations:
        show_demonstrations(); return
    if args.match_demonstrations:
        show_demonstration_matches(args.match_demonstrations); return
    if args.explain_demonstration is not None:
        show_demonstration_detail(args.explain_demonstration); return
    if args.replay_demonstration is not None:
        replay_demonstration_cli(args.replay_demonstration, args.execute_demonstration, args.allow_risky_replay); return
    if args.skills:
        show_skills(); return
    if args.evals:
        show_evals(); return
    if args.doctor:
        show_doctor(); return
    if args.replay:
        show_replay(args.replay); return
    if args.skill_history:
        show_skill_history(args.skill_history); return
    if args.rollback_skill:
        import memory as mem

        mem.init()
        name, version = args.rollback_skill
        if not args.yes:
            answer = input(f"Roll back skill '{name}' to version {version}? This overwrites the current version. [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                console.print("[dim]Rollback cancelled.[/dim]")
                return
        ok = mem.rollback_skill(name, int(version))
        if ok:
            console.print(f"[green]Rolled back {name} to version {version}.[/green]")
        else:
            console.print(f"[red]Skill {name} version {version} not found.[/red]")
        return
    if args.teach or args.correct_demonstration is not None:
        import memory as mem

        mem.init()
        try:
            steps = _build_teach_steps(args)
            if args.correct_demonstration is not None:
                saved = mem.correct_demonstration(
                    int(args.correct_demonstration),
                    goal=args.teach or None,
                    summary=args.teach_summary,
                    steps=steps,
                    screenshots=args.teach_screenshot,
                    app=args.teach_app,
                    environment=args.teach_environment,
                    tags=args.teach_tag,
                    permissions=args.teach_permission,
                )
            else:
                saved = mem.save_demonstration(
                    goal=args.teach,
                    summary=args.teach_summary or "",
                    steps=steps,
                    screenshots=args.teach_screenshot,
                    source="human",
                    app=args.teach_app or "",
                    environment=args.teach_environment or "",
                    tags=args.teach_tag,
                    permissions=args.teach_permission,
                )
        except Exception as exc:
            console.print(f"[red]Failed to save demonstration: {exc}[/red]")
            sys.exit(1)
        console.print(
            f"[green]Saved demonstration #{saved['id']}[/green] "
            f"[dim]{saved['goal']}[/dim]"
        )
        console.print(
            f"[dim]{len(saved['steps'])} steps · {sum(1 for step in saved['steps'] if step.get('executable'))} executable · "
            f"{len(saved['screenshots'])} screenshots[/dim]"
        )
        return
    if args.add_person:
        import memory as mem

        mem.init()
        saved = mem.save_person(
            args.add_person,
            relationship=args.person_relationship or "",
            notes=args.person_notes or "",
            aliases=args.person_alias,
        )
        console.print(f"[green]Saved person:[/green] {saved['name']}")
        return
    if args.add_project:
        import memory as mem

        mem.init()
        saved = mem.save_project(
            args.add_project,
            status=args.project_status or "",
            notes=args.project_notes or "",
            tags=args.project_tag,
        )
        console.print(f"[green]Saved project:[/green] {saved['name']}")
        return
    if args.add_commitment:
        import memory as mem

        mem.init()
        saved = mem.save_commitment(
            args.add_commitment,
            owner=args.commitment_owner or "user",
            counterparty=args.commitment_counterparty or "",
            project=args.commitment_project or "",
            due_at=args.commitment_due or "",
            status=args.commitment_status or "open",
            notes=args.commitment_notes or "",
        )
        console.print(f"[green]Saved commitment #{saved['id']}:[/green] {saved['title']}")
        return
    if args.ingest_signal:
        import memory as mem

        mem.init()
        try:
            metadata = json.loads(args.signal_metadata) if args.signal_metadata else {}
            if metadata and not isinstance(metadata, dict):
                raise ValueError("--signal-metadata must decode to a JSON object.")
            saved = mem.ingest_signal(
                args.signal_kind or "message",
                args.ingest_signal,
                source=args.signal_source or "manual",
                title=args.signal_title or "",
                metadata=metadata,
                happened_at=args.signal_happened_at or "",
            )
        except Exception as exc:
            console.print(f"[red]Failed to ingest signal: {exc}[/red]")
            sys.exit(1)
        extracted = saved.get("extracted", {})
        console.print(
            f"[green]Saved signal #{saved['id']}:[/green] "
            f"[dim]{saved['kind']} via {saved['source']}[/dim]"
        )
        console.print(
            "[dim]"
            f"extracted {len(extracted.get('people', []))} people · "
            f"{len(extracted.get('projects', []))} projects · "
            f"{len(extracted.get('commitments', []))} commitments"
            "[/dim]"
        )
        return
    if args.set_telegram_webhook:
        from integrations.messaging import set_telegram_webhook

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            console.print("[red]TELEGRAM_BOT_TOKEN is required to register a webhook.[/red]")
            sys.exit(1)
        try:
            response = set_telegram_webhook(
                token,
                args.set_telegram_webhook,
                os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN"),
            )
        except Exception as exc:
            console.print(f"[red]Failed to register Telegram webhook: {exc}[/red]")
            sys.exit(1)
        console.print(f"[green]Telegram webhook configured:[/green] {json.dumps(response)}")
        return
    if args.serve_messaging:
        from integrations.messaging import create_messaging_server

        server = create_messaging_server(host=args.messaging_host, port=args.messaging_port)
        host, port = server.address
        console.print()
        console.print(Panel(
            (
                f"[bold]Messaging server listening on {host}:{port}[/bold]\n"
                f"[dim]Telegram:[/dim] POST /telegram/webhook\n"
                f"[dim]WhatsApp verify:[/dim] GET /whatsapp/webhook\n"
                f"[dim]WhatsApp inbound:[/dim] POST /whatsapp/webhook\n"
                f"[dim]Health:[/dim] GET /healthz\n"
                f"[dim]DM policy:[/dim] default is pairing unless PHANTOM_MESSAGING_DM_POLICY=open"
            ),
            title="[cyan bold]PHANTOM Messaging[/cyan bold]",
            border_style="cyan",
        ))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]Shutting down messaging server...[/dim]")
            server.shutdown()
        return
    if args.serve_gateway:
        from core.gateway import create_gateway

        gateway = create_gateway(host=args.gateway_host, port=args.gateway_port, max_workers=args.gateway_workers)
        host, port = gateway.address
        console.print()
        console.print(Panel(
            (
                f"[bold]PHANTOM gateway listening on {host}:{port}[/bold]\n"
                f"[dim]Sessions:[/dim] POST /sessions · GET /sessions · GET /sessions/<id>\n"
                f"[dim]Session events:[/dim] GET /sessions/<id>/events\n"
                f"[dim]Doctor:[/dim] GET /doctor\n"
                f"[dim]Health:[/dim] GET /healthz"
            ),
            title="[cyan bold]PHANTOM Gateway[/cyan bold]",
            border_style="cyan",
        ))
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            console.print("\n[dim]Shutting down gateway...[/dim]")
            gateway.stop()
        return
    if not args.goal and _stdin_is_tty():
        interactive_chat(args)
        return

    goal = resolve_goal(args.goal)
    if not goal:
        console.print("[red]Provide a goal.[/red] Example: python phantom.py \"build a web scraper\"")
        sys.exit(1)

    try:
        run_goal_command(goal, args)
    except (ModuleNotFoundError, EnvironmentError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
