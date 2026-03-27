"""Manifest-based extension registry for PHANTOM."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "extensions"
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class ExtensionManifest:
    extension_id: str
    title: str
    description: str
    version: str
    capabilities: tuple[str, ...]
    enabled_by_default: bool
    config_schema: dict[str, Any]
    path: Path

    def summary_line(self) -> str:
        capability_text = ", ".join(self.capabilities[:4]) if self.capabilities else "no declared capabilities"
        return f"- {self.extension_id}: {self.description or self.title} ({capability_text})"

    def render(self) -> str:
        lines = [f"{self.extension_id}: {self.title or self.extension_id}"]
        if self.description:
            lines.append(f"  Description: {self.description}")
        if self.capabilities:
            lines.append(f"  Capabilities: {', '.join(self.capabilities)}")
        lines.append(f"  Enabled by default: {'yes' if self.enabled_by_default else 'no'}")
        lines.append(f"  Path: {self.path}")
        return "\n".join(lines)


def _tokenize(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(value or "").lower()))


def extension_manifest_paths() -> list[Path]:
    if not EXTENSIONS_DIR.exists():
        return []
    return sorted(EXTENSIONS_DIR.glob("*/phantom.plugin.json"))


def parse_extension_manifest(path: Path) -> ExtensionManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid manifest at {path}: root must be a JSON object.")
    extension_id = str(payload.get("id") or path.parent.name).strip()
    if not extension_id:
        raise ValueError(f"Invalid manifest at {path}: missing extension id.")
    capabilities_raw = payload.get("capabilities") or payload.get("provides") or []
    if isinstance(capabilities_raw, dict):
        capabilities = tuple(sorted(str(item).strip() for item in capabilities_raw.keys() if str(item).strip()))
    elif isinstance(capabilities_raw, list):
        capabilities = tuple(sorted(str(item).strip() for item in capabilities_raw if str(item).strip()))
    else:
        capabilities = ()
    return ExtensionManifest(
        extension_id=extension_id,
        title=str(payload.get("title") or payload.get("name") or extension_id).strip(),
        description=str(payload.get("description") or "").strip(),
        version=str(payload.get("version") or "0.1.0").strip() or "0.1.0",
        capabilities=capabilities,
        enabled_by_default=bool(payload.get("enabledByDefault", True)),
        config_schema=payload.get("configSchema") if isinstance(payload.get("configSchema"), dict) else {},
        path=path,
    )


def load_extensions() -> list[ExtensionManifest]:
    manifests: list[ExtensionManifest] = []
    for path in extension_manifest_paths():
        try:
            manifests.append(parse_extension_manifest(path))
        except Exception:
            continue
    return manifests


def extension_load_report() -> dict[str, Any]:
    loaded: list[ExtensionManifest] = []
    errors: list[dict[str, str]] = []
    for path in extension_manifest_paths():
        try:
            loaded.append(parse_extension_manifest(path))
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "loaded": loaded,
        "errors": errors,
        "count": len(loaded),
    }


def extension_summary(limit: int = 8) -> str:
    manifests = load_extensions()
    if not manifests:
        return "none"
    return "\n".join(item.summary_line() for item in manifests[:limit])


def match_extensions(query: str, limit: int = 3) -> list[ExtensionManifest]:
    manifests = load_extensions()
    if not manifests:
        return []
    tokens = _tokenize(query)
    if not tokens:
        return manifests[:limit]
    scored: list[tuple[int, str, ExtensionManifest]] = []
    for item in manifests:
        haystack = " ".join([item.extension_id, item.title, item.description, *item.capabilities])
        score = len(tokens & _tokenize(haystack))
        if score:
            scored.append((score, item.extension_id, item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in scored[:limit]]


def extension_context(query: str, limit: int = 2) -> str:
    matches = match_extensions(query, limit=limit)
    if not matches:
        return ""
    lines = ["RELEVANT EXTENSIONS:"]
    for item in matches:
        lines.append(item.render())
    return "\n".join(lines)
