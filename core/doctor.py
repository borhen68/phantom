"""Runtime diagnostics for PHANTOM."""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions import extension_load_report
from core.skill_catalog import skill_support_report
from core.settings import data_root, runtime_settings, scope_id, skill_root, trace_root, workspace_root


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _status_rank(status: str) -> int:
    return {"fail": 2, "warn": 1, "pass": 0}.get(status, 1)


def _workspace_check() -> DoctorCheck:
    root = workspace_root()
    if not root.exists():
        return DoctorCheck("workspace", "fail", f"Workspace does not exist: {root}")
    if not root.is_dir():
        return DoctorCheck("workspace", "fail", f"Workspace is not a directory: {root}")
    entries = sorted(item.name for item in root.iterdir())
    preview = ", ".join(entries[:5]) if entries else "(empty)"
    return DoctorCheck("workspace", "pass", f"{root} · entries: {preview}")


def _home_check() -> DoctorCheck:
    root = data_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        skill_root()
        trace_root()
    except OSError as exc:
        return DoctorCheck("home", "fail", f"Cannot initialize PHANTOM home at {root}: {exc}")
    return DoctorCheck("home", "pass", f"{root} is writable")


def _provider_check() -> DoctorCheck:
    settings = runtime_settings()
    providers = []
    if settings.secrets.anthropic_key:
        providers.append("anthropic")
    if settings.secrets.openai_key:
        providers.append("openai")
    if settings.secrets.groq_key:
        providers.append("groq")
    if not providers:
        return DoctorCheck(
            "providers",
            "warn",
            "No provider key configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GROQ_API_KEY.",
        )
    chain = os.environ.get("PHANTOM_PROVIDER_CHAIN") or os.environ.get("PHANTOM_PROVIDER") or ",".join(providers)
    return DoctorCheck("providers", "pass", f"configured={', '.join(providers)} · chain={chain}")


def _sandbox_check() -> DoctorCheck:
    available = [name for name in ("bwrap", "bubblewrap", "nsjail", "unshare") if shutil.which(name)]
    if not available:
        return DoctorCheck("sandbox", "warn", "No external sandbox tool found; PHANTOM will fall back to restricted in-host execution.")
    return DoctorCheck("sandbox", "pass", f"available tools: {', '.join(available)}")


def _browser_check() -> DoctorCheck:
    playwright_installed = importlib.util.find_spec("playwright") is not None
    playwright_cli = shutil.which("playwright")
    if playwright_installed or playwright_cli:
        detail = "Playwright runtime available"
        if playwright_cli:
            detail += f" · cli={playwright_cli}"
        return DoctorCheck("browser", "pass", detail)
    return DoctorCheck("browser", "warn", "Playwright is not installed; browser/operator flows will be unavailable.")


def _messaging_check() -> DoctorCheck:
    configured = []
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        configured.append("telegram")
    if os.environ.get("WHATSAPP_ACCESS_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
        configured.append("whatsapp")
    if configured:
        from integrations.messaging import messaging_access_report

        report = messaging_access_report()
        configured_detail = ", ".join(
            f"{item['platform']}({item['policy']})" for item in report["configured"]
        ) or ", ".join(configured)
        if report["risky_platforms"]:
            risky = ", ".join(report["risky_platforms"])
            return DoctorCheck(
                "messaging",
                "warn",
                f"configured channels: {configured_detail} · open DM policy on {risky}",
            )
        return DoctorCheck(
            "messaging",
            "pass",
            f"configured channels: {configured_detail} · approved={report['counts']['approved']} pending={report['counts']['pending']}",
        )
    return DoctorCheck("messaging", "warn", "No messaging channel tokens configured.")


def _project_shape_check() -> DoctorCheck:
    root = workspace_root()
    has_git = (root / ".git").exists()
    py_files = list(root.rglob("*.py")) if root.exists() and root.is_dir() else []
    if has_git:
        return DoctorCheck("project-shape", "pass", f"git repo detected · python_files={len(py_files)}")
    if py_files:
        return DoctorCheck("project-shape", "pass", f"plain workspace · python_files={len(py_files)}")
    return DoctorCheck("project-shape", "warn", "No Python files found in workspace.")


def _extensions_check() -> DoctorCheck:
    report = extension_load_report()
    if report["errors"]:
        return DoctorCheck(
            "extensions",
            "warn",
            f"loaded={report['count']} invalid={len(report['errors'])}",
        )
    if report["count"] == 0:
        return DoctorCheck("extensions", "warn", "No extension manifests discovered.")
    return DoctorCheck("extensions", "pass", f"loaded={report['count']} extension manifests")


def _skill_compatibility_check() -> DoctorCheck:
    report = skill_support_report()
    counts = report["counts"]
    detail = (
        f"native={counts['native']} compatible={counts['shell-compatible']} "
        f"blocked={counts['blocked']} unsupported={counts['unsupported']}"
    )
    if counts["unsupported"]:
        top = ", ".join(name for name, _ in report["missing_config"])
        if top:
            detail += f" · missing runtime surfaces: {top}"
        return DoctorCheck("skill-compat", "warn", detail)
    if counts["blocked"]:
        top_bins = ", ".join(name for name, _ in report["missing_bins"])
        top_env = ", ".join(name for name, _ in report["missing_env"])
        extras = []
        if top_bins:
            extras.append(f"bins: {top_bins}")
        if top_env:
            extras.append(f"env: {top_env}")
        if extras:
            detail += " · " + " · ".join(extras)
        return DoctorCheck("skill-compat", "warn", detail)
    return DoctorCheck("skill-compat", "pass", detail)


def doctor_report() -> dict[str, Any]:
    checks = [
        _workspace_check(),
        _home_check(),
        _provider_check(),
        _sandbox_check(),
        _browser_check(),
        _messaging_check(),
        _extensions_check(),
        _skill_compatibility_check(),
        _project_shape_check(),
    ]
    worst = max((_status_rank(item.status) for item in checks), default=0)
    status = {0: "pass", 1: "warn", 2: "fail"}[worst]
    return {
        "status": status,
        "scope": scope_id(),
        "workspace": str(workspace_root()),
        "home": str(data_root()),
        "checks": [item.as_dict() for item in checks],
    }
