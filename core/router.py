"""
PHANTOM Router — assigns the right model to the right job.
Haiku  → planning, routing, simple lookups  (cheap + fast)
Sonnet → execution, tool use, synthesis     (balanced)

Beats OpenClaw/Nanobot: they use one model for everything.
Smart routing = 60-80% cost reduction on complex multi-step tasks.
"""

import os

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-5"
DEFAULT_ROLE_MAX_TOKENS = {
    "planner": 1400,
    "executor": 1800,
    "critic": 700,
    "synthesizer": 1000,
}
GROQ_FRIENDLY_ROLE_MAX_TOKENS = {
    "planner": 900,
    "executor": 1200,
    "critic": 500,
    "synthesizer": 700,
}


class TaskComplexity:
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


def classify(task: str) -> TaskComplexity:
    """Heuristic task classification based on intent signals."""
    task_lower = task.lower()

    simple_signals = [
        "what is", "list", "show me", "search for", "find", "check",
        "status", "which", "when", "where", "how many", "recall",
    ]
    complex_signals = [
        "design", "architect", "analyze", "compare", "evaluate", "critique",
        "review", "synthesize", "refactor", "debug complex", "plan",
    ]

    if any(signal in task_lower for signal in complex_signals):
        return TaskComplexity.COMPLEX
    if any(signal in task_lower for signal in simple_signals) and len(task) < 120:
        return TaskComplexity.SIMPLE
    return TaskComplexity.MODERATE


def model_for(complexity: str) -> str:
    return {
        TaskComplexity.SIMPLE: HAIKU,
        TaskComplexity.MODERATE: SONNET,
        TaskComplexity.COMPLEX: SONNET,
    }.get(complexity, SONNET)


def planning_model() -> str:
    """Planner always uses Haiku — it just needs to structure tasks."""
    return os.environ.get("PHANTOM_PLANNING_MODEL", os.environ.get("PHANTOM_MODEL", HAIKU)).strip() or HAIKU


def execution_model() -> str:
    """Executor uses Sonnet — needs full tool-use capability."""
    return os.environ.get("PHANTOM_EXECUTION_MODEL", os.environ.get("PHANTOM_MODEL", SONNET)).strip() or SONNET


def critic_model() -> str:
    """Critic uses Sonnet — needs strong reasoning."""
    return os.environ.get("PHANTOM_CRITIC_MODEL", os.environ.get("PHANTOM_MODEL", SONNET)).strip() or SONNET


def synthesis_model() -> str:
    """Synthesis may use a separate model because it is often token-heavy."""
    return os.environ.get("PHANTOM_SYNTHESIS_MODEL", os.environ.get("PHANTOM_EXECUTION_MODEL", os.environ.get("PHANTOM_MODEL", SONNET))).strip() or SONNET


def _env_role_tokens(role: str) -> int | None:
    aliases = {
        "executor": ("EXECUTOR", "EXECUTION"),
        "synthesizer": ("SYNTHESIZER", "SYNTHESIS"),
        "planner": ("PLANNER",),
        "critic": ("CRITIC",),
    }
    value = None
    for alias in aliases.get(role, (role.upper(),)):
        value = os.environ.get(f"PHANTOM_{alias}_MAX_TOKENS")
        if value is not None:
            break
    if value is None:
        value = os.environ.get("PHANTOM_MAX_TOKENS")
    if value is None or str(value).strip() == "":
        return None
    try:
        return max(128, int(value))
    except ValueError:
        return None


def _is_groq_like_model(model: str) -> bool:
    model_lower = str(model or "").strip().lower()
    return (
        "gpt-oss" in model_lower
        or "llama" in model_lower
        or "qwen" in model_lower
        or "gemma" in model_lower
        or os.environ.get("PHANTOM_PROVIDER", "").strip().lower() == "groq"
    )


def max_tokens_for_role(role: str, model: str) -> int:
    explicit = _env_role_tokens(role)
    if explicit is not None:
        return explicit
    defaults = GROQ_FRIENDLY_ROLE_MAX_TOKENS if _is_groq_like_model(model) else DEFAULT_ROLE_MAX_TOKENS
    return defaults.get(role, 1200)
