"""Safety policy for PHANTOM tool execution."""

from __future__ import annotations

import ast
import builtins
import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.settings import data_root as configured_data_root, runtime_settings, workspace_root

SAFE_SKILL_MODULES = {
    "base64",
    "collections",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "hashlib",
    "io",
    "itertools",
    "json",
    "math",
    "operator",
    "pathlib",
    "re",
    "statistics",
    "string",
    "textwrap",
    "time",
    "typing",
    "uuid",
}
ALLOWED_SKILL_NODE_TYPES = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Expr,
    ast.Return,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.With,
    ast.withitem,
    ast.For,
    ast.While,
    ast.If,
    ast.Try,
    ast.ExceptHandler,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.keyword,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
    ast.And,
    ast.Or,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)
FORBIDDEN_SKILL_CALLS = {
    "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "dir",
    "type", "super",
}
FORBIDDEN_SKILL_NAMES = {
    "os", "sys", "subprocess", "socket", "importlib", "inspect", "builtins",
    "types", "__builtins__", "__loader__", "__spec__", "__package__",
}
FORBIDDEN_SHELL_PATTERNS = [
    # All patterns use re.IGNORECASE so case is handled consistently and explicitly.
    (r"(^|\s)sudo(\s|$)", "sudo is blocked."),
    (r"rm\s+-rf\s+/(?:\s|$)", "Destructive root deletion is blocked."),
    (r"git\s+reset\s+--hard", "Hard resets are blocked."),
    (r"\bmkfs(\.\w+)?\b", "Disk formatting commands are blocked."),
    (r"\bdd\s+if=", "Raw disk copy commands are blocked."),
    (r"\bshutdown\b", "System shutdown commands are blocked."),
    (r"\breboot\b", "System reboot commands are blocked."),
    (r"kill\s+-9\s+-1\b", "Process-table kill commands are blocked."),
    (r"curl\b.*\|\s*(sh|bash)\b", "Piped remote shell installers are blocked."),
    (r"wget\b.*\|\s*(sh|bash)\b", "Piped remote shell installers are blocked."),
]


class ToolSafetyError(ValueError):
    """Raised when a tool call violates the configured safety policy."""


@dataclass(frozen=True)
class SafetyPolicy:
    workspace_root: Path
    data_root: Path
    allow_shell: bool = True
    allow_web: bool = True
    allow_outside_workspace: bool = False
    allow_unsafe_skills: bool = False
    skill_timeout: int = 10


