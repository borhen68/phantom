"""Runtime configuration, secret loading, and redaction helpers."""

from __future__ import annotations

import contextvars
import getpass
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_PRICE_TABLE = {
    # Anthropic models — prices in USD per million tokens (public pricing, March 2026).
    "claude-haiku-4-5-20251001": {"input_per_million": 0.80, "output_per_million": 4.00},
    "claude-sonnet-4-5": {"input_per_million": 3.00, "output_per_million": 15.00},
    # Aliases kept for forward compatibility
    "claude-haiku-4-5": {"input_per_million": 0.80, "output_per_million": 4.00},
    "claude-sonnet-4-5-20251001": {"input_per_million": 3.00, "output_per_million": 15.00},
    # OpenAI models — prices in USD per million tokens (public pricing, March 2026).
    "gpt-4o": {"input_per_million": 2.50, "output_per_million": 10.00},
    "gpt-4o-mini": {"input_per_million": 0.15, "output_per_million": 0.60},
    "o1": {"input_per_million": 15.00, "output_per_million": 60.00},
    "o1-mini": {"input_per_million": 1.10, "output_per_million": 4.40},
}
ALLOWED_SECRET_NAMES = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY"}
_SCOPE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "phantom_scope_override",
    default=None,
)
_WORKSPACE_OVERRIDE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "phantom_workspace_override",
    default=None,
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float | None) -> float | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def workspace_root() -> Path:
    override = _WORKSPACE_OVERRIDE.get()
    value = override or os.environ.get("PHANTOM_WORKSPACE", os.getcwd())
    return Path(value).expanduser().resolve(strict=False)


def data_root() -> Path:
    return Path(os.environ.get("PHANTOM_HOME", str(Path.home() / ".phantom"))).expanduser().resolve(strict=False)


def scope_id() -> str:
    override = _SCOPE_OVERRIDE.get()
    if override:
        return override
    explicit = os.environ.get("PHANTOM_SCOPE")
    if explicit:
        return explicit
    return f"{getpass.getuser()}::{workspace_root()}"


@contextmanager
def override_scope(scope: str | None):
    token = _SCOPE_OVERRIDE.set((scope or "").strip() or None)
    try:
        yield
    finally:
        _SCOPE_OVERRIDE.reset(token)


@contextmanager
def override_workspace(path: str | os.PathLike[str] | None):
    token = _WORKSPACE_OVERRIDE.set(str(path).strip() if path else None)
    try:
        yield
    finally:
        _WORKSPACE_OVERRIDE.reset(token)


def _safe_scope_fragment() -> str:
    scope = scope_id()
    chars = [char if char.isalnum() else "_" for char in scope]
    return "".join(chars)[:80] or "default_scope"


def skill_root() -> Path:
    root = data_root() / "skills" / _safe_scope_fragment()
    root.mkdir(parents=True, exist_ok=True)
    return root


def trace_root() -> Path:
    root = data_root() / "traces"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class BudgetSettings:
    max_llm_calls: int = 24
    max_tool_calls: int = 48
    max_llm_calls_per_minute: int | None = None
    max_tool_calls_per_minute: int | None = None
    max_input_tokens: int = 120_000
    max_output_tokens: int = 40_000
    max_total_cost_usd: float | None = None
    max_parallelism: int = 3
    max_replans: int = 2
    max_critic_blocks: int = 3
    stop_file: str | None = None
    price_table: dict[str, dict[str, float | None]] = field(default_factory=lambda: DEFAULT_PRICE_TABLE.copy())


@dataclass(frozen=True)
class CheckpointSettings:
    enabled: bool = False
    confirm_plan: bool = False
    confirm_shell: bool = True
    confirm_writes: bool = True
    confirm_web: bool = False
    confirm_skill_changes: bool = True


@dataclass(frozen=True)
class SecretSettings:
    provider: str
    anthropic_key: str | None
    openai_key: str | None
    groq_key: str | None
    audit_labels: tuple[str, ...]
    secrets_file: str | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    budget: BudgetSettings
    checkpoints: CheckpointSettings
    secrets: SecretSettings
    allow_shell: bool
    allow_web: bool
    allow_outside_workspace: bool
    allow_unsafe_skills: bool


def _load_secrets_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return {}
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {
            key: value
            for key, value in payload.items()
            if key in ALLOWED_SECRET_NAMES and value is not None
        }
    except Exception:
        return {}


def load_secret(name: str) -> tuple[str | None, tuple[str, ...], str]:
    secrets_file = os.environ.get("PHANTOM_SECRETS_FILE")
    file_payload = _load_secrets_file(secrets_file)
    file_key = os.environ.get(f"{name}_FILE")
    if file_key:
        path = Path(file_key).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip(), (f"{name}_FILE",), "file"

    if name in file_payload:
        return str(file_payload[name]).strip(), (name, "PHANTOM_SECRETS_FILE"), "secrets-file"

    value = os.environ.get(name)
    if value:
        return value.strip(), (name,), "env"

    return None, tuple(), "missing"


def secret_settings() -> SecretSettings:
    anthropic_key, labels, provider = load_secret("ANTHROPIC_API_KEY")
    openai_key, _, _ = load_secret("OPENAI_API_KEY")
    groq_key, _, _ = load_secret("GROQ_API_KEY")
    return SecretSettings(
        provider=provider,
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        groq_key=groq_key,
        audit_labels=labels,
        secrets_file=os.environ.get("PHANTOM_SECRETS_FILE"),
    )


