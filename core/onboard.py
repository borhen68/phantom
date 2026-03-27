"""Interactive onboarding helpers for PHANTOM."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OnboardConfig:
    workspace: str
    provider: str
    confirm_plan: bool
    messaging_policy: str


def provider_env_lines(provider: str) -> list[str]:
    normalized = str(provider or "").strip().lower()
    if normalized == "groq":
        return [
            'export GROQ_API_KEY="replace_me"',
            'export PHANTOM_PROVIDER=groq',
            'export PHANTOM_PROVIDER_CHAIN=groq',
            'export PHANTOM_OPENAI_BASE_URL="https://api.groq.com/openai/v1"',
            'export PHANTOM_PLANNING_MODEL="openai/gpt-oss-20b"',
            'export PHANTOM_EXECUTION_MODEL="openai/gpt-oss-120b"',
            'export PHANTOM_CRITIC_MODEL="openai/gpt-oss-20b"',
            'export PHANTOM_SYNTHESIS_MODEL="openai/gpt-oss-20b"',
        ]
    if normalized == "openai":
        return [
            'export OPENAI_API_KEY="replace_me"',
            'export PHANTOM_PROVIDER=openai',
            'export PHANTOM_PROVIDER_CHAIN=openai',
        ]
    if normalized == "anthropic":
        return [
            'export ANTHROPIC_API_KEY="replace_me"',
            'export PHANTOM_PROVIDER=anthropic',
            'export PHANTOM_PROVIDER_CHAIN=anthropic',
        ]
    return []


def onboard_env_lines(config: OnboardConfig) -> list[str]:
    lines = [
        'export PHANTOM_HOME="$PWD/.phantom"',
        f'export PHANTOM_WORKSPACE="{config.workspace}"',
        f'export PHANTOM_CONFIRM_PLAN={"1" if config.confirm_plan else "0"}',
        f'export PHANTOM_MESSAGING_DM_POLICY={config.messaging_policy}',
    ]
    lines.extend(provider_env_lines(config.provider))
    return lines


def onboard_env_text(config: OnboardConfig) -> str:
    return "\n".join(onboard_env_lines(config)) + "\n"


def write_onboard_env(path: str | Path, config: OnboardConfig) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(onboard_env_text(config), encoding="utf-8")
    return target
