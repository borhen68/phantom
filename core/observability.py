"""Structured trace logging and replay helpers."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.settings import redact_payload, trace_root


@dataclass
class TraceRecorder:
    goal: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self):
        if self.path is None:
            self.path = trace_root() / f"{self.trace_id}.jsonl"

    def record(self, event_type: str, payload: dict[str, Any], *, agent: str | None = None) -> None:
        envelope = {
            "ts": time.time(),
            "trace_id": self.trace_id,
            "goal": self.goal,
            "agent": agent,
            "event_type": event_type,
            "payload": redact_payload(payload),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=True) + "\n")


def replay_trace(trace_id: str) -> list[dict[str, Any]]:
    path = trace_root() / f"{trace_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"Trace {trace_id} not found at {path}")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events
