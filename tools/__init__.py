"""
PHANTOM Tools — built-in tools + dynamic skill loader.

Skills are Python functions that agents can create at runtime and reuse in
future sessions.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from core.contracts import ArtifactRef, ToolExecutionResult, ToolExecutionStatus, VerificationResult
from core.errors import CheckpointDeclined
from core.settings import prompt_user, runtime_settings
from tools.safety import (
    ToolSafetyError,
    current_policy,
    ensure_path_allowed,
    validate_shell_command,
    validate_skill_code,
)


BUILTIN_TOOLS = [
    {
        "name": "shell",
        "description": "Run any shell command. For file ops, Python scripts, git, installs, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from disk.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates dirs as needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web via DuckDuckGo instant answers.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "slack_channel",
        "description": "Structured Slack channel and DM operations through the Slack Web API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "send_message",
                        "edit_message",
                        "delete_message",
                        "read_messages",
                        "react",
                        "reactions",
                        "pin_message",
                        "unpin_message",
                        "list_pins",
                        "member_info",
                        "emoji_list",
                    ],
                },
                "to": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_id": {"type": "string"},
                "user_id": {"type": "string"},
                "content": {"type": "string"},
                "emoji": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["action"],
        },
    },
    {
        "name": "discord_channel",
        "description": "Structured Discord channel operations through the Discord Bot API.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "send",
                        "edit",
                        "delete",
                        "read",
                        "react",
                        "pin",
                        "unpin",
                    ],
                },
                "to": {"type": "string"},
                "channel_id": {"type": "string"},
                "message_id": {"type": "string"},
                "user_id": {"type": "string"},
                "message": {"type": "string"},
                "emoji": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "silent": {"type": "boolean", "default": False},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["action"],
        },
    },
    {
        "name": "github_cli",
        "description": "Structured GitHub operations through the gh CLI for pull requests, issues, runs, and API queries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "auth_status",
                        "pr_list",
                        "pr_view",
                        "pr_checks",
                        "issue_list",
                        "issue_view",
                        "run_list",
                        "run_view",
                        "api",
                    ],
                },
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "run_id": {"type": "string"},
                "endpoint": {"type": "string"},
                "jq": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "limit": {"type": "integer", "default": 10},
                "fields": {"type": "object"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["action"],
        },
    },
    {
        "name": "tmux_session",
        "description": "Structured tmux session control for listing sessions, capturing panes, sending keys, and managing long-running terminal work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_sessions",
                        "list_windows",
                        "capture_pane",
                        "send_keys",
                        "new_session",
                        "kill_session",
                    ],
                },
                "target": {"type": "string"},
                "session_name": {"type": "string"},
                "command": {"type": "string"},
                "text": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "include_enter": {"type": "boolean", "default": False},
                "lines": {"type": "integer", "default": 20},
                "timeout": {"type": "integer", "default": 20},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_workflow",
        "description": (
            "Run a bounded browser workflow through Playwright. "
            "Use for web navigation, form filling, waiting, extracting page text, and screenshots."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "browser": {"type": "string", "default": "chromium"},
                "headless": {"type": "boolean", "default": True},
                "capture_final_screenshot": {"type": "boolean", "default": True},
                "session_id": {"type": "string"},
                "resume_session": {"type": "boolean", "default": False},
                "resume_last_page": {"type": "boolean", "default": False},
                "persist_session": {"type": "boolean", "default": True},
                "verify_resumed_state": {"type": "boolean", "default": True},
                "auto_reanchor": {"type": "boolean", "default": True},
                "attach_endpoint": {"type": "string"},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "browser_session",
        "description": "Create, inspect, list, or delete persistent browser sessions for operator workflows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "attach", "list", "inspect", "delete"],
                },
                "session_id": {"type": "string"},
                "browser": {"type": "string", "default": "chromium"},
                "headless": {"type": "boolean", "default": True},
                "attach_endpoint": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "remember",
        "description": "Save a fact to the world model for future use. Use for important discoveries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "remember_person",
        "description": "Save a person/contact with relationship notes and aliases for chief-of-staff memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "relationship": {"type": "string"},
                "notes": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remember_project",
        "description": "Save an ongoing project with status and notes for longitudinal context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remember_commitment",
        "description": "Save a commitment, promise, or follow-up with owner, counterparty, project, and due date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string"},
                "counterparty": {"type": "string"},
                "project": {"type": "string"},
                "due_at": {"type": "string"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_people",
        "description": "List remembered people relevant to the current query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        },
    },
    {
        "name": "list_projects",
        "description": "List remembered projects relevant to the current query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        },
    },
    {
        "name": "list_commitments",
        "description": "List remembered commitments, filtered by query or status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    },
    {
        "name": "chief_of_staff_briefing",
        "description": "Show relevant people, projects, and commitments for the current topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
        },
    },
    {
        "name": "ingest_signal",
        "description": (
            "Store a raw work signal such as a message, meeting note, email summary, or document note, "
            "and extract people, projects, and commitments into chief-of-staff memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "content": {"type": "string"},
                "source": {"type": "string"},
                "title": {"type": "string"},
                "happened_at": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["kind", "content"],
        },
    },
    {
        "name": "list_signals",
        "description": "List ingested raw signals, optionally filtered by kind or source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string"},
                "source": {"type": "string"},
            },
        },
    },
    {
        "name": "list_demonstrations",
        "description": "List recent human demonstrations or match them to a query.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "explain_demonstration",
        "description": "Show the full details of a saved human demonstration by id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "replay_demonstration",
        "description": (
            "Replay a human demonstration. Dry-run by default; only executable step types are run. "
            "High-risk steps stay blocked unless explicitly allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "execute": {"type": "boolean", "default": False},
                "allow_risky": {"type": "boolean", "default": False},
            },
            "required": ["id"],
        },
    },
    {
        "name": "create_skill",
        "description": (
            "Write and save a new reusable Python skill to disk. "
            "Use when you need a capability that doesn't exist yet. "
            "The skill becomes available to all future agent runs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case skill name"},
                "description": {"type": "string", "description": "What the skill does"},
                "code": {
                    "type": "string",
                    "description": "Complete Python function named `run(inputs: dict) -> str`",
                },
            },
            "required": ["name", "description", "code"],
        },
    },
    {
        "name": "use_skill",
        "description": "Execute a previously created skill by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "inputs": {"type": "object", "description": "Inputs to pass to the skill"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_skills",
        "description": "List all available skills the agent has created.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "skill_history",
        "description": "List previous saved versions of a skill.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "rollback_skill",
        "description": "Roll back a skill to a previous version.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "integer"},
            },
            "required": ["name", "version"],
        },
    },
]


def _get_tools_with_skills() -> list:
    """Return built-in tools plus schemas for any saved skills."""
    from memory import list_skills

    tools = list(BUILTIN_TOOLS)
    for skill in list_skills():
        tools.append({
            "name": f"skill_{skill['name']}",
            "description": f"[SKILL] {skill['description']} (uses: {skill['uses']})",
            "input_schema": {
                "type": "object",
                "properties": {"inputs": {"type": "object"}},
            },
        })
    return tools


def _minimal_env() -> dict:
    policy = current_policy()
    env = {}
    for key in ("PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "TERM"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["PHANTOM_HOME"] = str(policy.data_root)
    env["PHANTOM_WORKSPACE"] = str(policy.workspace_root)
    if os.environ.get("PHANTOM_SCOPE"):
        env["PHANTOM_SCOPE"] = os.environ["PHANTOM_SCOPE"]
    for key in (
        "PHANTOM_ALLOW_OUTSIDE_WORKSPACE",
        "PHANTOM_ALLOW_UNSAFE_SKILLS",
        "PHANTOM_SKILL_TIMEOUT",
        "PHANTOM_SKILL_MAX_FILE_BYTES",
        "PHANTOM_SKILL_MAX_OPEN_FILES",
        "PHANTOM_SKILL_MAX_PROCESSES",
        "PHANTOM_SKILL_MAX_MEMORY_BYTES",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _require_confirmation(tool_name: str, detail: str):
    checkpoints = runtime_settings().checkpoints
    required = (
        (tool_name == "shell" and checkpoints.confirm_shell)
        or (tool_name == "write_file" and checkpoints.confirm_writes)
        or (tool_name in {"web_search", "browser_workflow", "slack_channel", "discord_channel"} and checkpoints.confirm_web)
        or (tool_name in {"create_skill", "rollback_skill"} and checkpoints.confirm_skill_changes)
    )
    if not required:
        return
    if not prompt_user(f"PHANTOM approval required for {tool_name}: {detail}"):
        raise CheckpointDeclined(f"Human checkpoint declined {tool_name}: {detail}")


def _artifact(kind: str, *, label: str = "", path: str = "", **metadata) -> ArtifactRef:
    return ArtifactRef(kind=kind, label=label, path=path, metadata=metadata)


def _structured_tool_result(
    name: str,
    *,
    status: ToolExecutionStatus,
    ok: bool,
    summary: str,
    output: str | None = None,
    data: dict | None = None,
    verification: VerificationResult | None = None,
    artifacts: list[ArtifactRef] | tuple[ArtifactRef, ...] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=name,
        status=status,
        ok=ok,
        summary=str(summary or "").strip() or ("ok" if ok else "error"),
        output=str(output if output is not None else summary or ""),
        data=data or {},
        verification=verification,
        artifacts=tuple(artifacts or ()),
    )


def _status_from_output(output: str, err: bool, *, default_error: ToolExecutionStatus = ToolExecutionStatus.RUNTIME_ERROR) -> ToolExecutionStatus:
    if not err:
        return ToolExecutionStatus.SUCCESS
    text = str(output or "").strip().lower()
    if "checkpoint declined" in text or "human checkpoint declined" in text:
        return ToolExecutionStatus.CHECKPOINT_DECLINED
    if "timed out" in text:
        return ToolExecutionStatus.TIMEOUT
    if (
        "disabled by phantom_" in text
        or "blocked" in text
        or "not allowed" in text
        or "outside the workspace" in text
    ):
        return ToolExecutionStatus.SAFETY_BLOCKED
    if (
        "must be" in text
        or "requires" in text
        or "syntax error" in text
        or "valid python identifiers" in text
    ):
        return ToolExecutionStatus.VALIDATION_ERROR
    if "not found" in text or "no history found" in text or "unknown tool" in text:
        return ToolExecutionStatus.NOT_FOUND
    return default_error


def _shell(cmd, timeout=30):
    try:
        policy = current_policy()
        validate_shell_command(cmd, policy=policy)
        _require_confirmation("shell", cmd[:120])
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=policy.workspace_root,
            env=_minimal_env(),
        )
        output = (result.stdout + result.stderr).strip()
        return output or "(no output)", result.returncode != 0
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s", True
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _run_argv(argv: list[str], timeout=30):
    try:
        policy = current_policy()
        if not policy.allow_shell:
            raise ToolSafetyError("Shell-backed command execution is disabled by PHANTOM_ALLOW_SHELL=0.")
        if not argv or not argv[0]:
            raise ToolSafetyError("Command is empty.")
        binary = str(argv[0])
        if not shutil.which(binary):
            return f"{binary} not found on PATH.", True
        _require_confirmation("shell", " ".join(str(part) for part in argv[:8])[:120])
        result = subprocess.run(
            [str(part) for part in argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=policy.workspace_root,
            env=_minimal_env(),
        )
        output = (result.stdout + result.stderr).strip()
        return output or "(no output)", result.returncode != 0
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s", True
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _network_guard(tool_name: str, detail: str):
    policy = current_policy()
    if not policy.allow_web:
        raise ToolSafetyError(f"{tool_name} is disabled by PHANTOM_ALLOW_WEB=0.")
    _require_confirmation(tool_name, detail)


def _http_json_request(url: str, *, method="GET", headers=None, body=None, timeout=30):
    payload = None
    request_headers = {"User-Agent": "phantom/1.0"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        content_type = response.headers.get("Content-Type", "")
    if "json" in str(content_type).lower():
        return json.loads(raw or "{}")
    return raw


def _message_target_id(value: str, expected_prefix: str) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    prefix = f"{expected_prefix}:"
    if target.startswith(prefix):
        return target[len(prefix):].strip()
    return target


def _slack_token() -> str:
    return str(os.environ.get("PHANTOM_SLACK_BOT_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") or "").strip()


def _discord_token() -> str:
    return str(os.environ.get("PHANTOM_DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()


def _slack_channel(action, *, to="", channel_id="", message_id="", user_id="", content="", emoji="", limit=20, timeout=30):
    token = _slack_token()
    if not token:
        return "Slack bot token is missing. Set PHANTOM_SLACK_BOT_TOKEN or SLACK_BOT_TOKEN.", True
    action = str(action or "").strip()
    channel_id = str(channel_id or "").strip() or _message_target_id(to, "channel")
    user_id = str(user_id or "").strip() or _message_target_id(to, "user")
    if user_id and not channel_id:
        channel_id = user_id
    detail = f"slack_channel {action} {channel_id or user_id or to}".strip()
    try:
        _network_guard("slack_channel", detail)
        headers = {"Authorization": f"Bearer {token}"}
        base = "https://slack.com/api"
        if action == "send_message":
            payload = {"channel": channel_id, "text": str(content or "")}
            data = _http_json_request(f"{base}/chat.postMessage", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "edit_message":
            if not channel_id or not message_id:
                return "slack_channel edit_message requires channel_id and message_id.", True
            payload = {"channel": channel_id, "ts": str(message_id), "text": str(content or "")}
            data = _http_json_request(f"{base}/chat.update", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "delete_message":
            if not channel_id or not message_id:
                return "slack_channel delete_message requires channel_id and message_id.", True
            payload = {"channel": channel_id, "ts": str(message_id)}
            data = _http_json_request(f"{base}/chat.delete", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "read_messages":
            if not channel_id:
                return "slack_channel read_messages requires channel_id or to.", True
            query = urllib.parse.urlencode({"channel": channel_id, "limit": int(limit or 20)})
            data = _http_json_request(f"{base}/conversations.history?{query}", headers=headers, timeout=timeout)
        elif action == "react":
            if not channel_id or not message_id or not str(emoji or "").strip():
                return "slack_channel react requires channel_id, message_id, and emoji.", True
            payload = {"channel": channel_id, "timestamp": str(message_id), "name": str(emoji).strip(": ")}
            data = _http_json_request(f"{base}/reactions.add", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "reactions":
            if not channel_id or not message_id:
                return "slack_channel reactions requires channel_id and message_id.", True
            query = urllib.parse.urlencode({"channel": channel_id, "timestamp": str(message_id)})
            data = _http_json_request(f"{base}/reactions.get?{query}", headers=headers, timeout=timeout)
        elif action == "pin_message":
            if not channel_id or not message_id:
                return "slack_channel pin_message requires channel_id and message_id.", True
            payload = {"channel": channel_id, "timestamp": str(message_id)}
            data = _http_json_request(f"{base}/pins.add", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "unpin_message":
            if not channel_id or not message_id:
                return "slack_channel unpin_message requires channel_id and message_id.", True
            payload = {"channel": channel_id, "timestamp": str(message_id)}
            data = _http_json_request(f"{base}/pins.remove", method="POST", headers=headers, body=payload, timeout=timeout)
        elif action == "list_pins":
            if not channel_id:
                return "slack_channel list_pins requires channel_id.", True
            query = urllib.parse.urlencode({"channel": channel_id})
            data = _http_json_request(f"{base}/pins.list?{query}", headers=headers, timeout=timeout)
        elif action == "member_info":
            if not user_id:
                return "slack_channel member_info requires user_id.", True
            query = urllib.parse.urlencode({"user": user_id})
            data = _http_json_request(f"{base}/users.info?{query}", headers=headers, timeout=timeout)
        elif action == "emoji_list":
            data = _http_json_request(f"{base}/emoji.list", headers=headers, timeout=timeout)
        else:
            return f"Unknown slack_channel action: {action}", True

        if isinstance(data, dict) and data.get("ok") is False:
            return f"Slack API error: {data.get('error', 'unknown_error')}", True
        return json.dumps(data, ensure_ascii=True), False
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _discord_channel(action, *, to="", channel_id="", message_id="", user_id="", message="", emoji="", limit=20, silent=False, timeout=30):
    token = _discord_token()
    if not token:
        return "Discord bot token is missing. Set PHANTOM_DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.", True
    action = str(action or "").strip()
    channel_id = str(channel_id or "").strip() or _message_target_id(to, "channel")
    user_id = str(user_id or "").strip() or _message_target_id(to, "user")
    detail = f"discord_channel {action} {channel_id or user_id or to}".strip()
    try:
        _network_guard("discord_channel", detail)
        headers = {"Authorization": f"Bot {token}"}
        base = "https://discord.com/api/v10"
        if user_id and action == "send" and not channel_id:
            dm = _http_json_request(
                f"{base}/users/@me/channels",
                method="POST",
                headers=headers,
                body={"recipient_id": user_id},
                timeout=timeout,
            )
            channel_id = str(dm.get("id") or "")
        if action == "send":
            if not channel_id:
                return "discord_channel send requires channel_id or to.", True
            payload = {"content": str(message or "")}
            if silent:
                payload["flags"] = 1 << 12
            data = _http_json_request(
                f"{base}/channels/{channel_id}/messages",
                method="POST",
                headers=headers,
                body=payload,
                timeout=timeout,
            )
        elif action == "edit":
            if not channel_id or not message_id:
                return "discord_channel edit requires channel_id and message_id.", True
            data = _http_json_request(
                f"{base}/channels/{channel_id}/messages/{message_id}",
                method="PATCH",
                headers=headers,
                body={"content": str(message or "")},
                timeout=timeout,
            )
        elif action == "delete":
            if not channel_id or not message_id:
                return "discord_channel delete requires channel_id and message_id.", True
            data = _http_json_request(
                f"{base}/channels/{channel_id}/messages/{message_id}",
                method="DELETE",
                headers=headers,
                timeout=timeout,
            )
            if data == "":
                data = {"ok": True}
        elif action == "read":
            if not channel_id:
                return "discord_channel read requires channel_id or to.", True
            query = urllib.parse.urlencode({"limit": int(limit or 20)})
            data = _http_json_request(
                f"{base}/channels/{channel_id}/messages?{query}",
                headers=headers,
                timeout=timeout,
            )
        elif action == "react":
            if not channel_id or not message_id or not str(emoji or "").strip():
                return "discord_channel react requires channel_id, message_id, and emoji.", True
            encoded = urllib.parse.quote(str(emoji or "").strip(), safe="")
            data = _http_json_request(
                f"{base}/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
                method="PUT",
                headers=headers,
                timeout=timeout,
            )
            if data == "":
                data = {"ok": True}
        elif action == "pin":
            if not channel_id or not message_id:
                return "discord_channel pin requires channel_id and message_id.", True
            data = _http_json_request(
                f"{base}/channels/{channel_id}/pins/{message_id}",
                method="PUT",
                headers=headers,
                timeout=timeout,
            )
            if data == "":
                data = {"ok": True}
        elif action == "unpin":
            if not channel_id or not message_id:
                return "discord_channel unpin requires channel_id and message_id.", True
            data = _http_json_request(
                f"{base}/channels/{channel_id}/pins/{message_id}",
                method="DELETE",
                headers=headers,
                timeout=timeout,
            )
            if data == "":
                data = {"ok": True}
        else:
            return f"Unknown discord_channel action: {action}", True
        if isinstance(data, dict) and data.get("message") and data.get("code") and not data.get("id") and not data.get("ok"):
            return f"Discord API error: {data.get('message')}", True
        return json.dumps(data, ensure_ascii=True), False
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _github_cli(action, *, repo="", number=None, run_id="", endpoint="", jq="", method="GET", limit=10, fields=None, timeout=30):
    action = str(action or "").strip().lower()
    repo = str(repo or "").strip()
    jq = str(jq or "").strip()
    method = str(method or "GET").strip().upper() or "GET"
    argv = ["gh"]
    if action == "auth_status":
        argv += ["auth", "status"]
    elif action == "pr_list":
        argv += ["pr", "list", "--limit", str(int(limit or 10))]
    elif action == "pr_view":
        if number is None:
            return "github_cli pr_view requires number.", True
        argv += ["pr", "view", str(int(number))]
    elif action == "pr_checks":
        if number is None:
            return "github_cli pr_checks requires number.", True
        argv += ["pr", "checks", str(int(number))]
    elif action == "issue_list":
        argv += ["issue", "list", "--limit", str(int(limit or 10))]
    elif action == "issue_view":
        if number is None:
            return "github_cli issue_view requires number.", True
        argv += ["issue", "view", str(int(number))]
    elif action == "run_list":
        argv += ["run", "list", "--limit", str(int(limit or 10))]
    elif action == "run_view":
        if not str(run_id or "").strip():
            return "github_cli run_view requires run_id.", True
        argv += ["run", "view", str(run_id).strip()]
    elif action == "api":
        endpoint = str(endpoint or "").strip()
        if not endpoint:
            return "github_cli api requires endpoint.", True
        argv += ["api", endpoint]
        if method != "GET":
            argv += ["-X", method]
        if isinstance(fields, dict):
            for key in sorted(fields):
                argv += ["-f", f"{key}={fields[key]}"]
    else:
        return f"Unknown github_cli action: {action}", True

    if repo:
        argv += ["--repo", repo]
    if jq:
        argv += ["--jq", jq]
    return _run_argv(argv, timeout=timeout)


def _tmux_session(action, *, target="", session_name="", command="", text="", keys=None, include_enter=False, lines=20, timeout=20):
    action = str(action or "").strip().lower()
    target = str(target or "").strip()
    session_name = str(session_name or "").strip()
    argv = ["tmux"]
    if action == "list_sessions":
        argv += ["list-sessions"]
    elif action == "list_windows":
        if not target and not session_name:
            return "tmux_session list_windows requires target or session_name.", True
        argv += ["list-windows", "-t", target or session_name]
    elif action == "capture_pane":
        if not target:
            return "tmux_session capture_pane requires target.", True
        argv += ["capture-pane", "-t", target, "-p"]
    elif action == "send_keys":
        if not target:
            return "tmux_session send_keys requires target.", True
        argv += ["send-keys", "-t", target]
        if text:
            argv.append(str(text))
        for key in keys or []:
            argv.append(str(key))
        if include_enter:
            argv.append("Enter")
    elif action == "new_session":
        if not session_name:
            return "tmux_session new_session requires session_name.", True
        argv += ["new-session", "-d", "-s", session_name]
        if str(command or "").strip():
            argv.append(str(command).strip())
    elif action == "kill_session":
        if not target and not session_name:
            return "tmux_session kill_session requires target or session_name.", True
        argv += ["kill-session", "-t", target or session_name]
    else:
        return f"Unknown tmux_session action: {action}", True

    output, err = _run_argv(argv, timeout=timeout)
    if not err and action == "capture_pane":
        line_limit = max(1, int(lines or 20))
        trimmed = "\n".join(output.splitlines()[-line_limit:])
        return trimmed or output, False
    if not err and action in {"send_keys", "new_session", "kill_session"} and output == "(no output)":
        label = target or session_name or "tmux"
        return f"{action} ok for {label}", False
    return output, err


def _read_file(path):
    try:
        resolved = ensure_path_allowed(path, write=False)
        return Path(resolved).read_text(encoding="utf-8"), False
    except ToolSafetyError as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _write_file(path, content):
    try:
        target = ensure_path_allowed(path, write=True)
        _require_confirmation("write_file", str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars → {target}", False
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _web_search(query):
    try:
        policy = current_policy()
        if not policy.allow_web:
            raise ToolSafetyError("Web access is disabled by PHANTOM_ALLOW_WEB=0.")
        _require_confirmation("web_search", query[:120])

        headers = {"User-Agent": "Mozilla/5.0 (compatible; phantom/1.0)"}
        all_parts = []

        # --- Tier 1: DuckDuckGo instant answers (structured) ---
        try:
            instant_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1"
            req = urllib.request.Request(instant_url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read())
            if data.get("AbstractText"):
                all_parts.append(f"Summary: {data['AbstractText']}")
            for topic in data.get("RelatedTopics", [])[:4]:
                if isinstance(topic, dict) and topic.get("Text"):
                    all_parts.append(topic["Text"])
        except Exception:
            pass

        # --- Tier 2: DuckDuckGo HTML results page scrape ---
        if not all_parts:
            try:
                html_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
                req = urllib.request.Request(html_url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    html = response.read().decode("utf-8", errors="replace")
                # Extract result title + snippet text
                title_snippets = re.findall(
                    r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?'
                    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                    html, re.DOTALL
                )
                for title_html, snippet_html in title_snippets[:6]:
                    title = re.sub(r"<[^>]+>", "", title_html).strip()
                    snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()
                    if title or snippet:
                        all_parts.append(f"{title}: {snippet}" if title else snippet)
            except Exception:
                pass

        return "\n\n".join(all_parts) if all_parts else "No results found.", False
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except Exception as exc:
        return str(exc), True


def _browser_workflow(
    steps,
    browser="chromium",
    headless=True,
    capture_final_screenshot=True,
    session_id="",
    resume_session=False,
    resume_last_page=False,
    persist_session=True,
    verify_resumed_state=True,
    auto_reanchor=True,
    attach_endpoint="",
):
    _, summary, err = _execute_browser_workflow(
        steps,
        browser=browser,
        headless=headless,
        capture_final_screenshot=capture_final_screenshot,
        session_id=session_id,
        resume_session=resume_session,
        resume_last_page=resume_last_page,
        persist_session=persist_session,
        verify_resumed_state=verify_resumed_state,
        auto_reanchor=auto_reanchor,
        attach_endpoint=attach_endpoint,
    )
    return summary, err


def _execute_browser_workflow(
    steps,
    browser="chromium",
    headless=True,
    capture_final_screenshot=True,
    session_id="",
    resume_session=False,
    resume_last_page=False,
    persist_session=True,
    verify_resumed_state=True,
    auto_reanchor=True,
    attach_endpoint="",
):
    try:
        policy = current_policy()
        if not policy.allow_web:
            raise ToolSafetyError("Browser automation is disabled by PHANTOM_ALLOW_WEB=0.")
        if not isinstance(steps, list) or not steps:
            raise ToolSafetyError("browser_workflow requires a non-empty list of steps.")
        detail = ", ".join(
            str(step.get("action") or "step") if isinstance(step, dict) else "step"
            for step in steps[:4]
        )
        if len(steps) > 4:
            detail += ", ..."
        _require_confirmation("browser_workflow", detail or "browser workflow")
        from tools.browser_runtime import run_browser_workflow, summarize_browser_result

        result = run_browser_workflow(
            steps,
            browser_name=str(browser or "chromium"),
            headless=bool(headless),
            capture_final_screenshot=bool(capture_final_screenshot),
            session_id=str(session_id or "").strip(),
            resume_session=bool(resume_session),
            resume_last_page=bool(resume_last_page),
            persist_session=bool(persist_session),
            verify_resumed_state=bool(verify_resumed_state),
            auto_reanchor=bool(auto_reanchor),
            attach_endpoint=str(attach_endpoint or "").strip(),
        )
        return result, summarize_browser_result(result), not bool(result.get("ok", True))
    except (ModuleNotFoundError, ToolSafetyError, CheckpointDeclined) as exc:
        return {"ok": False, "error": str(exc), "step_results": [], "screenshots": []}, str(exc), True
    except Exception as exc:
        return {"ok": False, "error": str(exc), "step_results": [], "screenshots": []}, str(exc), True


def _browser_session(action, session_id="", browser="chromium", headless=True, attach_endpoint=""):
    from tools.browser_runtime import (
        attach_browser_session,
        delete_browser_session,
        ensure_browser_session,
        get_browser_session,
        list_browser_sessions,
    )

    action = str(action or "").strip().lower()
    if action == "create":
        if not str(session_id or "").strip():
            return "browser_session create requires session_id.", True
        payload = ensure_browser_session(
            str(session_id).strip(),
            browser_name=str(browser or "chromium"),
            headless=bool(headless),
            attach_endpoint=str(attach_endpoint or "").strip(),
        )
        return json.dumps(payload, ensure_ascii=True), False
    if action == "attach":
        if not str(session_id or "").strip() or not str(attach_endpoint or "").strip():
            return "browser_session attach requires session_id and attach_endpoint.", True
        payload = attach_browser_session(
            str(session_id).strip(),
            str(attach_endpoint).strip(),
            browser_name=str(browser or "chromium"),
            headless=bool(headless),
        )
        return json.dumps(payload, ensure_ascii=True), False
    if action == "list":
        return json.dumps({"sessions": list_browser_sessions()}, ensure_ascii=True), False
    if action == "inspect":
        if not str(session_id or "").strip():
            return "browser_session inspect requires session_id.", True
        payload = get_browser_session(str(session_id).strip())
        if not payload:
            return f"Browser session '{session_id}' not found.", True
        return json.dumps(payload, ensure_ascii=True), False
    if action == "delete":
        if not str(session_id or "").strip():
            return "browser_session delete requires session_id.", True
        ok = delete_browser_session(str(session_id).strip())
        if not ok:
            return f"Browser session '{session_id}' not found.", True
        return f"Deleted browser session '{session_id}'.", False
    return f"Unknown browser_session action: {action}", True


def _create_skill(name, description, code):
    from memory import save_skill

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return "Skill names must be valid Python identifiers.", True

    try:
        compile(code, f"skill_{name}.py", "exec")
        validate_skill_code(code)
        _require_confirmation("create_skill", name)
        save_skill(name, description, code)
        return f"Skill '{name}' created and saved. Available in all future sessions.", False
    except (ToolSafetyError, CheckpointDeclined) as exc:
        return str(exc), True
    except SyntaxError as exc:
        return f"Syntax error in skill code: {exc}", True


def _use_skill(name, inputs=None):
    from memory import get_skill_code, record_skill_use
    from tools.skill_runner import build_skill_commands

    code = get_skill_code(name)
    if not code:
        return f"Skill '{name}' not found. Use list_skills to see available skills.", True

    try:
        runner_path = Path(__file__).with_name("skill_runner.py")
        launch_errors = []
        payload = None
        result = None
        for command in build_skill_commands(runner_path):
            result = subprocess.run(
                command,
                input=json.dumps({"code": code, "inputs": inputs or {}}),
                capture_output=True,
                text=True,
                timeout=current_policy().skill_timeout,
                cwd=current_policy().workspace_root,
                env=_minimal_env(),
            )
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = None
            if payload is None and result.returncode != 0:
                launch_errors.append(result.stderr.strip() or result.stdout.strip() or "wrapper launch failed")
                continue
            break
        if payload is None:
            raise RuntimeError("; ".join(error for error in launch_errors if error) or "Skill execution failed.")
        if result is None or result.returncode != 0 or not payload.get("ok"):
            raise RuntimeError(payload.get("error", result.stderr.strip() if result else "Skill execution failed."))
        record_skill_use(name, failed=False)
        return str(payload.get("result", "")), False
    except Exception as exc:
        record_skill_use(name, failed=True)
        return f"Skill error: {exc}", True


def _skill_history(name):
    from memory import list_skill_versions

    versions = list_skill_versions(name)
    if not versions:
        return f"No history found for skill '{name}'.", True
    lines = [f"  v{item['version']}: {item['description']}" for item in versions]
    return "Skill history:\n" + "\n".join(lines), False


def _rollback_skill(name, version):
    from memory import rollback_skill

    try:
        _require_confirmation("rollback_skill", f"{name} -> v{version}")
        ok = rollback_skill(name, version)
        if not ok:
            return f"Skill '{name}' version {version} not found.", True
        return f"Rolled back skill '{name}' to version {version}.", False
    except CheckpointDeclined as exc:
        return str(exc), True


def _list_demonstrations(query=""):
    from memory import recent_demonstrations, recall_demonstrations

    demos = recall_demonstrations(query, limit=5) if str(query or "").strip() else recent_demonstrations(limit=5)
    if not demos:
        return "No demonstrations found.", False
    lines = []
    for demo in demos:
        executable = sum(1 for step in demo.get("steps", []) if step.get("executable"))
        confidence = demo.get("confidence")
        confidence_text = f" confidence={confidence:.2f}" if confidence is not None else ""
        reliability_text = f" reliability={demo.get('reliability', 0.0):.2f}"
        status_text = f" last={demo.get('last_replay_status')}" if demo.get("last_replay_status") else ""
        lines.append(
            f"  #{demo['id']}: {demo['goal']} "
            f"(steps={len(demo.get('steps', []))}, executable={executable}, uses={demo.get('uses', 0)}"
            f"{confidence_text}{reliability_text}{status_text})"
        )
    return "Demonstrations:\n" + "\n".join(lines), False


def _remember_person(name, relationship="", notes="", aliases=None):
    import memory as mem

    saved = mem.save_person(name, relationship=relationship, notes=notes, aliases=aliases or [])
    detail = f"Saved person: {saved['name']}"
    if saved.get("relationship"):
        detail += f" ({saved['relationship']})"
    return detail, False


def _remember_project(name, status="", notes="", tags=None):
    import memory as mem

    saved = mem.save_project(name, status=status, notes=notes, tags=tags or [])
    detail = f"Saved project: {saved['name']}"
    if saved.get("status"):
        detail += f" [{saved['status']}]"
    return detail, False


def _remember_commitment(title, owner="user", counterparty="", project="", due_at="", status="open", notes=""):
    import memory as mem

    saved = mem.save_commitment(
        title,
        owner=owner,
        counterparty=counterparty,
        project=project,
        due_at=due_at,
        status=status,
        notes=notes,
    )
    detail = f"Saved commitment #{saved['id']}: {saved['title']}"
    return detail, False


def _list_people(query=""):
    import memory as mem

    items = mem.chief_of_staff_briefing(str(query or ""), limit=8)["people"] if str(query or "").strip() else mem.list_people(limit=8)
    if not items:
        return "No people remembered yet.", False
    lines = []
    for item in items:
        aliases = f" aliases={','.join(item.get('aliases', []))}" if item.get("aliases") else ""
        relationship = f" ({item['relationship']})" if item.get("relationship") else ""
        notes = f" — {item['notes']}" if item.get("notes") else ""
        lines.append(f"  {item['name']}{relationship}{aliases}{notes}")
    return "People:\n" + "\n".join(lines), False


def _list_projects(query=""):
    import memory as mem

    items = mem.chief_of_staff_briefing(str(query or ""), limit=8)["projects"] if str(query or "").strip() else mem.list_projects(limit=8)
    if not items:
        return "No projects remembered yet.", False
    lines = []
    for item in items:
        status = f" [{item['status']}]" if item.get("status") else ""
        tags = f" tags={','.join(item.get('tags', []))}" if item.get("tags") else ""
        notes = f" — {item['notes']}" if item.get("notes") else ""
        lines.append(f"  {item['name']}{status}{tags}{notes}")
    return "Projects:\n" + "\n".join(lines), False


def _list_commitments(query="", status=""):
    import memory as mem

    if str(query or "").strip():
        items = mem.chief_of_staff_briefing(str(query), limit=10)["commitments"]
        if str(status or "").strip():
            items = [item for item in items if str(item.get("status", "")).strip().lower() == str(status).strip().lower()]
    else:
        items = mem.list_commitments(limit=10, status=str(status or "").strip())
    if not items:
        return "No commitments remembered yet.", False
    lines = []
    for item in items:
        extras = []
        if item.get("counterparty"):
            extras.append(f"to {item['counterparty']}")
        if item.get("project"):
            extras.append(f"project={item['project']}")
        if item.get("due_at"):
            extras.append(f"due={item['due_at']}")
        if item.get("status"):
            extras.append(f"status={item['status']}")
        lines.append(f"  #{item.get('id', '?')} {item['title']} ({', '.join(extras)})")
    return "Commitments:\n" + "\n".join(lines), False


def _chief_of_staff_briefing(query=""):
    import memory as mem

    briefing = mem.chief_of_staff_briefing(str(query or ""), limit=5)
    parts = []
    if briefing["people"]:
        parts.append(_list_people(query)[0])
    if briefing["projects"]:
        parts.append(_list_projects(query)[0])
    if briefing["commitments"]:
        parts.append(_list_commitments(query)[0])
    if briefing.get("signals"):
        parts.append(_list_signals(query=query)[0])
    if not parts:
        return "Chief-of-staff memory is empty for this topic.", False
    return "\n\n".join(parts), False


def _ingest_signal(kind, content, source="", title="", happened_at="", metadata=None):
    import memory as mem

    saved = mem.ingest_signal(
        str(kind or ""),
        str(content or ""),
        source=str(source or "manual"),
        title=str(title or ""),
        happened_at=str(happened_at or ""),
        metadata=metadata or {},
    )
    extracted = saved.get("extracted", {})
    return (
        "Saved signal "
        f"#{saved['id']} ({saved['kind']} via {saved['source']}) — "
        f"{len(extracted.get('people', []))} people, "
        f"{len(extracted.get('projects', []))} projects, "
        f"{len(extracted.get('commitments', []))} commitments extracted.",
        False,
    )


def _list_signals(query="", kind="", source=""):
    import memory as mem

    if str(query or "").strip():
        items = mem.chief_of_staff_briefing(str(query), limit=12).get("signals", [])
    else:
        items = mem.list_signals(limit=12, kind=str(kind or ""), source=str(source or ""))
    if not items:
        return "No ingested signals found.", False
    lines = []
    for item in items:
        title = item.get("title") or item.get("content", "")[:60]
        extracted = item.get("extracted", {})
        counts = (
            f"people={len(extracted.get('people', []))}, "
            f"projects={len(extracted.get('projects', []))}, "
            f"commitments={len(extracted.get('commitments', []))}"
        )
        lines.append(f"  #{item['id']} [{item['kind']}] {title} ({counts})")
    return "Signals:\n" + "\n".join(lines), False


def _explain_demonstration(demo_id):
    from memory import format_demonstration, get_demonstration

    demo = get_demonstration(int(demo_id))
    if not demo:
        return f"Demonstration #{demo_id} not found.", True
    return format_demonstration(demo), False


def _approve_risky_replay_step(index: int, step: dict):
    detail = step.get("instructions") or step.get("title") or step.get("target") or step.get("action") or f"step {index}"
    risk = str(step.get("risk") or "high").lower()
    if not prompt_user(f"Approve risky demonstration replay step {index} ({risk}): {detail}"):
        raise CheckpointDeclined(f"Human checkpoint declined risky replay step {index}: {detail}")


def _verify_replay_result(step: dict, tool_name: str, tool_inputs: dict, result: str, err: bool) -> tuple[bool, str]:
    import memory as mem

    if err:
        return False, "tool returned an error"

    expected = str(step.get("expected") or "").strip()
    try:
        if tool_name == "write_file":
            target = ensure_path_allowed(tool_inputs["path"], write=False)
            content = Path(target).read_text(encoding="utf-8")
            if expected:
                ok = expected in content
                return ok, f"{target} contains expected text={ok}"
            desired = str(tool_inputs.get("content") or "")
            ok = content == desired
            return ok, f"{target} written ({len(content)} chars)"

        if tool_name == "remember":
            key = str(tool_inputs.get("key") or "")
            actual = str(mem.know(key) or "")
            desired = str(tool_inputs.get("value") or expected or "")
            ok = actual == desired if desired else bool(actual)
            return ok, f"world[{key}]={actual[:120]}"

        haystack = str(result or "").strip()
        if expected:
            ok = expected.lower() in haystack.lower()
            return ok, f"expected text present={ok}"
        if tool_name == "read_file":
            return bool(haystack), "file read completed"
        if tool_name == "web_search":
            return haystack.lower() != "no results found.", "search produced results"
        if tool_name == "shell":
            return True, "command completed"
    except Exception as exc:
        return False, f"verification failed: {exc}"

    return True, "step completed"


def _step_to_replay_tool(step: dict) -> tuple[str, dict] | None:
    action = str(step.get("action") or "").strip().lower()
    inputs = dict(step.get("inputs") or {})
    target = str(step.get("target") or "").strip()
    if action == "shell":
        cmd = str(inputs.get("cmd") or target).strip()
        return ("shell", {"cmd": cmd}) if cmd else None
    if action == "read_file":
        path = str(inputs.get("path") or target).strip()
        return ("read_file", {"path": path}) if path else None
    if action == "write_file":
        path = str(inputs.get("path") or target).strip()
        content = str(inputs.get("content") or "").strip()
        return ("write_file", {"path": path, "content": content}) if path and content else None
    if action == "remember":
        key = str(inputs.get("key") or target).strip()
        value = str(inputs.get("value") or "").strip()
        return ("remember", {"key": key, "value": value}) if key and value else None
    if action == "web_search":
        query = str(inputs.get("query") or target).strip()
        return ("web_search", {"query": query}) if query else None
    return None


def _step_to_browser_workflow_step(step: dict) -> dict | None:
    action = str(step.get("action") or "").strip().lower()
    inputs = dict(step.get("inputs") or {})
    expected = str(step.get("expected") or "").strip()
    target = str(step.get("target") or "").strip()

    payload = None
    if action == "browser_goto":
        url = str(inputs.get("url") or target).strip()
        if url:
            payload = {"action": "goto", "url": url}
            if inputs.get("wait_until"):
                payload["wait_until"] = str(inputs["wait_until"])
    elif action == "browser_click":
        selector = str(inputs.get("selector") or target).strip()
        if selector:
            payload = {"action": "click", "selector": selector}
    elif action == "browser_fill":
        selector = str(inputs.get("selector") or target).strip()
        value = str(inputs.get("value") or "").strip()
        if selector and value:
            payload = {"action": "fill", "selector": selector, "value": value}
    elif action == "browser_press":
        key = str(inputs.get("key") or "").strip()
        selector = str(inputs.get("selector") or target).strip()
        if key:
            payload = {"action": "press", "key": key}
            if selector:
                payload["selector"] = selector
    elif action == "browser_wait_for":
        selector = str(inputs.get("selector") or target).strip()
        url_contains = str(inputs.get("url_contains") or "").strip()
        if selector or url_contains:
            payload = {"action": "wait_for"}
            if selector:
                payload["selector"] = selector
            if url_contains:
                payload["url_contains"] = url_contains
            if inputs.get("state"):
                payload["state"] = str(inputs["state"])
    elif action == "browser_extract_text":
        selector = str(inputs.get("selector") or target).strip()
        if selector:
            payload = {"action": "extract_text", "selector": selector}
            if inputs.get("name"):
                payload["name"] = str(inputs["name"])
    elif action == "browser_assert_text":
        selector = str(inputs.get("selector") or target).strip()
        step_expected = str(inputs.get("expected") or expected).strip()
        if selector and step_expected:
            payload = {"action": "assert_text", "selector": selector, "expected": step_expected}
    elif action == "browser_screenshot":
        payload = {"action": "screenshot"}
        if inputs.get("name") or target:
            payload["name"] = str(inputs.get("name") or target)
        if "full_page" in inputs:
            payload["full_page"] = bool(inputs.get("full_page"))

    if payload and "timeout_ms" in inputs:
        try:
            payload["timeout_ms"] = int(inputs["timeout_ms"])
        except (TypeError, ValueError):
            pass
    return payload


def _run_browser_batch(batch_steps: list[dict], start_index: int) -> tuple[str, bool, dict]:
    result, summary, err = _execute_browser_workflow(batch_steps)
    label = f"{start_index}" if len(batch_steps) == 1 else f"{start_index}-{start_index + len(batch_steps) - 1}"
    status = "error" if err else "ok"
    return f"  {label}. {status} (browser_workflow) {summary[:600]}", err, result


def _replay_demonstration(demo_id, execute=False, allow_risky=False):
    from memory import format_demonstration, get_demonstration, record_demonstration_feedback

    demo = get_demonstration(int(demo_id))
    if not demo:
        return f"Demonstration #{demo_id} not found.", True
    if not execute:
        return "Dry-run replay plan:\n" + format_demonstration(demo), False

    lines = [f"Replay demonstration #{demo['id']}: {demo['goal']}"]
    failed = False
    verification_failures = 0
    last_note = ""
    last_drift = None
    browser_batch: list[dict] = []
    browser_batch_start = 0

    def flush_browser_batch():
        nonlocal browser_batch, browser_batch_start, failed, verification_failures, last_note, last_drift
        if not browser_batch:
            return
        line, err, result = _run_browser_batch(browser_batch, browser_batch_start)
        lines.append(line)
        last_note = str(result.get("error") or result.get("title") or "browser batch complete")
        if err:
            failed = True
            verification_failures += 1
            if result.get("drift_report"):
                last_drift = result["drift_report"]
        browser_batch = []
        browser_batch_start = 0

    for index, step in enumerate(demo.get("steps", []), start=1):
        browser_step = _step_to_browser_workflow_step(step)
        if browser_step and step.get("executable"):
            if step.get("risk") in {"high", "destructive"} and not allow_risky:
                flush_browser_batch()
                lines.append(f"  {index}. blocked high-risk step: {step.get('instructions') or step.get('title')}")
                failed = True
                verification_failures += 1
                last_note = f"blocked risky step {index}"
                continue
            if step.get("risk") in {"high", "destructive"}:
                flush_browser_batch()
                try:
                    _approve_risky_replay_step(index, step)
                except CheckpointDeclined as exc:
                    lines.append(f"  {index}. blocked high-risk step: {exc}")
                    failed = True
                    verification_failures += 1
                    last_note = str(exc)
                    continue
            if not browser_batch:
                browser_batch_start = index
            browser_batch.append(browser_step)
            continue

        flush_browser_batch()
        mapping = _step_to_replay_tool(step)
        if not step.get("executable") or mapping is None:
            lines.append(f"  {index}. skipped manual step: {step.get('instructions') or step.get('title')}")
            continue
        if step.get("risk") in {"high", "destructive"} and not allow_risky:
            lines.append(f"  {index}. blocked high-risk step: {step.get('instructions') or step.get('title')}")
            failed = True
            verification_failures += 1
            last_note = f"blocked risky step {index}"
            continue
        if step.get("risk") in {"high", "destructive"}:
            try:
                _approve_risky_replay_step(index, step)
            except CheckpointDeclined as exc:
                lines.append(f"  {index}. blocked high-risk step: {exc}")
                failed = True
                verification_failures += 1
                last_note = str(exc)
                continue
        tool_name, tool_inputs = mapping
        tool_result = dispatch_structured(tool_name, tool_inputs)
        status = "error" if not tool_result.ok else "ok"
        verified, verification = _verify_replay_result(step, tool_name, tool_inputs, tool_result.output, not tool_result.ok)
        verification_label = "verified" if verified else "unverified"
        lines.append(
            f"  {index}. {status} ({tool_name}) {verification_label}: {verification} :: {tool_result.output[:120]}"
        )
        last_note = verification
        if not tool_result.ok:
            failed = True
            verification_failures += 1
        elif not verified:
            failed = True
            verification_failures += 1
    flush_browser_batch()
    if not last_note:
        last_note = f"replay completed with {verification_failures} verification issue(s)"
    record_demonstration_feedback(
        demo["id"],
        success=not failed,
        confidence=demo.get("last_confidence"),
        note=last_note,
        drift=last_drift,
    )
    return "\n".join(lines), failed


def dispatch_structured(name: str, inputs: dict) -> ToolExecutionResult:
    """Route a tool call to its implementation and return a typed result envelope."""
    import memory as mem

    if not isinstance(inputs, dict):
        return _structured_tool_result(
            name,
            status=ToolExecutionStatus.VALIDATION_ERROR,
            ok=False,
            summary="Tool inputs must be a JSON object.",
            data={"received_type": type(inputs).__name__},
        )

    if name == "shell":
        cmd = inputs["cmd"]
        timeout = inputs.get("timeout", 30)
        output, err = _shell(cmd, timeout)
        summary = output.splitlines()[0][:200] if output else f"Ran shell command: {cmd[:80]}"
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=summary,
            output=output,
            data={"cmd": cmd, "timeout": timeout},
        )
    if name == "read_file":
        path = inputs["path"]
        output, err = _read_file(path)
        summary = output.splitlines()[0][:200] if err else f"Read file {path} ({len(output)} chars)"
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=summary,
            output=output,
            data={"path": path},
            artifacts=[_artifact("file", label="read_file", path=str(path))],
        )
    if name == "write_file":
        path = inputs["path"]
        content = inputs["content"]
        output, err = _write_file(path, content)
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"path": path, "bytes": len(content)},
            artifacts=[_artifact("file", label="write_file", path=str(path), bytes=len(content))],
        )
    if name == "web_search":
        query = inputs["query"]
        output, err = _web_search(query)
        summary = output.splitlines()[0][:200] if output else f"Searched for {query}"
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=summary,
            output=output,
            data={"query": query},
        )
    if name == "slack_channel":
        action = inputs["action"]
        output, err = _slack_channel(
            action,
            to=inputs.get("to", ""),
            channel_id=inputs.get("channel_id", ""),
            message_id=inputs.get("message_id", ""),
            user_id=inputs.get("user_id", ""),
            content=inputs.get("content", ""),
            emoji=inputs.get("emoji", ""),
            limit=inputs.get("limit", 20),
            timeout=inputs.get("timeout", 30),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0][:200] if output else f"slack_channel {action}",
            output=output,
            data={"action": action, "channel_id": inputs.get("channel_id", ""), "user_id": inputs.get("user_id", "")},
            artifacts=[_artifact("slack", label=action, channel=inputs.get("channel_id", "") or inputs.get("to", ""))],
        )
    if name == "discord_channel":
        action = inputs["action"]
        output, err = _discord_channel(
            action,
            to=inputs.get("to", ""),
            channel_id=inputs.get("channel_id", ""),
            message_id=inputs.get("message_id", ""),
            user_id=inputs.get("user_id", ""),
            message=inputs.get("message", ""),
            emoji=inputs.get("emoji", ""),
            limit=inputs.get("limit", 20),
            silent=bool(inputs.get("silent", False)),
            timeout=inputs.get("timeout", 30),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0][:200] if output else f"discord_channel {action}",
            output=output,
            data={"action": action, "channel_id": inputs.get("channel_id", ""), "user_id": inputs.get("user_id", "")},
            artifacts=[_artifact("discord", label=action, channel=inputs.get("channel_id", "") or inputs.get("to", ""))],
        )
    if name == "github_cli":
        action = inputs["action"]
        output, err = _github_cli(
            action,
            repo=inputs.get("repo", ""),
            number=inputs.get("number"),
            run_id=inputs.get("run_id", ""),
            endpoint=inputs.get("endpoint", ""),
            jq=inputs.get("jq", ""),
            method=inputs.get("method", "GET"),
            limit=inputs.get("limit", 10),
            fields=inputs.get("fields", {}),
            timeout=inputs.get("timeout", 30),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0][:200] if output else f"github_cli {action}",
            output=output,
            data={"action": action, "repo": inputs.get("repo", "")},
            artifacts=[_artifact("github", label=action, repo=inputs.get("repo", ""))],
        )
    if name == "tmux_session":
        action = inputs["action"]
        output, err = _tmux_session(
            action,
            target=inputs.get("target", ""),
            session_name=inputs.get("session_name", ""),
            command=inputs.get("command", ""),
            text=inputs.get("text", ""),
            keys=inputs.get("keys", []),
            include_enter=bool(inputs.get("include_enter", False)),
            lines=inputs.get("lines", 20),
            timeout=inputs.get("timeout", 20),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0][:200] if output else f"tmux_session {action}",
            output=output,
            data={"action": action, "target": inputs.get("target", ""), "session_name": inputs.get("session_name", "")},
            artifacts=[_artifact("tmux", label=action, session=inputs.get("session_name", "") or inputs.get("target", ""))],
        )
    if name == "browser_workflow":
        steps = inputs["steps"]
        browser = inputs.get("browser", "chromium")
        headless = inputs.get("headless", True)
        capture = inputs.get("capture_final_screenshot", True)
        payload, summary, err = _execute_browser_workflow(
            steps,
            browser=browser,
            headless=headless,
            capture_final_screenshot=capture,
            session_id=inputs.get("session_id", ""),
            resume_session=bool(inputs.get("resume_session", False)),
            resume_last_page=bool(inputs.get("resume_last_page", False)),
            persist_session=bool(inputs.get("persist_session", True)),
            verify_resumed_state=bool(inputs.get("verify_resumed_state", True)),
            auto_reanchor=bool(inputs.get("auto_reanchor", True)),
            attach_endpoint=inputs.get("attach_endpoint", ""),
        )
        screenshots = payload.get("screenshots") or []
        artifacts = [
            _artifact(
                "browser_screenshot",
                label=str(item.get("caption") or item.get("name") or f"shot_{index}"),
                path=str(item.get("path") or ""),
                action=item.get("action", ""),
            )
            for index, item in enumerate(screenshots, start=1)
        ]
        verification = None
        if payload.get("step_results"):
            failed_steps = [item for item in payload["step_results"] if not item.get("ok", True)]
            verification = VerificationResult(
                ok=not failed_steps,
                summary=f"{len(payload['step_results']) - len(failed_steps)}/{len(payload['step_results'])} browser steps succeeded",
                details={"failed_steps": len(failed_steps)},
            )
        return _structured_tool_result(
            name,
            status=_status_from_output(summary, err),
            ok=not err,
            summary=summary,
            output=summary,
            data={
                "browser": browser,
                "headless": bool(headless),
                "session_id": inputs.get("session_id", ""),
                "resume_session": bool(inputs.get("resume_session", False)),
                "persist_session": bool(inputs.get("persist_session", True)),
                "verify_resumed_state": bool(inputs.get("verify_resumed_state", True)),
                "auto_reanchor": bool(inputs.get("auto_reanchor", True)),
                "attach_endpoint": inputs.get("attach_endpoint", ""),
                "payload": payload,
            },
            verification=verification,
            artifacts=artifacts,
        )
    if name == "browser_session":
        action = inputs["action"]
        output, err = _browser_session(
            action,
            session_id=inputs.get("session_id", ""),
            browser=inputs.get("browser", "chromium"),
            headless=bool(inputs.get("headless", True)),
            attach_endpoint=inputs.get("attach_endpoint", ""),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0][:200] if output else f"browser_session {action}",
            output=output,
            data={"action": action, "session_id": inputs.get("session_id", "")},
            artifacts=[_artifact("browser_session", label=inputs.get("session_id", "") or action)],
        )
    if name == "remember":
        mem.learn(inputs["key"], inputs["value"])
        return _structured_tool_result(
            name,
            status=ToolExecutionStatus.SUCCESS,
            ok=True,
            summary=f"Saved: {inputs['key']}",
            output=f"Saved: {inputs['key']}",
            data={"key": inputs["key"], "value": inputs["value"]},
            artifacts=[_artifact("memory_fact", label=inputs["key"])],
        )
    if name == "remember_person":
        output, err = _remember_person(
            inputs["name"],
            relationship=inputs.get("relationship", ""),
            notes=inputs.get("notes", ""),
            aliases=inputs.get("aliases", []),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"name": inputs["name"]},
            artifacts=[_artifact("person", label=inputs["name"])],
        )
    if name == "remember_project":
        output, err = _remember_project(
            inputs["name"],
            status=inputs.get("status", ""),
            notes=inputs.get("notes", ""),
            tags=inputs.get("tags", []),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"name": inputs["name"]},
            artifacts=[_artifact("project", label=inputs["name"])],
        )
    if name == "remember_commitment":
        output, err = _remember_commitment(
            inputs["title"],
            owner=inputs.get("owner", "user"),
            counterparty=inputs.get("counterparty", ""),
            project=inputs.get("project", ""),
            due_at=inputs.get("due_at", ""),
            status=inputs.get("status", "open"),
            notes=inputs.get("notes", ""),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"title": inputs["title"]},
            artifacts=[_artifact("commitment", label=inputs["title"])],
        )
    if name == "list_people":
        output, err = _list_people(inputs.get("query", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", "")},
        )
    if name == "list_projects":
        output, err = _list_projects(inputs.get("query", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", "")},
        )
    if name == "list_commitments":
        output, err = _list_commitments(inputs.get("query", ""), inputs.get("status", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", ""), "status": inputs.get("status", "")},
        )
    if name == "chief_of_staff_briefing":
        output, err = _chief_of_staff_briefing(inputs.get("query", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", "")},
        )
    if name == "ingest_signal":
        output, err = _ingest_signal(
            inputs["kind"],
            inputs["content"],
            source=inputs.get("source", ""),
            title=inputs.get("title", ""),
            happened_at=inputs.get("happened_at", ""),
            metadata=inputs.get("metadata", {}),
        )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"kind": inputs["kind"], "source": inputs.get("source", "")},
            artifacts=[_artifact("signal", label=inputs.get("title") or inputs["kind"])],
        )
    if name == "list_signals":
        output, err = _list_signals(inputs.get("query", ""), inputs.get("kind", ""), inputs.get("source", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", ""), "kind": inputs.get("kind", ""), "source": inputs.get("source", "")},
        )
    if name == "list_demonstrations":
        output, err = _list_demonstrations(inputs.get("query", ""))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"query": inputs.get("query", "")},
        )
    if name == "explain_demonstration":
        output, err = _explain_demonstration(inputs["id"])
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"id": inputs["id"]},
        )
    if name == "replay_demonstration":
        output, err = _replay_demonstration(
            inputs["id"],
            execute=bool(inputs.get("execute", False)),
            allow_risky=bool(inputs.get("allow_risky", False)),
        )
        verification = None
        lowered = output.lower()
        if bool(inputs.get("execute", False)):
            verification = VerificationResult(
                ok=not err,
                summary="replay completed" if not err else "replay had errors or blocked steps",
                details={"contains_drift": "drift" in lowered},
            )
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={
                "id": inputs["id"],
                "execute": bool(inputs.get("execute", False)),
                "allow_risky": bool(inputs.get("allow_risky", False)),
            },
            verification=verification,
        )
    if name == "create_skill":
        output, err = _create_skill(inputs["name"], inputs["description"], inputs["code"])
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"name": inputs["name"]},
            artifacts=[_artifact("skill", label=inputs["name"])],
        )
    if name == "use_skill":
        output, err = _use_skill(inputs["name"], inputs.get("inputs", {}))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output.splitlines()[0] if output else f"Skill {inputs['name']} executed",
            output=output,
            data={"name": inputs["name"]},
        )
    if name == "list_skills":
        skills = mem.list_skills()
        if not skills:
            return _structured_tool_result(
                name,
                status=ToolExecutionStatus.SUCCESS,
                ok=True,
                summary="No skills created yet.",
                output="No skills created yet.",
            )
        lines = [
            f"  {skill['name']}: {skill['description']} "
            f"(used {skill['uses']}x, v{skill.get('current_version', 1)})"
            for skill in skills
        ]
        output = "Available skills:\n" + "\n".join(lines)
        return _structured_tool_result(
            name,
            status=ToolExecutionStatus.SUCCESS,
            ok=True,
            summary="Available skills listed.",
            output=output,
        )
    if name == "skill_history":
        output, err = _skill_history(inputs["name"])
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err, default_error=ToolExecutionStatus.NOT_FOUND),
            ok=not err,
            summary=output.splitlines()[0],
            output=output,
            data={"name": inputs["name"]},
        )
    if name == "rollback_skill":
        output, err = _rollback_skill(inputs["name"], int(inputs["version"]))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output,
            output=output,
            data={"name": inputs["name"], "version": int(inputs["version"])},
            artifacts=[_artifact("skill", label=inputs["name"], version=int(inputs["version"]))],
        )
    if name.startswith("skill_"):
        output, err = _use_skill(name[6:], inputs.get("inputs", {}))
        return _structured_tool_result(
            name,
            status=_status_from_output(output, err),
            ok=not err,
            summary=output.splitlines()[0] if output else f"Skill {name[6:]} executed",
            output=output,
            data={"name": name[6:]},
        )
    return _structured_tool_result(
        name,
        status=ToolExecutionStatus.NOT_FOUND,
        ok=False,
        summary=f"Unknown tool: {name}",
        output=f"Unknown tool: {name}",
    )


def dispatch(name: str, inputs: dict) -> tuple[str, bool]:
    """Route a tool call to its implementation."""
    result = dispatch_structured(name, inputs)
    return result.output, not result.ok
