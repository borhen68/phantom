"""
PHANTOM Memory — scoped persistent state with history and versioning.

State is isolated per workspace/user scope so runs from different projects do
not collide inside the same local database.
"""
import json
import os
import re
import shutil
import sqlite3
import time
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from core.contracts import ProcedureMatch
from core.settings import data_root as configured_data_root
from core.settings import scope_id, skill_root

DB = Path.home() / ".phantom" / "memory.db"
LATEST_SCHEMA_VERSION = 6
DEMO_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "then",
    "when", "your", "have", "after", "before", "show", "used", "step",
    "click", "open", "using", "into", "over", "under",
}
EXECUTABLE_DEMO_ACTIONS = {
    "shell",
    "read_file",
    "write_file",
    "remember",
    "web_search",
    "browser_goto",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_wait_for",
    "browser_extract_text",
    "browser_assert_text",
    "browser_screenshot",
}


def data_dir() -> Path:
    return configured_data_root()


def db_path() -> Path:
    return data_dir() / DB.name


@contextmanager
def _conn():
    database = db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _scope() -> str:
    return scope_id()


def _safe_scope_fragment(scope: str) -> str:
    chars = [char if char.isalnum() else "_" for char in scope]
    return "".join(chars)[:80] or "default_scope"


def demonstration_root() -> Path:
    root = data_dir() / "demonstrations" / _safe_scope_fragment(_scope())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _limit(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def init():
    with _conn() as connection:
        _run_migrations(connection)
    _prune_scope()


def _create_base_tables(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        ts REAL,
        goal TEXT,
        outcome TEXT,
        summary TEXT,
        lessons TEXT
    );
    CREATE TABLE IF NOT EXISTS world_facts (
        scope TEXT,
        key TEXT,
        value TEXT,
        ts REAL,
        confidence REAL DEFAULT 1.0,
        version INTEGER DEFAULT 1,
        conflicts INTEGER DEFAULT 0,
        expires_at REAL,
        PRIMARY KEY (scope, key)
    );
    CREATE TABLE IF NOT EXISTS world_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        key TEXT,
        value TEXT,
        ts REAL,
        confidence REAL DEFAULT 1.0,
        version INTEGER,
        source TEXT
    );
    CREATE TABLE IF NOT EXISTS skills_current (
        scope TEXT,
        name TEXT,
        description TEXT,
        code TEXT,
        ts REAL,
        uses INTEGER DEFAULT 0,
        failures INTEGER DEFAULT 0,
        current_version INTEGER DEFAULT 1,
        PRIMARY KEY (scope, name)
    );
    CREATE TABLE IF NOT EXISTS skill_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        name TEXT,
        version INTEGER,
        description TEXT,
        code TEXT,
        ts REAL
    );
    CREATE TABLE IF NOT EXISTS tool_stats (
        scope TEXT,
        tool TEXT,
        calls INTEGER DEFAULT 0,
        failures INTEGER DEFAULT 0,
        PRIMARY KEY (scope, tool)
    );
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        ts REAL,
        goal TEXT,
        outcome TEXT,
        duration_ms INTEGER,
        tasks_planned INTEGER,
        tasks_completed INTEGER,
        tool_calls INTEGER,
        tool_errors INTEGER,
        critic_blocks INTEGER,
        planner_fallback INTEGER,
        parallel INTEGER,
        metrics TEXT,
        summary TEXT
    );
    CREATE TABLE IF NOT EXISTS demonstrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        ts REAL,
        goal TEXT,
        summary TEXT,
        steps TEXT,
        screenshots TEXT,
        source TEXT DEFAULT 'human',
        uses INTEGER DEFAULT 0
    );
    """)


def _migration_v1(connection):
    _create_base_tables(connection)


def _migration_v2(connection):
    _create_base_tables(connection)
    _ensure_column(connection, "demonstrations", "app", "TEXT DEFAULT ''")
    _ensure_column(connection, "demonstrations", "environment", "TEXT DEFAULT ''")
    _ensure_column(connection, "demonstrations", "tags", "TEXT DEFAULT '[]'")
    _ensure_column(connection, "demonstrations", "permissions", "TEXT DEFAULT '[]'")
    _ensure_column(connection, "demonstrations", "correction_of", "INTEGER")
    _ensure_column(connection, "demonstrations", "last_used", "REAL")
    _ensure_column(connection, "demonstrations", "last_confidence", "REAL DEFAULT 0.0")
    _ensure_column(connection, "demonstrations", "success_count", "INTEGER DEFAULT 0")
    _ensure_column(connection, "demonstrations", "failure_count", "INTEGER DEFAULT 0")
    _ensure_column(connection, "demonstrations", "last_replay_ts", "REAL")
    _ensure_column(connection, "demonstrations", "last_replay_status", "TEXT DEFAULT ''")
    _ensure_column(connection, "demonstrations", "last_replay_note", "TEXT DEFAULT ''")
    _ensure_column(connection, "demonstrations", "last_drift", "TEXT DEFAULT ''")
    _ensure_column(connection, "demonstrations", "schema_version", "INTEGER DEFAULT 2")


def _migration_v3(connection):
    connection.executescript("""
    CREATE INDEX IF NOT EXISTS idx_episodes_scope_ts ON episodes(scope, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_scope_ts ON runs(scope, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_world_history_scope_ts ON world_history(scope, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_demonstrations_scope_ts ON demonstrations(scope, ts DESC);
    """)


def _migration_v4(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS msg_dedupe (
        key TEXT PRIMARY KEY,
        seen_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_msg_dedupe_seen_at ON msg_dedupe(seen_at);
    """)


def _migration_v5(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS people (
        scope TEXT,
        name TEXT,
        relationship TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        aliases TEXT DEFAULT '[]',
        ts REAL NOT NULL,
        last_seen REAL NOT NULL,
        source TEXT DEFAULT 'human',
        PRIMARY KEY (scope, name)
    );
    CREATE TABLE IF NOT EXISTS projects (
        scope TEXT,
        name TEXT,
        status TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        ts REAL NOT NULL,
        last_active REAL NOT NULL,
        source TEXT DEFAULT 'human',
        PRIMARY KEY (scope, name)
    );
    CREATE TABLE IF NOT EXISTS commitments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        title TEXT NOT NULL,
        owner TEXT DEFAULT 'user',
        counterparty TEXT DEFAULT '',
        project TEXT DEFAULT '',
        due_at TEXT DEFAULT '',
        status TEXT DEFAULT 'open',
        notes TEXT DEFAULT '',
        ts REAL NOT NULL,
        updated_at REAL NOT NULL,
        source TEXT DEFAULT 'human'
    );
    CREATE INDEX IF NOT EXISTS idx_people_scope_seen ON people(scope, last_seen DESC);
    CREATE INDEX IF NOT EXISTS idx_projects_scope_active ON projects(scope, last_active DESC);
    CREATE INDEX IF NOT EXISTS idx_commitments_scope_updated ON commitments(scope, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_commitments_scope_status ON commitments(scope, status, updated_at DESC);
    """)


def _migration_v6(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS ingested_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        title TEXT DEFAULT '',
        content TEXT NOT NULL,
        metadata TEXT DEFAULT '{}',
        extracted TEXT DEFAULT '{}',
        ts REAL NOT NULL,
        happened_at TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_ingested_signals_scope_ts ON ingested_signals(scope, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_ingested_signals_scope_kind ON ingested_signals(scope, kind, ts DESC);
    """)


MIGRATIONS = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
}


def _run_migrations(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
    """)
    row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    current = int(row["version"] or 0) if row else 0
    for version, migrate in sorted(MIGRATIONS.items()):
        if version <= current:
            continue
        migrate(connection)
        connection.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )


def _ensure_column(connection, table: str, column: str, ddl: str):
    columns = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _prune_scope():
    scope = _scope()
    max_episodes = _limit("PHANTOM_MAX_EPISODES", 250)
    max_runs = _limit("PHANTOM_MAX_RUNS", 100)
    max_world_history = _limit("PHANTOM_MAX_WORLD_HISTORY", 500)
    max_skill_versions = _limit("PHANTOM_MAX_SKILL_VERSIONS", 20)
    max_demonstrations = _limit("PHANTOM_MAX_DEMONSTRATIONS", 100)
    max_signals = _limit("PHANTOM_MAX_INGESTED_SIGNALS", 300)

    with _conn() as connection:
        connection.execute(
            """
            DELETE FROM episodes
            WHERE scope=? AND id NOT IN (
                SELECT id FROM episodes WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_episodes),
        )
        connection.execute(
            """
            DELETE FROM runs
            WHERE scope=? AND id NOT IN (
                SELECT id FROM runs WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_runs),
        )
        connection.execute(
            """
            DELETE FROM world_history
            WHERE scope=? AND id NOT IN (
                SELECT id FROM world_history WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_world_history),
        )
        connection.execute(
            """
            DELETE FROM skill_versions
            WHERE scope=? AND id NOT IN (
                SELECT id FROM skill_versions WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_skill_versions),
        )
        connection.execute(
            """
            DELETE FROM demonstrations
            WHERE scope=? AND id NOT IN (
                SELECT id FROM demonstrations WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_demonstrations),
        )
        connection.execute(
            """
            DELETE FROM ingested_signals
            WHERE scope=? AND id NOT IN (
                SELECT id FROM ingested_signals WHERE scope=? ORDER BY ts DESC LIMIT ?
            )
            """,
            (scope, scope, max_signals),
        )


def save_episode(goal, outcome, summary, lessons: list):
    with _conn() as connection:
        connection.execute(
            "INSERT INTO episodes (scope,ts,goal,outcome,summary,lessons) VALUES (?,?,?,?,?,?)",
            (_scope(), time.time(), goal, outcome, summary, json.dumps(lessons)),
        )
    _prune_scope()


def recall(goal: str, limit=4) -> list[dict]:
    keywords = [word.lower() for word in goal.split() if len(word) > 3]
    with _conn() as connection:
        rows = connection.execute(
            "SELECT * FROM episodes WHERE scope=? ORDER BY ts DESC LIMIT 60",
            (_scope(),),
        ).fetchall()

    scored = []
    for row in rows:
        episode = dict(row)
        haystack = f"{episode['goal']} {episode['summary']}".lower()
        score = sum(1 for keyword in keywords if keyword in haystack)
        if score:
            scored.append((score, episode["ts"], episode))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [episode for _, _, episode in scored[:limit]]


def recent_episodes(limit=10) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            "SELECT * FROM episodes WHERE scope=? ORDER BY ts DESC LIMIT ?",
            (_scope(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _copy_demonstration_assets(paths: list[str]) -> list[str]:
    copied: list[dict] = []
    root = demonstration_root()
    stamp = int(time.time() * 1000)
    for index, raw_item in enumerate(paths or [], start=1):
        if isinstance(raw_item, dict):
            raw_path = str(raw_item.get("path") or "").strip()
            caption = str(raw_item.get("caption") or "").strip()
        else:
            raw_text = str(raw_item or "").strip()
            raw_path, _, caption = raw_text.partition("::")
            raw_path = raw_path.strip()
            caption = caption.strip()
        source = Path(raw_path).expanduser().resolve(strict=False)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Demonstration screenshot not found: {source}")
        stem = "".join(char if char.isalnum() else "_" for char in source.stem)[:40] or "asset"
        suffix = source.suffix[:16]
        target = root / f"{stamp}_{index}_{stem}{suffix}"
        shutil.copy2(source, target)
        copied.append({
            "path": str(target),
            "caption": caption,
            "analysis": _analyze_screenshot_file(target, caption=caption),
        })
    return copied


def _png_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        data = path.read_bytes()[:24]
    except Exception:
        return None, None
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None, None


def _jpeg_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return None, None
            while True:
                marker_prefix = handle.read(1)
                if not marker_prefix:
                    return None, None
                if marker_prefix != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3", b"\xc5", b"\xc6", b"\xc7", b"\xc9", b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"}:
                    length = int.from_bytes(handle.read(2), "big")
                    if length < 7:
                        return None, None
                    handle.read(1)
                    height = int.from_bytes(handle.read(2), "big")
                    width = int.from_bytes(handle.read(2), "big")
                    return width, height
                if marker in {b"\xd8", b"\xd9"}:
                    continue
                length_bytes = handle.read(2)
                if len(length_bytes) != 2:
                    return None, None
                segment_length = int.from_bytes(length_bytes, "big")
                if segment_length < 2:
                    return None, None
                handle.seek(segment_length - 2, os.SEEK_CUR)
    except Exception:
        return None, None


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    width, height = _png_dimensions(path)
    if width and height:
        return width, height
    return _jpeg_dimensions(path)


def _analyze_screenshot_file(path: Path, *, caption: str = "") -> dict:
    width, height = _image_dimensions(path)
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    digest = ""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        digest = ""
    return {
        "file_name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "sha256_prefix": digest,
        "caption_tokens": _tokenize(caption),
    }


def _tokenize(text: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[a-z0-9_]+", str(text or "").lower()):
        if len(token) < 3 or token in DEMO_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _string_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _normalize_demo_step(step, index: int) -> dict:
    if isinstance(step, str):
        text = step.strip()
        return {
            "index": index,
            "action": "manual",
            "title": text,
            "target": "",
            "instructions": text,
            "expected": "",
            "risk": "low",
            "inputs": {},
            "executable": False,
            "screenshot_refs": [],
        }

    payload = dict(step or {})
    action = str(payload.get("action") or payload.get("kind") or "manual").strip().lower()
    instructions = str(payload.get("instructions") or payload.get("title") or payload.get("note") or "").strip()
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
    target = str(
        payload.get("target")
        or inputs.get("path")
        or inputs.get("cmd")
        or inputs.get("query")
        or inputs.get("key")
        or inputs.get("selector")
        or inputs.get("url")
        or inputs.get("url_contains")
        or ""
    ).strip()
    expected = str(payload.get("expected") or payload.get("result") or "").strip()
    risk = str(
        payload.get("risk")
        or ("medium" if action in {"shell", "write_file", "browser_fill", "browser_press"} else "low")
    ).strip().lower()
    screenshot_refs = _string_list(payload.get("screenshot_refs"))
    executable = bool(payload.get("executable")) or (
        action in EXECUTABLE_DEMO_ACTIONS and bool(inputs or target)
    )

    if action == "shell" and not inputs.get("cmd") and target:
        inputs = {**inputs, "cmd": target}
    elif action == "read_file" and not inputs.get("path") and target:
        inputs = {**inputs, "path": target}
    elif action == "write_file" and target and "path" not in inputs:
        inputs = {**inputs, "path": target}
    elif action == "remember" and target and "key" not in inputs:
        inputs = {**inputs, "key": target}
    elif action == "web_search" and target and "query" not in inputs:
        inputs = {**inputs, "query": target}
    elif action == "browser_goto" and target and "url" not in inputs:
        inputs = {**inputs, "url": target}
    elif action in {"browser_click", "browser_extract_text", "browser_assert_text"} and target and "selector" not in inputs:
        inputs = {**inputs, "selector": target}
    elif action == "browser_fill" and target and "selector" not in inputs:
        inputs = {**inputs, "selector": target}
    elif action == "browser_press" and target and "selector" not in inputs and "key" in inputs:
        inputs = {**inputs, "selector": target}
    elif action == "browser_wait_for" and target and "selector" not in inputs and "url_contains" not in inputs:
        inputs = {**inputs, "selector": target}

    title = str(payload.get("title") or instructions or f"{action} step").strip()
    return {
        "index": int(payload.get("index") or index),
        "action": action or "manual",
        "title": title,
        "target": target,
        "instructions": instructions or title,
        "expected": expected,
        "risk": risk or "low",
        "inputs": inputs,
        "executable": executable,
        "screenshot_refs": screenshot_refs,
    }


def _normalize_steps(steps: list | None) -> list[dict]:
    normalized = []
    for index, step in enumerate(steps or [], start=1):
        normalized.append(_normalize_demo_step(step, index))
    return normalized


def _normalize_screenshots(screenshots) -> list[dict]:
    normalized = []
    for item in screenshots or []:
        if isinstance(item, dict):
            shot = {
                "path": str(item.get("path") or "").strip(),
                "caption": str(item.get("caption") or "").strip(),
            }
            if isinstance(item.get("analysis"), dict):
                shot["analysis"] = dict(item["analysis"])
            normalized.append(shot)
        else:
            raw = str(item or "").strip()
            if not raw:
                continue
            path, _, caption = raw.partition("::")
            shot = {"path": path.strip(), "caption": caption.strip()}
            candidate = Path(path.strip())
            if candidate.exists():
                shot["analysis"] = _analyze_screenshot_file(candidate, caption=caption.strip())
            normalized.append(shot)
    return [shot for shot in normalized if shot["path"]]


def _row_to_demo(row) -> dict:
    item = dict(row)
    raw_steps = json.loads(item.get("steps") or "[]")
    raw_shots = json.loads(item.get("screenshots") or "[]")
    item["steps"] = _normalize_steps(raw_steps)
    item["screenshots"] = _normalize_screenshots(raw_shots)
    item["tags"] = json.loads(item.get("tags") or "[]")
    item["permissions"] = json.loads(item.get("permissions") or "[]")
    item["schema_version"] = int(item.get("schema_version") or 1)
    item["uses"] = int(item.get("uses") or 0)
    item["success_count"] = int(item.get("success_count") or 0)
    item["failure_count"] = int(item.get("failure_count") or 0)
    item["last_confidence"] = float(item.get("last_confidence") or 0.0)
    item["last_replay_status"] = str(item.get("last_replay_status") or "").strip()
    item["last_replay_note"] = str(item.get("last_replay_note") or "").strip()
    raw_drift = item.get("last_drift") or ""
    try:
        item["last_drift"] = json.loads(raw_drift) if raw_drift else None
    except Exception:
        item["last_drift"] = {"raw": str(raw_drift)}
    item["reliability"] = demonstration_reliability(item)
    return item


def _score_demonstration(topic: str, demo: dict) -> tuple[float, float, list[str]]:
    query_tokens = set(_tokenize(topic))
    if not query_tokens:
        return 0.0, 0.0, []

    goal_tokens = set(_tokenize(demo.get("goal", "")))
    summary_tokens = set(_tokenize(demo.get("summary", "")))
    step_tokens = set(
        token
        for step in demo.get("steps", [])
        for token in _tokenize(" ".join([step.get("title", ""), step.get("instructions", ""), step.get("expected", ""), step.get("target", "")]))
    )
    tag_tokens = set(_tokenize(" ".join(demo.get("tags") or [])))
    app_env_tokens = set(_tokenize(" ".join([demo.get("app", ""), demo.get("environment", "")])))
    shot_tokens = set(
        token
        for shot in demo.get("screenshots", [])
        for token in _tokenize(
            " ".join(
                [
                    shot.get("caption", ""),
                    str((shot.get("analysis") or {}).get("file_name", "")),
                ]
            )
        )
    )

    score = 0.0
    reasons: list[str] = []
    overlaps = [
        ("goal", goal_tokens, 4.0),
        ("summary", summary_tokens, 3.0),
        ("steps", step_tokens, 2.0),
        ("tags", tag_tokens, 2.0),
        ("context", app_env_tokens, 1.5),
        ("screenshots", shot_tokens, 1.0),
    ]
    for label, tokens, weight in overlaps:
        overlap = sorted(query_tokens & tokens)
        if overlap:
            score += len(overlap) * weight
            reasons.append(f"{label}:" + ",".join(overlap[:4]))

    haystack = " ".join(
        [
            str(demo.get("goal", "")),
            str(demo.get("summary", "")),
            " ".join(step.get("instructions", "") for step in demo.get("steps", [])),
        ]
    ).lower()
    topic_text = str(topic or "").strip().lower()
    if topic_text and topic_text in haystack:
        score += 5.0
        reasons.append("exact_phrase")

    executable_steps = sum(1 for step in demo.get("steps", []) if step.get("executable"))
    total_steps = max(1, len(demo.get("steps", [])))
    readiness = executable_steps / total_steps
    score += readiness
    reliability = demonstration_reliability(demo)
    score += reliability * 2.5
    if reliability >= 0.7:
        reasons.append(f"reliable:{reliability:.2f}")
    failures = int(demo.get("failure_count", 0))
    if failures:
        penalty = min(3.0, failures * 0.4)
        score -= penalty
        reasons.append(f"failure_penalty:{penalty:.1f}")
    if str(demo.get("last_replay_status") or "").lower() == "success":
        score += 0.75
        reasons.append("last_replay:success")
    elif str(demo.get("last_replay_status") or "").lower() == "drift":
        score -= 0.5
        reasons.append("last_replay:drift")
    confidence = min(0.98, round(0.15 + (score / max(8.0, len(query_tokens) * 3.0)), 2))
    if score <= 0:
        return 0.0, 0.0, []
    return score, confidence, reasons


def demonstration_reliability(demo: dict) -> float:
    successes = int(demo.get("success_count", 0))
    failures = int(demo.get("failure_count", 0))
    return round((successes + 1) / (successes + failures + 2), 2)


def _format_demo_step(step: dict) -> str:
    prefix = f"({step.get('action', 'manual')})"
    target = f" target={step.get('target')}" if step.get("target") else ""
    expected = f" expected={step.get('expected')}" if step.get("expected") else ""
    runnable = " executable" if step.get("executable") else " manual"
    return f"{prefix}{target}{expected}{runnable} — {step.get('instructions') or step.get('title')}"


def save_demonstration(
    goal: str,
    summary: str = "",
    steps: list[str] | None = None,
    screenshots: list[str] | None = None,
    source: str = "human",
    app: str = "",
    environment: str = "",
    tags: list[str] | None = None,
    permissions: list[str] | None = None,
    correction_of: int | None = None,
) -> dict:
    cleaned_goal = str(goal or "").strip()
    normalized_steps = _normalize_steps(list(steps or []))
    cleaned_summary = str(summary or "").strip() or (
        normalized_steps[0]["instructions"] if normalized_steps else "Human demonstration"
    )
    if not cleaned_goal:
        raise ValueError("Demonstration goal is required.")
    if not cleaned_summary and not normalized_steps and not screenshots:
        raise ValueError("Demonstrations need a summary, a step, or at least one screenshot.")

    copied = _copy_demonstration_assets(list(screenshots or []))
    payload_steps = json.dumps(normalized_steps)
    payload_screenshots = json.dumps(copied)
    payload_tags = json.dumps(_string_list(tags))
    payload_permissions = json.dumps(_string_list(permissions))
    now = time.time()
    with _conn() as connection:
        cursor = connection.execute(
            """
            INSERT INTO demonstrations (
                scope, ts, goal, summary, steps, screenshots, source,
                app, environment, tags, permissions, correction_of, schema_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _scope(),
                now,
                cleaned_goal,
                cleaned_summary,
                payload_steps,
                payload_screenshots,
                source,
                str(app or "").strip(),
                str(environment or "").strip(),
                payload_tags,
                payload_permissions,
                correction_of,
                2,
            ),
        )
        demo_id = int(cursor.lastrowid)
    _prune_scope()
    return get_demonstration(demo_id) or {
        "id": demo_id,
        "goal": cleaned_goal,
        "summary": cleaned_summary,
        "steps": normalized_steps,
        "screenshots": copied,
        "source": source,
        "app": str(app or "").strip(),
        "environment": str(environment or "").strip(),
        "tags": _string_list(tags),
        "permissions": _string_list(permissions),
        "correction_of": correction_of,
        "ts": now,
    }


def recent_demonstrations(limit=5) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM demonstrations
            WHERE scope=?
            ORDER BY ts DESC LIMIT ?
            """,
            (_scope(), limit),
        ).fetchall()
    return [_row_to_demo(row) for row in rows]


def recall_demonstrations(topic: str, limit=3) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM demonstrations
            WHERE scope=?
            ORDER BY ts DESC LIMIT 80
            """,
            (_scope(),),
        ).fetchall()

    scored = []
    for row in rows:
        item = _row_to_demo(row)
        score, confidence, reasons = _score_demonstration(topic, item)
        if score > 0:
            item["match_score"] = round(score, 2)
            item["confidence"] = confidence
            item["match_reasons"] = reasons
            scored.append((score, item["ts"], item))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    results = [item for _, _, item in scored[:limit]]
    if results:
        with _conn() as connection:
            for item in results:
                connection.execute(
                    "UPDATE demonstrations SET uses=uses+1, last_used=?, last_confidence=? WHERE scope=? AND id=?",
                    (time.time(), item.get("confidence", 0.0), _scope(), item["id"]),
                )
                item["uses"] = int(item.get("uses", 0)) + 1
    return results


def _procedure_match_from_demo(demo: dict) -> ProcedureMatch:
    steps = demo.get("steps") or []
    executable_steps = sum(1 for step in steps if step.get("executable"))
    total_steps = max(1, len(steps))
    return ProcedureMatch(
        demo_id=int(demo["id"]),
        goal=str(demo.get("goal", "")),
        summary=str(demo.get("summary", "")),
        confidence=float(demo.get("confidence", demo.get("last_confidence", 0.0)) or 0.0),
        reliability=float(demo.get("reliability", demonstration_reliability(demo)) or 0.0),
        executable_steps=executable_steps,
        total_steps=total_steps,
        ready_for_replay=bool(executable_steps),
        reasons=tuple(str(item) for item in demo.get("match_reasons", []) if str(item).strip()),
        app=str(demo.get("app", "") or ""),
        environment=str(demo.get("environment", "") or ""),
        tags=tuple(str(item) for item in demo.get("tags", []) if str(item).strip()),
        last_replay_status=str(demo.get("last_replay_status", "") or ""),
    )


def procedure_matches(topic: str, limit=3) -> list[ProcedureMatch]:
    demos = recall_demonstrations(topic, limit=limit)
    return [_procedure_match_from_demo(demo) for demo in demos[:limit]]


def procedure_context(
    topic: str,
    limit=2,
    matches: list[ProcedureMatch] | None = None,
) -> str:
    items = matches if matches is not None else procedure_matches(topic, limit=limit)
    if not items:
        return ""

    lines = ["MATCHED PROCEDURES:"]
    for match in items[:limit]:
        lines.append("  " + match.render_for_executor().replace("\n", "\n  "))
    return "\n".join(lines)


def demonstration_context(topic: str, limit=2, demonstrations: list[dict] | None = None) -> str:
    demos = demonstrations if demonstrations is not None else recall_demonstrations(topic, limit=limit)
    if not demos:
        return ""

    lines = ["HUMAN DEMONSTRATIONS:"]
    for demo in demos[:limit]:
        lines.append(
            f"  Demo #{demo['id']} confidence={demo.get('confidence', demo.get('last_confidence', 0.0)):.2f} "
            f"uses={demo.get('uses', 0)} reliability={demo.get('reliability', demonstration_reliability(demo)):.2f}"
        )
        lines.append(f"  Goal: {demo['goal']}")
        lines.append(f"  Summary: {demo['summary']}")
        if demo.get("last_replay_status"):
            lines.append(f"  Last replay: {demo['last_replay_status']}")
        if demo.get("last_replay_note"):
            lines.append(f"  Replay note: {demo['last_replay_note']}")
        if demo.get("app"):
            lines.append(f"  App: {demo['app']}")
        if demo.get("environment"):
            lines.append(f"  Environment: {demo['environment']}")
        if demo.get("tags"):
            lines.append(f"  Tags: {', '.join(demo['tags'])}")
        if demo.get("permissions"):
            lines.append(f"  Permissions: {', '.join(demo['permissions'])}")
        if demo.get("match_reasons"):
            lines.append(f"  Match reasons: {', '.join(demo['match_reasons'])}")
        steps = demo.get("steps") or []
        if steps:
            executable_steps = sum(1 for step in steps if step.get("executable"))
            lines.append(f"  Steps: executable {executable_steps}/{len(steps)}")
            for index, step in enumerate(steps[:6], start=1):
                lines.append(f"    {index}. {_format_demo_step(step)}")
        screenshots = demo.get("screenshots") or []
        if screenshots:
            lines.append("  Screenshot references:")
            for shot in screenshots[:4]:
                suffix = f" :: {shot.get('caption')}" if shot.get("caption") else ""
                analysis = shot.get("analysis") or {}
                dims = ""
                if analysis.get("width") and analysis.get("height"):
                    dims = f" [{analysis['width']}x{analysis['height']}]"
                lines.append(f"    - {shot.get('path')}{suffix}{dims}")
        if demo.get("last_drift"):
            drift = demo["last_drift"]
            lines.append(
                "  Last drift: "
                + ", ".join(
                    part for part in [
                        drift.get("action"),
                        drift.get("target"),
                        drift.get("current_url"),
                    ] if part
                )
            )
    return "\n".join(lines)


def get_demonstration(demo_id: int) -> Optional[dict]:
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM demonstrations WHERE scope=? AND id=?",
            (_scope(), int(demo_id)),
        ).fetchone()
    return _row_to_demo(row) if row else None


def correct_demonstration(
    demo_id: int,
    *,
    goal: str | None = None,
    summary: str | None = None,
    steps: list | None = None,
    screenshots: list | None = None,
    app: str | None = None,
    environment: str | None = None,
    tags: list[str] | None = None,
    permissions: list[str] | None = None,
    source: str = "human_correction",
) -> dict:
    base = get_demonstration(demo_id)
    if not base:
        raise ValueError(f"Demonstration {demo_id} not found.")
    merged_steps = steps if steps is not None and len(steps) else base.get("steps", [])
    merged_screenshots = screenshots if screenshots is not None and len(screenshots) else list(base.get("screenshots", []))
    return save_demonstration(
        goal=str(goal or base["goal"]).strip(),
        summary=str(summary or base["summary"]).strip(),
        steps=merged_steps,
        screenshots=merged_screenshots,
        source=source,
        app=str(app if app is not None else base.get("app", "")).strip(),
        environment=str(environment if environment is not None else base.get("environment", "")).strip(),
        tags=tags if tags is not None and len(tags) else list(base.get("tags", [])),
        permissions=permissions if permissions is not None and len(permissions) else list(base.get("permissions", [])),
        correction_of=int(base["id"]),
    )


def format_demonstration(demo: dict) -> str:
    lines = [f"Demonstration #{demo['id']}: {demo['goal']}", f"Summary: {demo['summary']}"]
    if demo.get("app"):
        lines.append(f"App: {demo['app']}")
    if demo.get("environment"):
        lines.append(f"Environment: {demo['environment']}")
    if demo.get("tags"):
        lines.append(f"Tags: {', '.join(demo['tags'])}")
    if demo.get("permissions"):
        lines.append(f"Permissions: {', '.join(demo['permissions'])}")
    if demo.get("correction_of"):
        lines.append(f"Correction of: #{demo['correction_of']}")
    lines.append(
        f"Uses: {demo.get('uses', 0)} · successes: {demo.get('success_count', 0)} · "
        f"failures: {demo.get('failure_count', 0)} · reliability: {demo.get('reliability', demonstration_reliability(demo)):.2f} · "
        f"last_confidence: {demo.get('last_confidence', 0.0):.2f}"
    )
    if demo.get("last_replay_status"):
        lines.append(f"Last replay: {demo['last_replay_status']}")
    if demo.get("last_replay_note"):
        lines.append(f"Last replay note: {demo['last_replay_note']}")
    if demo.get("last_drift"):
        drift = demo["last_drift"]
        lines.append(
            "Last drift: "
            + ", ".join(
                part for part in [
                    drift.get("action"),
                    drift.get("target"),
                    drift.get("current_url"),
                    drift.get("screenshot"),
                ] if part
            )
        )
    if demo.get("steps"):
        lines.append("Steps:")
        for index, step in enumerate(demo["steps"], start=1):
            lines.append(f"  {index}. {_format_demo_step(step)}")
    if demo.get("screenshots"):
        lines.append("Screenshots:")
        for shot in demo["screenshots"]:
            suffix = f" :: {shot.get('caption')}" if shot.get("caption") else ""
            analysis = shot.get("analysis") or {}
            dims = ""
            if analysis.get("width") and analysis.get("height"):
                dims = f" [{analysis['width']}x{analysis['height']}]"
            lines.append(f"  - {shot.get('path')}{suffix}{dims}")
    return "\n".join(lines)


def record_demonstration_feedback(
    demo_id: int,
    success: bool,
    confidence: float | None = None,
    note: str = "",
    drift: dict | None = None,
):
    status = "success" if success else ("drift" if drift else "failure")
    with _conn() as connection:
        connection.execute(
            """
            UPDATE demonstrations
            SET last_used=?, last_confidence=COALESCE(?, last_confidence),
                success_count=success_count + ?,
                failure_count=failure_count + ?,
                last_replay_ts=?,
                last_replay_status=?,
                last_replay_note=?,
                last_drift=?
            WHERE scope=? AND id=?
            """,
            (
                time.time(),
                confidence,
                1 if success else 0,
                0 if success else 1,
                time.time(),
                status,
                str(note or "").strip(),
                json.dumps(drift) if drift else "",
                _scope(),
                int(demo_id),
            ),
        )


def _normalize_signal_person(item) -> dict | None:
    if isinstance(item, str):
        name = item.strip()
        return {"name": name} if name else None
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "relationship": str(item.get("relationship") or "").strip(),
            "notes": str(item.get("notes") or "").strip(),
            "aliases": _string_list(item.get("aliases")),
        }
    return None


def _normalize_signal_project(item) -> dict | None:
    if isinstance(item, str):
        name = item.strip()
        return {"name": name} if name else None
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        return {
            "name": name,
            "status": str(item.get("status") or "").strip(),
            "notes": str(item.get("notes") or "").strip(),
            "tags": _string_list(item.get("tags")),
        }
    return None


def _normalize_signal_commitment(item, metadata: dict) -> dict | None:
    if isinstance(item, str):
        title = item.strip()
        if not title:
            return None
        return {
            "title": title,
            "owner": str(metadata.get("owner") or "user").strip() or "user",
            "counterparty": str(metadata.get("counterparty") or metadata.get("person") or "").strip(),
            "project": str(metadata.get("project") or "").strip(),
            "due_at": str(metadata.get("due_at") or metadata.get("due") or "").strip(),
            "status": str(metadata.get("status") or "open").strip() or "open",
            "notes": str(metadata.get("notes") or "").strip(),
        }
    if isinstance(item, dict):
        title = str(item.get("title") or item.get("task") or "").strip()
        if not title:
            return None
        return {
            "title": title,
            "owner": str(item.get("owner") or metadata.get("owner") or "user").strip() or "user",
            "counterparty": str(item.get("counterparty") or metadata.get("counterparty") or metadata.get("person") or "").strip(),
            "project": str(item.get("project") or metadata.get("project") or "").strip(),
            "due_at": str(item.get("due_at") or item.get("due") or metadata.get("due_at") or metadata.get("due") or "").strip(),
            "status": str(item.get("status") or metadata.get("status") or "open").strip() or "open",
            "notes": str(item.get("notes") or "").strip(),
        }
    return None


def _infer_commitment_from_signal(title: str, content: str, metadata: dict) -> dict | None:
    text = " ".join(part for part in [str(title or "").strip(), str(content or "").strip()] if part).strip()
    if not text:
        return None
    match = re.search(
        r"\b(?:i|we)\s+(?:will|need to|should|must|promised to|owe)\s+([^.;\n]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    clause = match.group(1).strip(" .")
    if len(clause) < 4:
        return None
    return {
        "title": clause[:160],
        "owner": str(metadata.get("owner") or "user").strip() or "user",
        "counterparty": str(metadata.get("counterparty") or metadata.get("person") or "").strip(),
        "project": str(metadata.get("project") or "").strip(),
        "due_at": str(metadata.get("due_at") or metadata.get("due") or "").strip(),
        "status": str(metadata.get("status") or "open").strip() or "open",
        "notes": "Extracted from ingested signal",
    }


def _extract_signal_entities(kind: str, source: str, title: str, content: str, metadata: dict) -> dict:
    extracted = {
        "people": [],
        "projects": [],
        "commitments": [],
    }

    for key in ("people", "contacts"):
        value = metadata.get(key)
        if isinstance(value, list):
            items = value
        elif isinstance(value, dict):
            items = [value]
        else:
            items = _string_list(value)
        for raw in items:
            person = _normalize_signal_person(raw)
            if person and person["name"] not in {item["name"] for item in extracted["people"]}:
                extracted["people"].append(person)

    if metadata.get("person"):
        person = _normalize_signal_person({"name": metadata.get("person"), "relationship": metadata.get("relationship"), "notes": metadata.get("notes", "")})
        if person and person["name"] not in {item["name"] for item in extracted["people"]}:
            extracted["people"].append(person)

    for key in ("projects", "project"):
        value = metadata.get(key)
        if isinstance(value, list):
            items = value
        elif isinstance(value, dict):
            items = [value]
        else:
            items = _string_list(value)
        for raw in items:
            project = _normalize_signal_project(raw)
            if project and project["name"] not in {item["name"] for item in extracted["projects"]}:
                extracted["projects"].append(project)

    commitments_value = metadata.get("commitments", [])
    commitment_items = commitments_value if isinstance(commitments_value, list) else [commitments_value] if commitments_value else []
    for raw in commitment_items:
        commitment = _normalize_signal_commitment(raw, metadata)
        if commitment:
            extracted["commitments"].append(commitment)

    inferred = _infer_commitment_from_signal(title, content, metadata)
    if inferred and inferred["title"] not in {item["title"] for item in extracted["commitments"]}:
        extracted["commitments"].append(inferred)

    signal_source = f"signal:{kind}:{source}"
    for person in extracted["people"]:
        save_person(
            person["name"],
            relationship=person.get("relationship", ""),
            notes=person.get("notes", ""),
            aliases=person.get("aliases", []),
            source=signal_source,
        )
    for project in extracted["projects"]:
        save_project(
            project["name"],
            status=project.get("status", ""),
            notes=project.get("notes", ""),
            tags=project.get("tags", []),
            source=signal_source,
        )
    for commitment in extracted["commitments"]:
        save_commitment(
            commitment["title"],
            owner=commitment.get("owner", "user"),
            counterparty=commitment.get("counterparty", ""),
            project=commitment.get("project", ""),
            due_at=commitment.get("due_at", ""),
            status=commitment.get("status", "open"),
            notes=commitment.get("notes", ""),
            source=signal_source,
        )
    return extracted


def ingest_signal(
    kind: str,
    content: str,
    *,
    source: str = "manual",
    title: str = "",
    metadata: dict | None = None,
    happened_at: str = "",
) -> dict:
    init()
    cleaned_kind = str(kind or "").strip().lower()
    cleaned_content = str(content or "").strip()
    cleaned_source = str(source or "manual").strip() or "manual"
    cleaned_title = str(title or "").strip()
    cleaned_happened_at = str(happened_at or "").strip()
    metadata_dict = _json_dict(metadata)
    if not cleaned_kind:
        raise ValueError("Signal kind is required.")
    if not cleaned_content:
        raise ValueError("Signal content is required.")

    extracted = _extract_signal_entities(cleaned_kind, cleaned_source, cleaned_title, cleaned_content, metadata_dict)
    now = time.time()
    with _conn() as connection:
        cursor = connection.execute(
            """
            INSERT INTO ingested_signals (
                scope, kind, source, title, content, metadata, extracted, ts, happened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _scope(),
                cleaned_kind,
                cleaned_source,
                cleaned_title,
                cleaned_content,
                json.dumps(metadata_dict),
                json.dumps(extracted),
                now,
                cleaned_happened_at,
            ),
        )
        signal_id = int(cursor.lastrowid)
    _prune_scope()
    return {
        "id": signal_id,
        "kind": cleaned_kind,
        "source": cleaned_source,
        "title": cleaned_title,
        "content": cleaned_content,
        "metadata": metadata_dict,
        "extracted": extracted,
        "happened_at": cleaned_happened_at,
        "ts": now,
    }


def list_signals(limit=20, kind: str = "", source: str = "") -> list[dict]:
    query = "SELECT * FROM ingested_signals WHERE scope=?"
    params: list[object] = [_scope()]
    if str(kind or "").strip():
        query += " AND kind=?"
        params.append(str(kind).strip().lower())
    if str(source or "").strip():
        query += " AND source=?"
        params.append(str(source).strip())
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        with _conn() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
    except sqlite3.OperationalError:
        return []
    results = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_dict(item.get("metadata") or "{}")
        item["extracted"] = _json_dict(item.get("extracted") or "{}")
        results.append(item)
    return results


def _relevant_signals(topic: str, limit=4) -> list[dict]:
    scored = []
    query_tokens = _tokenize(topic)
    for item in list_signals(limit=120):
        haystack_parts = [
            item.get("title", ""),
            item.get("content", ""),
            json.dumps(item.get("metadata", {})),
            json.dumps(item.get("extracted", {})),
            item.get("kind", ""),
            item.get("source", ""),
        ]
        score, reasons = _score_text_tokens(topic, *haystack_parts)
        if score > 0 or (not query_tokens and str(item.get("kind", "")).lower() in {"message", "email", "meeting"}):
            item["match_score"] = score
            item["match_reasons"] = reasons
            scored.append((score, item.get("ts", 0), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def save_person(
    name: str,
    *,
    relationship: str = "",
    notes: str = "",
    aliases: list[str] | None = None,
    source: str = "human",
) -> dict:
    init()
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("Person name is required.")
    now = time.time()
    alias_list = _string_list(aliases)
    with _conn() as connection:
        connection.execute(
            """
            INSERT INTO people (scope, name, relationship, notes, aliases, ts, last_seen, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                relationship=excluded.relationship,
                notes=excluded.notes,
                aliases=excluded.aliases,
                last_seen=excluded.last_seen,
                source=excluded.source
            """,
            (_scope(), cleaned_name, str(relationship or "").strip(), str(notes or "").strip(), json.dumps(alias_list), now, now, source),
        )
    return {
        "name": cleaned_name,
        "relationship": str(relationship or "").strip(),
        "notes": str(notes or "").strip(),
        "aliases": alias_list,
        "last_seen": now,
        "source": source,
    }


def save_project(
    name: str,
    *,
    status: str = "",
    notes: str = "",
    tags: list[str] | None = None,
    source: str = "human",
) -> dict:
    init()
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        raise ValueError("Project name is required.")
    now = time.time()
    tag_list = _string_list(tags)
    with _conn() as connection:
        connection.execute(
            """
            INSERT INTO projects (scope, name, status, notes, tags, ts, last_active, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, name) DO UPDATE SET
                status=excluded.status,
                notes=excluded.notes,
                tags=excluded.tags,
                last_active=excluded.last_active,
                source=excluded.source
            """,
            (_scope(), cleaned_name, str(status or "").strip(), str(notes or "").strip(), json.dumps(tag_list), now, now, source),
        )
    return {
        "name": cleaned_name,
        "status": str(status or "").strip(),
        "notes": str(notes or "").strip(),
        "tags": tag_list,
        "last_active": now,
        "source": source,
    }


def save_commitment(
    title: str,
    *,
    owner: str = "user",
    counterparty: str = "",
    project: str = "",
    due_at: str = "",
    status: str = "open",
    notes: str = "",
    source: str = "human",
) -> dict:
    init()
    cleaned_title = str(title or "").strip()
    if not cleaned_title:
        raise ValueError("Commitment title is required.")
    now = time.time()
    with _conn() as connection:
        cursor = connection.execute(
            """
            INSERT INTO commitments (
                scope, title, owner, counterparty, project, due_at, status, notes, ts, updated_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _scope(),
                cleaned_title,
                str(owner or "user").strip() or "user",
                str(counterparty or "").strip(),
                str(project or "").strip(),
                str(due_at or "").strip(),
                str(status or "open").strip() or "open",
                str(notes or "").strip(),
                now,
                now,
                source,
            ),
        )
        commitment_id = int(cursor.lastrowid)
    return {
        "id": commitment_id,
        "title": cleaned_title,
        "owner": str(owner or "user").strip() or "user",
        "counterparty": str(counterparty or "").strip(),
        "project": str(project or "").strip(),
        "due_at": str(due_at or "").strip(),
        "status": str(status or "open").strip() or "open",
        "notes": str(notes or "").strip(),
        "updated_at": now,
        "source": source,
    }


def list_people(limit=20) -> list[dict]:
    try:
        with _conn() as connection:
            rows = connection.execute(
                "SELECT * FROM people WHERE scope=? ORDER BY last_seen DESC LIMIT ?",
                (_scope(), limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    results = []
    for row in rows:
        item = dict(row)
        item["aliases"] = json.loads(item.get("aliases") or "[]")
        results.append(item)
    return results


def list_projects(limit=20) -> list[dict]:
    try:
        with _conn() as connection:
            rows = connection.execute(
                "SELECT * FROM projects WHERE scope=? ORDER BY last_active DESC LIMIT ?",
                (_scope(), limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    results = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.get("tags") or "[]")
        results.append(item)
    return results


def list_commitments(limit=20, status: str = "") -> list[dict]:
    query = "SELECT * FROM commitments WHERE scope=?"
    params: list[object] = [_scope()]
    if str(status or "").strip():
        query += " AND status=?"
        params.append(str(status).strip())
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    try:
        with _conn() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _score_text_tokens(topic: str, *parts: str) -> tuple[float, list[str]]:
    query_tokens = set(_tokenize(topic))
    if not query_tokens:
        return 0.0, []
    score = 0.0
    reasons: list[str] = []
    for part in parts:
        tokens = set(_tokenize(part))
        overlap = sorted(query_tokens & tokens)
        if overlap:
            score += len(overlap)
            reasons.extend(overlap[:4])
    return score, reasons


def _relevant_people(topic: str, limit=3) -> list[dict]:
    scored = []
    for item in list_people(limit=80):
        score, reasons = _score_text_tokens(
            topic,
            item.get("name", ""),
            item.get("relationship", ""),
            item.get("notes", ""),
            " ".join(item.get("aliases", [])),
        )
        if score > 0:
            item["match_score"] = score
            item["match_reasons"] = reasons
            scored.append((score, item.get("last_seen", 0), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def _relevant_projects(topic: str, limit=3) -> list[dict]:
    scored = []
    for item in list_projects(limit=80):
        score, reasons = _score_text_tokens(
            topic,
            item.get("name", ""),
            item.get("status", ""),
            item.get("notes", ""),
            " ".join(item.get("tags", [])),
        )
        if score > 0:
            item["match_score"] = score
            item["match_reasons"] = reasons
            scored.append((score, item.get("last_active", 0), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def _relevant_commitments(topic: str, limit=5) -> list[dict]:
    scored = []
    for item in list_commitments(limit=120):
        score, reasons = _score_text_tokens(
            topic,
            item.get("title", ""),
            item.get("counterparty", ""),
            item.get("project", ""),
            item.get("notes", ""),
            item.get("status", ""),
            item.get("due_at", ""),
        )
        if score > 0 or (not _tokenize(topic) and item.get("status", "open") == "open"):
            item["match_score"] = score
            item["match_reasons"] = reasons
            bonus = 1.0 if str(item.get("status", "")).lower() == "open" else 0.0
            scored.append((score + bonus, item.get("updated_at", 0), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [item for _, _, item in scored[:limit]]


def chief_of_staff_context(topic: str, limit: int = 3) -> str:
    people = _relevant_people(topic, limit=limit)
    projects = _relevant_projects(topic, limit=limit)
    commitments = _relevant_commitments(topic, limit=max(limit, 4))
    signals = _relevant_signals(topic, limit=limit)
    if not people and not projects and not commitments and not signals:
        return ""

    lines = ["CHIEF OF STAFF MEMORY:"]
    if people:
        lines.append("  People:")
        for person in people:
            suffix = f" ({person['relationship']})" if person.get("relationship") else ""
            notes = f" — {person['notes']}" if person.get("notes") else ""
            lines.append(f"    - {person['name']}{suffix}{notes}")
    if projects:
        lines.append("  Projects:")
        for project in projects:
            status = f" [{project['status']}]" if project.get("status") else ""
            notes = f" — {project['notes']}" if project.get("notes") else ""
            lines.append(f"    - {project['name']}{status}{notes}")
    if commitments:
        lines.append("  Commitments:")
        for item in commitments:
            extras = []
            if item.get("counterparty"):
                extras.append(f"to {item['counterparty']}")
            if item.get("project"):
                extras.append(f"project={item['project']}")
            if item.get("due_at"):
                extras.append(f"due={item['due_at']}")
            if item.get("status"):
                extras.append(f"status={item['status']}")
            detail = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"    - {item['title']}{detail}")
    if signals:
        lines.append("  Recent signals:")
        for item in signals:
            label = item.get("title") or item.get("content", "")[:80]
            meta = f"{item.get('kind')} via {item.get('source')}"
            lines.append(f"    - {label} [{meta}]")
    return "\n".join(lines)


def chief_of_staff_briefing(topic: str = "", limit: int = 5) -> dict:
    return {
        "people": _relevant_people(topic, limit=limit),
        "projects": _relevant_projects(topic, limit=limit),
        "commitments": _relevant_commitments(topic, limit=limit),
        "signals": _relevant_signals(topic, limit=limit),
    }


def learn(key: str, value: str, confidence=1.0, ttl_seconds: int | None = None, source="agent"):
    scope = _scope()
    now = time.time()
    expires_at = now + ttl_seconds if ttl_seconds else None
    with _conn() as connection:
        current = connection.execute(
            "SELECT value, version, conflicts FROM world_facts WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        next_version = int(current["version"]) + 1 if current else 1
        next_conflicts = int(current["conflicts"]) if current else 0
        if current and current["value"] != value:
            next_conflicts += 1

        connection.execute(
            """
            INSERT INTO world_facts (scope,key,value,ts,confidence,version,conflicts,expires_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(scope,key) DO UPDATE SET
                value=excluded.value,
                ts=excluded.ts,
                confidence=excluded.confidence,
                version=excluded.version,
                conflicts=excluded.conflicts,
                expires_at=excluded.expires_at
            """,
            (scope, key, value, now, confidence, next_version, next_conflicts, expires_at),
        )
        connection.execute(
            """
            INSERT INTO world_history (scope,key,value,ts,confidence,version,source)
            VALUES (?,?,?,?,?,?,?)
            """,
            (scope, key, value, now, confidence, next_version, source),
        )
    _prune_scope()


def know(key: str) -> Optional[str]:
    with _conn() as connection:
        row = connection.execute(
            """
            SELECT value FROM world_facts
            WHERE scope=? AND key=? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (_scope(), key, time.time()),
        ).fetchone()
    return row["value"] if row else None


def world_context(topic: str) -> str:
    """Return relevant world knowledge as a formatted string."""
    keywords = [word.lower() for word in topic.split() if len(word) > 3]
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT key, value FROM world_facts
            WHERE scope=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY ts DESC LIMIT 100
            """,
            (_scope(), time.time()),
        ).fetchall()

    hits = [
        (row["key"], row["value"])
        for row in rows
        if any(keyword in row["key"].lower() or keyword in row["value"].lower() for keyword in keywords)
    ]
    if not hits:
        return ""
    return "WORLD KNOWLEDGE:\n" + "\n".join(f"  {key}: {value}" for key, value in hits[:8])


def recent_world_facts(limit=8) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT key, value, version, conflicts
            FROM world_facts
            WHERE scope=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY ts DESC LIMIT ?
            """,
            (_scope(), time.time(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def save_skill(name: str, description: str, code: str):
    scope = _scope()
    now = time.time()
    with _conn() as connection:
        current = connection.execute(
            "SELECT current_version FROM skills_current WHERE scope=? AND name=?",
            (scope, name),
        ).fetchone()
        version = int(current["current_version"]) + 1 if current else 1

        connection.execute(
            """
            INSERT INTO skills_current (scope,name,description,code,ts,current_version)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(scope,name) DO UPDATE SET
                description=excluded.description,
                code=excluded.code,
                ts=excluded.ts,
                current_version=excluded.current_version
            """,
            (scope, name, description, code, now, version),
        )
        connection.execute(
            """
            INSERT INTO skill_versions (scope,name,version,description,code,ts)
            VALUES (?,?,?,?,?,?)
            """,
            (scope, name, version, description, code, now),
        )

    (skill_root() / f"{name}.py").write_text(code, encoding="utf-8")
    _prune_scope()


def list_skills() -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT name,description,uses,failures,current_version
            FROM skills_current
            WHERE scope=?
            ORDER BY uses DESC, name ASC
            """,
            (_scope(),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_skill_code(name: str) -> Optional[str]:
    with _conn() as connection:
        row = connection.execute(
            "SELECT code FROM skills_current WHERE scope=? AND name=?",
            (_scope(), name),
        ).fetchone()
    return row["code"] if row else None


def record_skill_use(name: str, failed=False):
    with _conn() as connection:
        connection.execute(
            """
            INSERT INTO skills_current (scope,name,description,code,ts,uses,failures,current_version)
            VALUES (?,?,?,?,?,1,?,1)
            ON CONFLICT(scope,name) DO UPDATE SET uses=uses+1, failures=failures+?
            """,
            (_scope(), name, "", "", time.time(), int(failed), int(failed)),
        )


def list_skill_versions(name: str) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """
            SELECT version, description, ts
            FROM skill_versions
            WHERE scope=? AND name=?
            ORDER BY version DESC
            """,
            (_scope(), name),
        ).fetchall()
    return [dict(row) for row in rows]


def rollback_skill(name: str, version: int) -> bool:
    with _conn() as connection:
        row = connection.execute(
            """
            SELECT description, code, version
            FROM skill_versions
            WHERE scope=? AND name=? AND version=?
            """,
            (_scope(), name, version),
        ).fetchone()
        if not row:
            return False
        connection.execute(
            """
            UPDATE skills_current
            SET description=?, code=?, ts=?, current_version=?
            WHERE scope=? AND name=?
            """,
            (row["description"], row["code"], time.time(), row["version"], _scope(), name),
        )

    (skill_root() / f"{name}.py").write_text(row["code"], encoding="utf-8")
    return True


def record_tool(tool: str, failed=False):
    with _conn() as connection:
        connection.execute(
            """
            INSERT INTO tool_stats (scope,tool,calls,failures) VALUES (?, ?, 1, ?)
            ON CONFLICT(scope,tool) DO UPDATE SET calls=calls+1, failures=failures+?
            """,
            (_scope(), tool, int(failed), int(failed)),
        )


def save_run(goal: str, summary: str, metrics: dict):
    with _conn() as connection:
        connection.execute(
            """
            INSERT INTO runs (
                scope, ts, goal, outcome, duration_ms, tasks_planned, tasks_completed,
                tool_calls, tool_errors, critic_blocks, planner_fallback,
                parallel, metrics, summary
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _scope(),
                time.time(),
                goal,
                metrics.get("outcome", "partial"),
                metrics.get("duration_ms", 0),
                metrics.get("tasks_planned", 0),
                metrics.get("tasks_completed", 0),
                metrics.get("tool_calls", 0),
                metrics.get("tool_errors", 0),
                metrics.get("critic_blocks", 0),
                int(bool(metrics.get("planner_fallback", False))),
                int(bool(metrics.get("parallel", False))),
                json.dumps(metrics),
                summary,
            ),
        )
    _prune_scope()


def recent_runs(limit=5) -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            "SELECT * FROM runs WHERE scope=? ORDER BY ts DESC LIMIT ?",
            (_scope(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def tool_health() -> dict:
    with _conn() as connection:
        rows = connection.execute("SELECT * FROM tool_stats WHERE scope=?", (_scope(),)).fetchall()
    return {
        row["tool"]: {
            "calls": row["calls"],
            "fail_rate": round(row["failures"] / max(row["calls"], 1), 2),
        }
        for row in rows
    }
