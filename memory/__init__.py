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

from core.settings import data_root as configured_data_root
from core.settings import scope_id, skill_root

DB = Path.home() / ".phantom" / "memory.db"
LATEST_SCHEMA_VERSION = 4
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


MIGRATIONS = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
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