def _price_table() -> dict[str, dict[str, float | None]]:
    raw = os.environ.get("PHANTOM_PRICE_TABLE_JSON")
    if not raw:
        return DEFAULT_PRICE_TABLE.copy()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return DEFAULT_PRICE_TABLE.copy()


def provider_timeout_seconds() -> int:
    return max(1, _env_int("PHANTOM_API_TIMEOUT_SECONDS", 120))


def provider_max_retries() -> int:
    return max(0, _env_int("PHANTOM_PROVIDER_RETRIES", 2))


def provider_retry_backoff_seconds() -> float:
    value = _env_float("PHANTOM_PROVIDER_RETRY_BACKOFF_SECONDS", 1.0)
    return max(0.0, float(value if value is not None else 1.0))


def procedure_autoplay_enabled() -> bool:
    return _env_bool("PHANTOM_AUTO_REPLAY_PROCEDURES", True)


def procedure_min_confidence() -> float:
    value = _env_float("PHANTOM_PROCEDURE_MIN_CONFIDENCE", 0.8)
    return max(0.0, min(1.0, float(value if value is not None else 0.8)))


def procedure_min_reliability() -> float:
    value = _env_float("PHANTOM_PROCEDURE_MIN_RELIABILITY", 0.6)
    return max(0.0, min(1.0, float(value if value is not None else 0.6)))


def budget_settings() -> BudgetSettings:
    return BudgetSettings(
        max_llm_calls=_env_int("PHANTOM_MAX_LLM_CALLS", 24),
        max_tool_calls=_env_int("PHANTOM_MAX_TOOL_CALLS", 48),
        max_llm_calls_per_minute=_env_optional_int("PHANTOM_MAX_LLM_CALLS_PER_MINUTE", None),
        max_tool_calls_per_minute=_env_optional_int("PHANTOM_MAX_TOOL_CALLS_PER_MINUTE", None),
        max_input_tokens=_env_int("PHANTOM_MAX_INPUT_TOKENS", 120_000),
        max_output_tokens=_env_int("PHANTOM_MAX_OUTPUT_TOKENS", 40_000),
        max_total_cost_usd=_env_float("PHANTOM_MAX_COST_USD", None),
        max_parallelism=max(1, _env_int("PHANTOM_MAX_PARALLELISM", 3)),
        max_replans=max(0, _env_int("PHANTOM_MAX_REPLANS", 2)),
        max_critic_blocks=max(1, _env_int("PHANTOM_MAX_CRITIC_BLOCKS", 3)),
        stop_file=os.environ.get("PHANTOM_STOP_FILE"),
        price_table=_price_table(),
    )


def checkpoint_settings() -> CheckpointSettings:
    enabled = _env_bool("PHANTOM_CONFIRM", False)
    return CheckpointSettings(
        enabled=enabled,
        confirm_plan=_env_bool("PHANTOM_CONFIRM_PLAN", enabled),
        confirm_shell=_env_bool("PHANTOM_CONFIRM_SHELL", enabled),
        confirm_writes=_env_bool("PHANTOM_CONFIRM_WRITES", enabled),
        confirm_web=_env_bool("PHANTOM_CONFIRM_WEB", False),
        confirm_skill_changes=_env_bool("PHANTOM_CONFIRM_SKILLS", enabled),
    )


def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(
        budget=budget_settings(),
        checkpoints=checkpoint_settings(),
        secrets=secret_settings(),
        allow_shell=_env_bool("PHANTOM_ALLOW_SHELL", True),
        allow_web=_env_bool("PHANTOM_ALLOW_WEB", True),
        allow_outside_workspace=_env_bool("PHANTOM_ALLOW_OUTSIDE_WORKSPACE", False),
        allow_unsafe_skills=_env_bool("PHANTOM_ALLOW_UNSAFE_SKILLS", False),
    )


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    table = budget_settings().price_table
    pricing = table.get(model)
    if not pricing:
        return None
    input_price = pricing.get("input_per_million")
    output_price = pricing.get("output_per_million")
    if input_price is None or output_price is None:
        return None
    return (input_tokens / 1_000_000) * float(input_price) + (output_tokens / 1_000_000) * float(output_price)


def redact_text(value: str, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    text = str(value)
    redact_values = [secret for secret in (secrets or []) if secret]
    configured_secrets = runtime_settings().secrets
    for secret in (
        configured_secrets.anthropic_key,
        configured_secrets.openai_key,
        configured_secrets.groq_key,
    ):
        if secret:
            redact_values.append(secret)
    for secret in redact_values:
        text = text.replace(secret, "[REDACTED]")
    return text


def redact_payload(payload: Any, secrets: list[str] | tuple[str, ...] | None = None) -> Any:
    if isinstance(payload, dict):
        return {key: redact_payload(value, secrets=secrets) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(item, secrets=secrets) for item in payload]
    if isinstance(payload, tuple):
        return [redact_payload(item, secrets=secrets) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload, secrets=secrets)
    return payload


def prompt_user(message: str) -> bool:
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        response = input(f"{message} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return response in {"y", "yes"}


def prompt_choice(message: str, choices: dict[str, tuple[str, ...]], default: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        return default
    normalized_default = str(default or "").strip().lower()
    try:
        response = input(message).strip().lower()
    except EOFError:
        return normalized_default
    if not response:
        return normalized_default
    for canonical, aliases in choices.items():
        accepted = {canonical, *(alias.lower() for alias in aliases)}
        if response in accepted:
            return canonical
    return normalized_default


def prompt_text(message: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    try:
        return input(message).strip()
    except EOFError:
        return ""
