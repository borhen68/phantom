"""Subprocess runtime for generated PHANTOM skills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.safety import ToolSafetyError, current_policy, skill_exec_globals, validate_skill_code


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _apply_resource_limits():
    try:
        import resource
    except ImportError:  # pragma: no cover - non-Unix fallback
        return

    policy = current_policy()
    limits = [
        ("RLIMIT_CPU", max(1, policy.skill_timeout)),
        ("RLIMIT_FSIZE", _env_int("PHANTOM_SKILL_MAX_FILE_BYTES", 1_048_576)),
        ("RLIMIT_NOFILE", _env_int("PHANTOM_SKILL_MAX_OPEN_FILES", 64)),
        ("RLIMIT_NPROC", _env_int("PHANTOM_SKILL_MAX_PROCESSES", 1)),
        ("RLIMIT_CORE", 0),
        ("RLIMIT_AS", _env_int("PHANTOM_SKILL_MAX_MEMORY_BYTES", 268_435_456)),
    ]
    for name, limit in limits:
        resource_name = getattr(resource, name, None)
        if resource_name is None:
            continue
        try:
            resource.setrlimit(resource_name, (limit, limit))
        except (OSError, ValueError):
            continue


def _command_available(args: list[str]) -> bool:
    try:
        subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _base_skill_command(script_path: str | Path) -> list[str]:
    return [sys.executable, "-I", str(script_path)]


def _bubblewrap_command(script_path: str | Path) -> list[str]:
    policy = current_policy()
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--proc", "/proc",
        "--dev", "/dev",
        "--ro-bind", "/", "/",
        "--chdir", str(policy.workspace_root),
        "--setenv", "HOME", str(policy.data_root),
        "--",
        *_base_skill_command(script_path),
    ]


def _nsjail_command(script_path: str | Path) -> list[str]:
    policy = current_policy()
    return [
        "nsjail",
        "-Mo",
        "--disable_proc",
        "--iface_no_lo",
        "--cwd", str(policy.workspace_root),
        "--",
        *_base_skill_command(script_path),
    ]


def _unshare_command(script_path: str | Path) -> list[str]:
    return ["unshare", "--net", "--", *_base_skill_command(script_path)]


def build_skill_commands(script_path: str | Path) -> list[list[str]]:
    base = _base_skill_command(script_path)
    if sys.platform != "linux":
        return [base]

    requested = os.environ.get("PHANTOM_SKILL_SANDBOX", "auto").strip().lower()
    candidates: list[tuple[str, list[str], list[str]]] = []
    if requested in {"auto", "bwrap", "bubblewrap"}:
        candidates.append(("bwrap", ["bwrap", "--version"], _bubblewrap_command(script_path)))
    if requested in {"auto", "nsjail"}:
        candidates.append(("nsjail", ["nsjail", "--help"], _nsjail_command(script_path)))
    if requested in {"auto", "unshare"}:
        candidates.append(("unshare", ["unshare", "--help"], _unshare_command(script_path)))
    if requested in {"none", "python"}:
        return [base]

    commands = []
    for _, check_cmd, sandbox_cmd in candidates:
        if _command_available(check_cmd):
            commands.append(sandbox_cmd)
    commands.append(base)
    return commands


def build_skill_command(script_path: str | Path) -> list[str]:
    return build_skill_commands(script_path)[0]


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    code = payload["code"]
    inputs = payload.get("inputs", {})

    _apply_resource_limits()
    validate_skill_code(code, policy=current_policy())
    globals_dict = skill_exec_globals()
    namespace = dict(globals_dict)
    exec(code, namespace)
    runner = namespace.get("run")
    if not callable(runner):
        raise ToolSafetyError("Skill is missing a callable run(inputs) function.")

    result = runner(inputs)
    sys.stdout.write(json.dumps({"ok": True, "result": str(result)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
