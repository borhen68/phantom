"""
PHANTOM Tools — built-in tools + dynamic skill loader.

Skills are Python functions that agents can create at runtime and reuse in
future sessions.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

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
            },
            "required": ["steps"],
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
        or (tool_name in {"web_search", "browser_workflow"} and checkpoints.confirm_web)
        or (tool_name in {"create_skill", "rollback_skill"} and checkpoints.confirm_skill_changes)
    )
    if not required:
        return
    if not prompt_user(f"PHANTOM approval required for {tool_name}: {detail}"):
        raise CheckpointDeclined(f"Human checkpoint declined {tool_name}: {detail}")


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


def _browser_workflow(steps, browser="chromium", headless=True, capture_final_screenshot=True):
    _, summary, err = _execute_browser_workflow(
        steps,
        browser=browser,
        headless=headless,
        capture_final_screenshot=capture_final_screenshot,
    )
    return summary, err


def _execute_browser_workflow(steps, browser="chromium", headless=True, capture_final_screenshot=True):
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
        )
        return result, summarize_browser_result(result), not bool(result.get("ok", True))
    except (ModuleNotFoundError, ToolSafetyError, CheckpointDeclined) as exc:
        return {"ok": False, "error": str(exc), "step_results": [], "screenshots": []}, str(exc), True
    except Exception as exc:
        return {"ok": False, "error": str(exc), "step_results": [], "screenshots": []}, str(exc), True


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
        result, err = dispatch(tool_name, tool_inputs)
        status = "error" if err else "ok"
        verified, verification = _verify_replay_result(step, tool_name, tool_inputs, result, err)
        verification_label = "verified" if verified else "unverified"
        lines.append(f"  {index}. {status} ({tool_name}) {verification_label}: {verification} :: {result[:120]}")
        last_note = verification
        if err:
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


def dispatch(name: str, inputs: dict) -> tuple[str, bool]:
    """Route a tool call to its implementation."""
    import memory as mem

    if not isinstance(inputs, dict):
        return "Tool inputs must be a JSON object.", True

    if name == "shell":
        return _shell(inputs["cmd"], inputs.get("timeout", 30))
    if name == "read_file":
        return _read_file(inputs["path"])
    if name == "write_file":
        return _write_file(inputs["path"], inputs["content"])
    if name == "web_search":
        return _web_search(inputs["query"])
    if name == "browser_workflow":
        return _browser_workflow(
            inputs["steps"],
            browser=inputs.get("browser", "chromium"),
            headless=inputs.get("headless", True),
            capture_final_screenshot=inputs.get("capture_final_screenshot", True),
        )
    if name == "remember":
        mem.learn(inputs["key"], inputs["value"])
        return f"Saved: {inputs['key']}", False
    if name == "list_demonstrations":
        return _list_demonstrations(inputs.get("query", ""))
    if name == "explain_demonstration":
        return _explain_demonstration(inputs["id"])
    if name == "replay_demonstration":
        return _replay_demonstration(
            inputs["id"],
            execute=bool(inputs.get("execute", False)),
            allow_risky=bool(inputs.get("allow_risky", False)),
        )
    if name == "create_skill":
        return _create_skill(inputs["name"], inputs["description"], inputs["code"])
    if name == "use_skill":
        return _use_skill(inputs["name"], inputs.get("inputs", {}))
    if name == "list_skills":
        skills = mem.list_skills()
        if not skills:
            return "No skills created yet.", False
        lines = [
            f"  {skill['name']}: {skill['description']} "
            f"(used {skill['uses']}x, v{skill.get('current_version', 1)})"
            for skill in skills
        ]
        return "Available skills:\n" + "\n".join(lines), False
    if name == "skill_history":
        return _skill_history(inputs["name"])
    if name == "rollback_skill":
        return _rollback_skill(inputs["name"], int(inputs["version"]))
    if name.startswith("skill_"):
        return _use_skill(name[6:], inputs.get("inputs", {}))
    return f"Unknown tool: {name}", True
