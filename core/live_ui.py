"""Lightweight live run dashboard for PHANTOM."""

from __future__ import annotations

import json
import queue
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core.settings import redact_payload
from core.souls import soul_for


def _json_body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def _agent_card(role: str) -> dict[str, str]:
    soul = soul_for(role)
    return {
        "role": role,
        "name": soul.name,
        "title": soul.title,
        "color": soul.color,
        "state": "idle",
        "last": "",
    }


def _task_status_from_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized in {"success"}:
        return "completed"
    if normalized in {"failed", "budget_exceeded", "critic_blocked", "checkpoint_declined"}:
        return "failed"
    return "completed"


@dataclass
class LiveDashboard:
    """A local HTTP dashboard that mirrors the active PHANTOM run."""

    max_history: int = 80
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _listeners: set[queue.Queue[str | None]] = field(default_factory=set, repr=False, compare=False)
    _server: ThreadingHTTPServer | None = field(default=None, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _host: str = "127.0.0.1"
    _port: int = 0
    _snapshot: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._snapshot = {
            "status": "idle",
            "phase": "idle",
            "goal": "",
            "trace_id": "",
            "scope": "",
            "current_agent": "orchestrator",
            "current_activity": "Waiting to start",
            "current_task": "",
            "memory": {"episodes": 0, "demonstrations": 0},
            "briefing": {"people": 0, "projects": 0, "commitments": 0, "signals": 0},
            "procedures": [],
            "latest_tool": None,
            "metrics": {},
            "tasks": [],
            "history": [],
            "agents": {
                "planner": _agent_card("planner"),
                "executor": _agent_card("executor"),
                "critic": _agent_card("critic"),
                "synthesizer": _agent_card("synthesizer"),
                "orchestrator": _agent_card("orchestrator"),
            },
        }

    @property
    def address(self) -> tuple[str, int]:
        return self._host, self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "LiveDashboard":
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path in {"/", ""}:
                    self._send_html()
                    return
                if self.path == "/snapshot":
                    self._send_json(dashboard.snapshot())
                    return
                if self.path == "/events":
                    self._stream_events()
                    return
                if self.path == "/healthz":
                    self._send_json({"ok": True, "status": dashboard.snapshot()["status"]})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

            def _send_html(self) -> None:
                body = _DASHBOARD_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, payload: Any) -> None:
                body = _json_body(payload)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream_events(self) -> None:
                event_queue = dashboard._subscribe()
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(f"event: snapshot\ndata: {json.dumps(dashboard.snapshot(), ensure_ascii=True)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while True:
                        try:
                            payload = event_queue.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            continue
                        if payload is None:
                            break
                        self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    dashboard._unsubscribe(event_queue)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._host, self._port = self._server.server_address[:2]
        self._thread = threading.Thread(target=self._server.serve_forever, name="phantom-live-ui", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
            self._listeners.clear()
        for listener in listeners:
            listener.put(None)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._snapshot)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        clean = redact_payload(data)
        with self._lock:
            self._apply_event(event_type, clean)
            payload = json.dumps(self._snapshot, ensure_ascii=True)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(payload)
            except queue.Full:
                continue

    def _subscribe(self) -> queue.Queue[str | None]:
        listener: queue.Queue[str | None] = queue.Queue(maxsize=10)
        with self._lock:
            self._listeners.add(listener)
        return listener

    def _unsubscribe(self, listener: queue.Queue[str | None]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def _set_active_agent(self, role: str, message: str = "") -> None:
        normalized = str(role or "orchestrator").strip().lower() or "orchestrator"
        for name, card in self._snapshot["agents"].items():
            if name == normalized:
                card["state"] = "active"
                if message:
                    card["last"] = message
            elif card["state"] == "active":
                card["state"] = "idle"
        self._snapshot["current_agent"] = normalized

    def _ensure_tasks(self, data: dict[str, Any]) -> None:
        tasks = [str(item) for item in data.get("tasks", [])]
        graph = data.get("graph", [])
        mapped = []
        for index, entry in enumerate(graph):
            mapped.append({
                "id": str(entry.get("id", f"t{index + 1}")),
                "task": tasks[index] if index < len(tasks) else str(entry.get("task", "")),
                "depends_on": list(entry.get("depends_on", [])),
                "parallel": bool(entry.get("parallel", False)),
                "status": "planned",
            })
        if not mapped:
            mapped = [
                {"id": f"t{index + 1}", "task": task, "depends_on": [], "parallel": False, "status": "planned"}
                for index, task in enumerate(tasks)
            ]
        self._snapshot["tasks"] = mapped

    def _mark_task(self, *, task_id: str = "", task_text: str = "", status: str = "") -> None:
        for item in self._snapshot["tasks"]:
            if task_id and item["id"] == task_id:
                item["status"] = status
                return
            if task_text and item["task"] == task_text:
                item["status"] = status
                return
        if task_text:
            self._snapshot["tasks"].append({
                "id": task_id or f"t{len(self._snapshot['tasks']) + 1}",
                "task": task_text,
                "depends_on": [],
                "parallel": False,
                "status": status or "planned",
            })

    def _append_history(self, event_type: str, data: dict[str, Any]) -> None:
        entry = {
            "ts": time.time(),
            "type": event_type,
            "agent": str(data.get("agent") or self._snapshot.get("current_agent") or "orchestrator"),
            "text": self._describe_event(event_type, data),
        }
        history = self._snapshot["history"]
        history.append(entry)
        if len(history) > self.max_history:
            del history[: len(history) - self.max_history]

    def _describe_event(self, event_type: str, data: dict[str, Any]) -> str:
        if event_type == "start":
            return f"Run started for: {data.get('goal', '')}"
        if event_type == "memory":
            return (
                f"Loaded {data.get('episodes', 0)} memories and "
                f"{data.get('demonstrations', 0)} demonstrations"
            )
        if event_type == "briefing":
            return (
                f"Chief-of-staff context: {data.get('people', 0)} people, "
                f"{data.get('projects', 0)} projects, {data.get('commitments', 0)} commitments"
            )
        if event_type == "procedures":
            return f"Matched {len(data.get('matches', []))} reusable procedures"
        if event_type == "planning":
            return "Planner is decomposing the goal"
        if event_type == "plan":
            return f"Planner produced {len(data.get('tasks', []))} tasks"
        if event_type == "plan_approval_required":
            return "Waiting for human approval before execution"
        if event_type == "plan_approved":
            return "Plan approved, execution is starting"
        if event_type == "plan_revision_requested":
            return f"Human requested plan changes: {data.get('feedback', '')}"
        if event_type == "plan_revised":
            return f"Planner revised the plan to {len(data.get('tasks', []))} tasks"
        if event_type == "executing":
            return f"Executing task: {data.get('task', '')}"
        if event_type == "procedure_selected":
            return (
                f"Reusing learned procedure #{data.get('demo_id')} "
                f"(confidence {data.get('confidence', 0.0):.2f})"
            )
        if event_type == "tool":
            return f"Tool call: {data.get('name', '')}"
        if event_type == "tool_result":
            result = "ok" if not data.get("error") else "failed"
            return f"Tool result: {data.get('name', '')} {result}"
        if event_type == "critic":
            return data.get("issue", "Critic requested a change")
        if event_type == "task_done":
            return f"Task finished: {data.get('task', '')}"
        if event_type == "replanning":
            return f"Replanning because: {data.get('reason', '')}"
        if event_type == "replan":
            return f"New plan contains {len(data.get('tasks', []))} tasks"
        if event_type == "synthesizing":
            return "Synthesizing the final answer"
        if event_type == "done":
            return f"Run finished with outcome: {data.get('outcome', 'unknown')}"
        if event_type == "warn":
            return f"Warning: {data.get('message', '')}"
        if event_type == "planning_error":
            return f"Planning error: {data.get('error', '')}"
        if event_type == "halted":
            return f"Run halted: {data.get('reason', '')}"
        if event_type == "soul":
            return data.get("intro", "")
        if event_type == "text":
            return str(data.get("text", ""))[:160]
        return json.dumps(data, ensure_ascii=True)[:160]

    def _apply_event(self, event_type: str, data: dict[str, Any]) -> None:
        agent = str(data.get("agent") or self._snapshot.get("current_agent") or "orchestrator")
        if event_type == "start":
            self._snapshot["status"] = "running"
            self._snapshot["phase"] = "starting"
            self._snapshot["goal"] = str(data.get("goal", ""))
            self._snapshot["trace_id"] = str(data.get("trace_id", ""))
            self._snapshot["scope"] = str(data.get("scope", ""))
            self._snapshot["current_activity"] = "Starting run"
            self._set_active_agent("orchestrator", "Starting run")
        elif event_type == "memory":
            self._snapshot["memory"] = {
                "episodes": int(data.get("episodes", 0)),
                "demonstrations": int(data.get("demonstrations", 0)),
            }
            self._snapshot["current_activity"] = "Loading memory and prior demonstrations"
        elif event_type == "briefing":
            self._snapshot["briefing"] = {
                "people": int(data.get("people", 0)),
                "projects": int(data.get("projects", 0)),
                "commitments": int(data.get("commitments", 0)),
                "signals": int(data.get("signals", 0)),
            }
        elif event_type == "procedures":
            self._snapshot["procedures"] = list(data.get("matches", []))[:5]
            self._snapshot["current_activity"] = "Checking for reusable procedures"
        elif event_type == "planning":
            self._snapshot["phase"] = "planning"
            self._snapshot["current_activity"] = "Planner is mapping the work"
            self._set_active_agent("planner", "Building task waves")
        elif event_type == "plan":
            self._ensure_tasks(data)
            self._snapshot["current_activity"] = f"Plan ready with {len(self._snapshot['tasks'])} tasks"
        elif event_type == "plan_approval_required":
            self._snapshot["phase"] = "approval"
            self._snapshot["current_activity"] = "Waiting for human plan approval"
        elif event_type == "plan_approved":
            self._snapshot["phase"] = "executing"
            self._snapshot["current_activity"] = "Plan approved"
        elif event_type == "plan_declined":
            self._snapshot["status"] = "cancelled"
            self._snapshot["phase"] = "cancelled"
            self._snapshot["current_activity"] = "Plan declined before execution"
        elif event_type == "plan_revision_requested":
            self._snapshot["phase"] = "revising"
            self._snapshot["current_activity"] = "Revising the plan from human feedback"
            self._set_active_agent("planner", "Revising plan")
        elif event_type == "plan_revised":
            self._ensure_tasks(data)
            self._snapshot["phase"] = "approval"
            self._snapshot["current_activity"] = "Revised plan ready for review"
        elif event_type == "wave":
            self._snapshot["current_activity"] = f"Dispatching wave of {len(data.get('tasks', []))} task(s)"
        elif event_type == "executing":
            task = str(data.get("task", ""))
            self._snapshot["phase"] = "executing"
            self._snapshot["current_task"] = task
            self._snapshot["current_activity"] = f"Executing: {task}"
            self._mark_task(task_id=str(data.get("task_id", "")), task_text=task, status="running")
            self._set_active_agent("executor", task)
        elif event_type == "procedure_selected":
            self._snapshot["current_activity"] = f"Reusing procedure #{data.get('demo_id')}"
            self._set_active_agent("executor", self._snapshot["current_activity"])
        elif event_type == "tool":
            self._snapshot["latest_tool"] = {
                "name": str(data.get("name", "")),
                "inputs": data.get("inputs", {}),
            }
            self._snapshot["current_activity"] = f"Running tool: {data.get('name', '')}"
            self._set_active_agent(agent, self._snapshot["current_activity"])
        elif event_type == "tool_result":
            state = "Tool finished" if not data.get("error") else "Tool failed"
            self._snapshot["current_activity"] = f"{state}: {data.get('name', '')}"
        elif event_type == "critic":
            self._snapshot["phase"] = "critic"
            self._snapshot["current_activity"] = "Critic is reviewing the current approach"
            self._set_active_agent("critic", str(data.get("issue", "")))
        elif event_type == "task_done":
            task = str(data.get("task", ""))
            outcome = str(data.get("outcome", "success"))
            self._mark_task(task_id=str(data.get("id", "")), task_text=task, status=_task_status_from_outcome(outcome))
            self._snapshot["current_task"] = task
            self._snapshot["current_activity"] = f"Completed: {task}"
            self._snapshot["phase"] = "executing"
            self._set_active_agent("executor", self._snapshot["current_activity"])
        elif event_type == "replanning":
            self._snapshot["phase"] = "replanning"
            self._snapshot["current_activity"] = f"Replanning: {data.get('reason', '')}"
            self._set_active_agent("planner", self._snapshot["current_activity"])
        elif event_type == "replan":
            self._ensure_tasks(data)
            self._snapshot["phase"] = "executing"
            self._snapshot["current_activity"] = "Replan accepted"
        elif event_type == "synthesizing":
            self._snapshot["phase"] = "synthesizing"
            self._snapshot["current_activity"] = "Synthesizing the final answer"
            self._set_active_agent("synthesizer", "Combining results")
        elif event_type == "done":
            self._snapshot["status"] = str(data.get("outcome", "done"))
            self._snapshot["phase"] = "done"
            self._snapshot["current_activity"] = "Run finished"
            self._snapshot["metrics"] = dict(data.get("metrics", {}))
            self._set_active_agent("orchestrator", "Run finished")
        elif event_type == "warn":
            self._snapshot["current_activity"] = f"Warning: {data.get('message', '')}"
        elif event_type == "planning_error":
            self._snapshot["status"] = "failure"
            self._snapshot["phase"] = "error"
            self._snapshot["current_activity"] = f"Planning error: {data.get('error', '')}"
        elif event_type == "halted":
            self._snapshot["status"] = "halted"
            self._snapshot["phase"] = "halted"
            self._snapshot["current_activity"] = f"Halted: {data.get('reason', '')}"
        elif event_type == "soul":
            self._set_active_agent(agent, str(data.get("intro", "")))
        elif event_type == "text" and data.get("text"):
            self._snapshot["current_activity"] = str(data.get("text", ""))[:160]
        self._append_history(event_type, data)


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PHANTOM Live Run</title>
  <style>
    :root {
      --bg0: #08111f;
      --bg1: #0f1b31;
      --panel: rgba(10, 18, 34, 0.76);
      --line: rgba(130, 170, 255, 0.18);
      --text: #ebf1ff;
      --muted: #9caed3;
      --accent: #67d1ff;
      --success: #57d38c;
      --warn: #f7c76b;
      --danger: #ff7a7a;
      --shadow: 0 30px 80px rgba(2, 6, 14, 0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "SF Pro Display", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(103, 209, 255, 0.12), transparent 32%),
        radial-gradient(circle at top right, rgba(87, 211, 140, 0.10), transparent 28%),
        linear-gradient(180deg, var(--bg1), var(--bg0));
      min-height: 100vh;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(255, 255, 255, 0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 255, 255, 0.03) 1px, transparent 1px);
      background-size: 34px 34px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.25), rgba(0,0,0,0.9));
      pointer-events: none;
    }
    .shell {
      width: min(1380px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 44px;
      position: relative;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.4fr 0.9fr;
      gap: 20px;
      margin-bottom: 20px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 22px 22px 20px;
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow);
    }
    .kicker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      border: 1px solid rgba(103, 209, 255, 0.24);
      border-radius: 999px;
      padding: 8px 14px;
      color: var(--accent);
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 20px rgba(103, 209, 255, 0.6);
      animation: pulse 1.6s ease-in-out infinite;
    }
    h1 {
      margin: 16px 0 8px;
      font-size: clamp(30px, 4vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }
    .goal {
      font-size: 18px;
      color: var(--muted);
      line-height: 1.5;
      max-width: 70ch;
    }
    .meta-grid, .metric-grid, .agents, .content-grid {
      display: grid;
      gap: 16px;
    }
    .meta-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 20px; }
    .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .meta, .metric {
      border: 1px solid rgba(156, 174, 211, 0.18);
      border-radius: 18px;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.02);
    }
    .eyebrow {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .value {
      font-size: 20px;
      margin-top: 8px;
      font-weight: 600;
      letter-spacing: -0.02em;
    }
    .agents { grid-template-columns: repeat(5, minmax(0, 1fr)); margin-bottom: 18px; }
    .agent {
      position: relative;
      overflow: hidden;
      min-height: 144px;
    }
    .agent::after {
      content: "";
      position: absolute;
      inset: auto -40% -70px -40%;
      height: 120px;
      background: radial-gradient(circle, rgba(103, 209, 255, 0.25), transparent 65%);
      opacity: 0;
      transition: opacity 160ms ease;
      pointer-events: none;
    }
    .agent.active::after {
      opacity: 1;
      animation: drift 2.4s linear infinite;
    }
    .agent-name {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 24px;
      letter-spacing: -0.03em;
      margin-bottom: 10px;
    }
    .agent-state {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
    }
    .agent.active .agent-state { color: var(--accent); }
    .agent-last {
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
      min-height: 64px;
    }
    .content-grid { grid-template-columns: 1.1fr 0.95fr; }
    .stack { display: grid; gap: 16px; }
    .task-list, .feed-list {
      display: grid;
      gap: 10px;
      margin-top: 14px;
      max-height: 420px;
      overflow: auto;
      padding-right: 4px;
    }
    .task, .feed-item {
      border: 1px solid rgba(156, 174, 211, 0.16);
      border-radius: 16px;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.02);
    }
    .task-top, .feed-top {
      display: flex;
      gap: 12px;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .task-id, .feed-tag {
      color: var(--accent);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .task-status {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      border-radius: 999px;
      padding: 6px 10px;
      border: 1px solid rgba(255,255,255,0.12);
    }
    .status-planned { color: var(--muted); }
    .status-running { color: var(--accent); border-color: rgba(103, 209, 255, 0.36); }
    .status-completed { color: var(--success); border-color: rgba(87, 211, 140, 0.30); }
    .status-failed { color: var(--danger); border-color: rgba(255, 122, 122, 0.30); }
    .task-text, .feed-text {
      line-height: 1.5;
      color: var(--text);
    }
    .feed-meta {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }
    .pill {
      border: 1px solid rgba(103, 209, 255, 0.22);
      border-radius: 999px;
      padding: 8px 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.02);
      font-size: 13px;
    }
    .empty {
      color: var(--muted);
      padding: 16px 0 6px;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(0.88); opacity: 0.55; }
      50% { transform: scale(1.08); opacity: 1; }
    }
    @keyframes drift {
      0% { transform: translateX(-16%); }
      100% { transform: translateX(16%); }
    }
    @media (max-width: 1100px) {
      .hero, .content-grid, .agents, .meta-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="panel">
        <div class="kicker"><span class="dot"></span><span id="status-line">PHANTOM live run</span></div>
        <h1 id="phase-title">Waiting for run</h1>
        <div class="goal" id="goal-text">No active run yet.</div>
        <div class="meta-grid">
          <div class="meta"><div class="eyebrow">Current activity</div><div class="value" id="current-activity">Idle</div></div>
          <div class="meta"><div class="eyebrow">Current task</div><div class="value" id="current-task">None</div></div>
          <div class="meta"><div class="eyebrow">Trace and scope</div><div class="value" id="trace-scope">Waiting</div></div>
        </div>
      </div>
      <div class="panel">
        <div class="eyebrow">Run metrics</div>
        <div class="metric-grid" style="margin-top:14px;">
          <div class="metric"><div class="eyebrow">LLM calls</div><div class="value" id="metric-llm">0</div></div>
          <div class="metric"><div class="eyebrow">Tool calls</div><div class="value" id="metric-tools">0</div></div>
          <div class="metric"><div class="eyebrow">Duration</div><div class="value" id="metric-duration">0 ms</div></div>
          <div class="metric"><div class="eyebrow">Outcome</div><div class="value" id="metric-outcome">Idle</div></div>
        </div>
        <div class="pill-row" id="memory-pills"></div>
      </div>
    </section>

    <section class="agents" id="agents"></section>

    <section class="content-grid">
      <div class="stack">
        <div class="panel">
          <div class="eyebrow">Task waveboard</div>
          <div class="task-list" id="tasks"></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <div class="eyebrow">Live event feed</div>
          <div class="feed-list" id="feed"></div>
        </div>
      </div>
    </section>
  </div>

  <script>
    const AGENT_ORDER = ["planner", "executor", "critic", "synthesizer", "orchestrator"];

    function titleCase(value) {
      const text = String(value || "");
      if (!text) return "Idle";
      return text.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());
    }

    function formatTime(ts) {
      const value = Number(ts || 0);
      if (!value) return "";
      return new Date(value * 1000).toLocaleTimeString();
    }

    function setText(id, value) {
      const node = document.getElementById(id);
      if (node) node.textContent = value;
    }

    function renderAgents(snapshot) {
      const root = document.getElementById("agents");
      root.innerHTML = "";
      const agents = snapshot.agents || {};
      for (const role of AGENT_ORDER) {
        const agent = agents[role];
        if (!agent) continue;
        const card = document.createElement("div");
        card.className = "panel agent" + (agent.state === "active" ? " active" : "");
        card.innerHTML = `
          <div class="agent-name">
            <span>${agent.name}</span>
            <span class="agent-state">${titleCase(agent.state)}</span>
          </div>
          <div class="eyebrow">${titleCase(role)}</div>
          <div class="agent-last">${agent.last || "Standing by."}</div>
        `;
        root.appendChild(card);
      }
    }

    function renderTasks(snapshot) {
      const root = document.getElementById("tasks");
      root.innerHTML = "";
      const tasks = snapshot.tasks || [];
      if (!tasks.length) {
        root.innerHTML = `<div class="empty">No task graph yet.</div>`;
        return;
      }
      for (const task of tasks) {
        const node = document.createElement("div");
        const status = String(task.status || "planned");
        node.className = "task";
        node.innerHTML = `
          <div class="task-top">
            <span class="task-id">${task.id || "task"}</span>
            <span class="task-status status-${status}">${titleCase(status)}</span>
          </div>
          <div class="task-text">${task.task || ""}</div>
          <div class="feed-meta">deps: ${(task.depends_on || []).join(", ") || "none"} | ${(task.parallel ? "parallel" : "serial")}</div>
        `;
        root.appendChild(node);
      }
    }

    function renderFeed(snapshot) {
      const root = document.getElementById("feed");
      root.innerHTML = "";
      const feed = (snapshot.history || []).slice().reverse();
      if (!feed.length) {
        root.innerHTML = `<div class="empty">Waiting for events.</div>`;
        return;
      }
      for (const item of feed) {
        const node = document.createElement("div");
        node.className = "feed-item";
        node.innerHTML = `
          <div class="feed-top">
            <span class="feed-tag">${titleCase(item.agent || "orchestrator")}</span>
            <span class="feed-meta">${formatTime(item.ts)}</span>
          </div>
          <div class="feed-text">${item.text || ""}</div>
        `;
        root.appendChild(node);
      }
    }

    function renderMemory(snapshot) {
      const root = document.getElementById("memory-pills");
      root.innerHTML = "";
      const memory = snapshot.memory || {};
      const briefing = snapshot.briefing || {};
      const procedures = snapshot.procedures || [];
      const latestTool = snapshot.latest_tool;
      const pills = [
        `${memory.episodes || 0} memories`,
        `${memory.demonstrations || 0} demonstrations`,
        `${briefing.people || 0} people`,
        `${briefing.projects || 0} projects`,
        `${briefing.commitments || 0} commitments`,
        `${briefing.signals || 0} signals`,
        `${procedures.length || 0} procedure matches`,
      ];
      if (latestTool && latestTool.name) {
        pills.push(`tool: ${latestTool.name}`);
      }
      for (const text of pills) {
        const pill = document.createElement("div");
        pill.className = "pill";
        pill.textContent = text;
        root.appendChild(pill);
      }
    }

    function render(snapshot) {
      setText("status-line", `PHANTOM ${titleCase(snapshot.status)} | ${titleCase(snapshot.phase)}`);
      setText("phase-title", titleCase(snapshot.phase || "idle"));
      setText("goal-text", snapshot.goal || "No active run yet.");
      setText("current-activity", snapshot.current_activity || "Idle");
      setText("current-task", snapshot.current_task || "None");
      setText("trace-scope", `${snapshot.trace_id || "no-trace"} | ${snapshot.scope || "default"}`);
      const metrics = snapshot.metrics || {};
      setText("metric-llm", String(metrics.llm_calls || 0));
      setText("metric-tools", String(metrics.tool_calls || 0));
      setText("metric-duration", `${metrics.duration_ms || 0} ms`);
      setText("metric-outcome", titleCase(snapshot.status || "idle"));
      renderMemory(snapshot);
      renderAgents(snapshot);
      renderTasks(snapshot);
      renderFeed(snapshot);
    }

    async function boot() {
      const initial = await fetch("/snapshot", { cache: "no-store" }).then((res) => res.json());
      render(initial);
      const stream = new EventSource("/events");
      stream.addEventListener("snapshot", (event) => {
        render(JSON.parse(event.data));
      });
      stream.onerror = () => {
        setText("status-line", "PHANTOM disconnected");
      };
    }

    boot();
  </script>
</body>
</html>
"""