def current_policy() -> SafetyPolicy:
    settings = runtime_settings()
    try:
        skill_timeout = max(1, int(os.environ.get("PHANTOM_SKILL_TIMEOUT", "10")))
    except ValueError:
        skill_timeout = 10
    return SafetyPolicy(
        workspace_root=workspace_root(),
        data_root=configured_data_root(),
        allow_shell=settings.allow_shell,
        allow_web=settings.allow_web,
        allow_outside_workspace=settings.allow_outside_workspace,
        allow_unsafe_skills=settings.allow_unsafe_skills,
        skill_timeout=skill_timeout,
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_path_allowed(path: str | Path, *, write: bool, policy: SafetyPolicy | None = None) -> Path:
    policy = policy or current_policy()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = policy.workspace_root / candidate
    resolved = candidate.resolve(strict=False)

    if policy.allow_outside_workspace:
        return resolved

    allowed_roots = [policy.workspace_root.resolve(strict=False), policy.data_root.resolve(strict=False)]
    if any(_is_relative_to(resolved, root) for root in allowed_roots):
        return resolved

    action = "write" if write else "read"
    raise ToolSafetyError(
        f"Refusing to {action} outside allowed roots. "
        f"Allowed roots: {policy.workspace_root}, {policy.data_root}"
    )


def validate_shell_command(cmd: str, policy: SafetyPolicy | None = None) -> str:
    policy = policy or current_policy()
    if not policy.allow_shell:
        raise ToolSafetyError("Shell execution is disabled by PHANTOM_ALLOW_SHELL=0.")

    # Use re.IGNORECASE explicitly so case handling is unambiguous (no silent lowercasing).
    for pattern, message in FORBIDDEN_SHELL_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            raise ToolSafetyError(message)
    if not policy.allow_web and re.search(r"\b(curl|wget)\b", cmd, re.IGNORECASE):
        raise ToolSafetyError("Network shell commands are disabled by PHANTOM_ALLOW_WEB=0.")
    return cmd


def validate_skill_code(code: str, policy: SafetyPolicy | None = None) -> None:
    policy = policy or current_policy()
    if policy.allow_unsafe_skills:
        return

    tree = ast.parse(code, filename="<skill>", mode="exec")
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    run_node = None
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            if isinstance(node.value.value, str):
                continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _validate_skill_import(node)
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            if run_node is not None:
                raise ToolSafetyError("Skill modules may define only one top-level run(inputs) function.")
            _validate_run_signature(node)
            run_node = node
            continue
        raise ToolSafetyError(
            "Skill modules may only contain safe imports and a single top-level run(inputs) function."
        )

    if run_node is None:
        raise ToolSafetyError("Skill code must define a `run(inputs: dict) -> str` function.")

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_SKILL_NODE_TYPES):
            raise ToolSafetyError(f"Skill uses unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.FunctionDef):
            if node is not run_node or parents.get(node) is not tree:
                raise ToolSafetyError("Nested or helper functions are not allowed in generated skills.")
        elif isinstance(node, ast.AsyncFunctionDef):
            raise ToolSafetyError("Async functions are not allowed in generated skills.")
        elif isinstance(node, ast.ClassDef):
            raise ToolSafetyError("Generated skills may not define classes.")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            _validate_skill_import(node)
        elif isinstance(node, ast.Call):
            _validate_skill_call(node)
        elif isinstance(node, ast.Name):
            _validate_skill_name(node.id)
        elif isinstance(node, ast.Attribute):
            _validate_skill_attribute(node)


def _validate_run_signature(node: ast.FunctionDef) -> None:
    if node.decorator_list:
        raise ToolSafetyError("Generated skills may not use decorators.")
    args = node.args
    if args.posonlyargs or args.vararg or args.kwonlyargs or args.kwarg:
        raise ToolSafetyError("run(inputs) must use exactly one positional argument.")
    if len(args.args) != 1 or args.args[0].arg != "inputs":
        raise ToolSafetyError("Skill entrypoint must be exactly run(inputs).")


def _validate_skill_import(node: ast.Import | ast.ImportFrom) -> None:
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    else:
        if node.level:
            raise ToolSafetyError("Relative imports are not allowed in generated skills.")
        modules = [node.module or ""]
    for module_name in modules:
        root = module_name.split(".")[0]
        if root not in SAFE_SKILL_MODULES:
            raise ToolSafetyError(f"Skill imports blocked module: {root or '(relative import)'}")


def _validate_skill_call(node: ast.Call) -> None:
    if isinstance(node.func, ast.Name):
        _validate_skill_name(node.func.id)
        if node.func.id in FORBIDDEN_SKILL_CALLS:
            raise ToolSafetyError(f"Skill uses blocked builtin: {node.func.id}")
    elif isinstance(node.func, ast.Attribute):
        _validate_skill_attribute(node.func)
    else:
        raise ToolSafetyError("Generated skills may only call named functions or public attributes.")


def _validate_skill_name(name: str) -> None:
    if name in FORBIDDEN_SKILL_NAMES:
        raise ToolSafetyError(f"Skill references blocked name: {name}")
    if name.startswith("__"):
        raise ToolSafetyError(f"Skill uses blocked dunder name: {name}")


def _validate_skill_attribute(node: ast.Attribute) -> None:
    if node.attr.startswith("_"):
        raise ToolSafetyError(f"Skill uses blocked private attribute: {node.attr}")
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name):
        _validate_skill_name(root.id)


def _safe_import(policy: SafetyPolicy):
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if not policy.allow_unsafe_skills and root not in SAFE_SKILL_MODULES:
            raise ToolSafetyError(f"Skill import blocked: {root}")
        return real_import(name, globals, locals, fromlist, level)

    return _import


def _safe_open(policy: SafetyPolicy):
    def _open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise ToolSafetyError("Generated skills may only read files. Use write_file for writes.")
        resolved = ensure_path_allowed(path, write=False, policy=policy)
        return builtins.open(resolved, mode, *args, **kwargs)

    return _open


def skill_exec_globals(policy: SafetyPolicy | None = None) -> dict:
    policy = policy or current_policy()
    safe_builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "Exception": Exception,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "open": _safe_open(policy),
        "range": range,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "__import__": _safe_import(policy),
    }
    return {
        "__builtins__": safe_builtins,
        "Path": Path,
    }
