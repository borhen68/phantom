"""Persistent HTTP control plane for PHANTOM sessions."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from core.doctor import doctor_report
from core.settings import override_scope, override_workspace, redact_payload, scope_id, workspace_root


def _json_body(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


@dataclass
class GatewaySession:
    session_id: str
    goal: str
    workspace: str
    scope: str
    parallel: bool = True
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    outcome: str = ""
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _listeners: set[queue.Queue[str | None]] = field(default_factory=set, repr=False, compare=False)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        clean = redact_payload(data)
        with self._lock:
            self.updated_at = time.time()
            if event_type == "start":
                self.status = "running"
            elif event_type == "done":
                self.status = str(clean.get("outcome") or "done")
                self.outcome = str(clean.get("outcome") or "")
                self.summary = str(clean.get("summary") or "")
                self.metrics = dict(clean.get("metrics") or {})
            elif event_type == "halted":
                self.status = "halted"
            elif event_type == "planning_error":
                self.status = "failure"
            self.history.append({
                "ts": self.updated_at,
                "type": event_type,
                "payload": clean,
            })
            if len(self.history) > 120:
                del self.history[: len(self.history) - 120]
            payload = json.dumps({
                "session_id": self.session_id,
                "goal": self.goal,
                "workspace": self.workspace,
                "scope": self.scope,
                "parallel": self.parallel,
                "status": self.status,
                "outcome": self.outcome,
                "summary": self.summary,
                "metrics": deepcopy(self.metrics),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "history": deepcopy(self.history),
            }, ensure_ascii=True)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener.put_nowait(payload)
            except queue.Full:
                continue

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "goal": self.goal,
                "workspace": self.workspace,
                "scope": self.scope,
                "parallel": self.parallel,
                "status": self.status,
                "outcome": self.outcome,
                "summary": self.summary,
                "metrics": deepcopy(self.metrics),
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "history": deepcopy(self.history),
            }

    def subscribe(self) -> queue.Queue[str | None]:
        listener: queue.Queue[str | None] = queue.Queue(maxsize=20)
        with self._lock:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue[str | None]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def close(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
            self._listeners.clear()
        for listener in listeners:
            listener.put(None)


@dataclass
class PhantomGateway:
    max_history: int = 40
    max_workers: int = 4
    _sessions: dict[str, GatewaySession] = field(default_factory=dict, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _executor: object | None = field(default=None, init=False, repr=False, compare=False)
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False, compare=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False, compare=False)
    _host: str = "127.0.0.1"
    _port: int = 0

    @property
    def address(self) -> tuple[str, int]:
        return self._host, self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "PhantomGateway":
        from concurrent.futures import ThreadPoolExecutor

        gateway = self
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="phantom-gateway")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"", "/"}:
                    self._send_json({"ok": True, "gateway": gateway.url, "sessions": gateway.list_sessions()})
                    return
                if parsed.path == "/healthz":
                    self._send_json({"ok": True, "sessions": len(gateway.list_sessions()), "status": "running"})
                    return
                if parsed.path == "/doctor":
                    self._send_json(doctor_report())
                    return
                if parsed.path == "/sessions":
                    self._send_json({"sessions": gateway.list_sessions()})
                    return
                if parsed.path.startswith("/sessions/"):
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) == 2:
                        session = gateway.get_session(parts[1])
                        if session is None:
                            self.send_error(HTTPStatus.NOT_FOUND)
                            return
                        self._send_json(session.snapshot())
                        return
                    if len(parts) == 3 and parts[2] == "events":
                        session = gateway.get_session(parts[1])
                        if session is None:
                            self.send_error(HTTPStatus.NOT_FOUND)
                            return
                        self._stream_events(session)
                        return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/sessions":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                payload = self._read_json()
                if not isinstance(payload, dict) or not str(payload.get("goal") or "").strip():
                    self.send_error(HTTPStatus.BAD_REQUEST, "Expected JSON object with non-empty 'goal'.")
                    return
                session = gateway.submit(
                    goal=str(payload.get("goal")).strip(),
                    workspace=str(payload.get("workspace") or workspace_root()).strip(),
                    scope=str(payload.get("scope") or "").strip() or None,
                    parallel=bool(payload.get("parallel", True)),
                )
                self._send_json(session.snapshot(), status=HTTPStatus.ACCEPTED)

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

            def _read_json(self) -> Any:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(length) if length > 0 else b""
                if not body:
                    return {}
                try:
                    return json.loads(body.decode("utf-8"))
                except Exception:
                    return None

            def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = _json_body(payload)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream_events(self, session: GatewaySession) -> None:
                listener = session.subscribe()
                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(f"event: snapshot\ndata: {json.dumps(session.snapshot(), ensure_ascii=True)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while True:
                        try:
                            payload = listener.get(timeout=15)
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
                    session.unsubscribe(listener)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._host, self._port = self._server.server_address[:2]
        self._thread = threading.Thread(target=self._server.serve_forever, name="phantom-gateway", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            session.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
        return [
            {
                "session_id": item.session_id,
                "goal": item.goal,
                "workspace": item.workspace,
                "scope": item.scope,
                "status": item.status,
                "outcome": item.outcome,
                "updated_at": item.updated_at,
            }
            for item in items[: self.max_history]
        ]

    def get_session(self, session_id: str) -> GatewaySession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def submit(self, *, goal: str, workspace: str, scope: str | None = None, parallel: bool = True) -> GatewaySession:
        if self._executor is None:
            raise RuntimeError("Gateway is not running.")
        session_id = uuid.uuid4().hex[:12]
        session_scope = scope or f"gateway::{session_id}"
        session = GatewaySession(
            session_id=session_id,
            goal=goal,
            workspace=workspace,
            scope=session_scope,
            parallel=parallel,
        )
        with self._lock:
            self._sessions[session_id] = session
        self._executor.submit(self._run_session, session)
        return session

    def _run_session(self, session: GatewaySession) -> None:
        from core.orchestrator import run

        try:
            with override_workspace(session.workspace), override_scope(session.scope):
                result = run(goal=session.goal, on_event=session.publish, parallel=session.parallel)
            if not session.outcome:
                session.publish("done", {
                    "outcome": result.get("outcome", ""),
                    "summary": result.get("summary", ""),
                    "metrics": result.get("metrics", {}),
                })
        except Exception as exc:
            session.publish("done", {
                "outcome": "failure",
                "summary": f"Gateway session failed: {exc}",
                "metrics": {},
            })
        finally:
            session.close()


def create_gateway(host: str = "127.0.0.1", port: int = 8787, *, max_workers: int = 4) -> PhantomGateway:
    return PhantomGateway(max_workers=max_workers).start(host=host, port=port)
