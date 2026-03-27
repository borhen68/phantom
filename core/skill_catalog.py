"""Bundled repo skill/playbook catalog."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sys

from core.extensions import load_extensions

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills"
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


@dataclass(frozen=True)
class SkillRequirements:
    bins: tuple[str, ...] = ()
    any_bins: tuple[str, ...] = ()
    env: tuple[str, ...] = ()
    config: tuple[str, ...] = ()
    os: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.bins or self.any_bins or self.env or self.config or self.os)


@dataclass(frozen=True)
class SkillSupport:
    status: str
    detail: str
    missing_bins: tuple[str, ...] = ()
    missing_any_bins: tuple[str, ...] = ()
    missing_env: tuple[str, ...] = ()
    missing_config: tuple[str, ...] = ()
    unsupported_os: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundledSkill:
    name: str
    summary: str
    use_when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    guidance: tuple[str, ...]
    resources: tuple[str, ...]
    source: str
    requirements: SkillRequirements
    path: Path

    def summary_line(self) -> str:
        return f"- {self.name}: {self.summary}"

    def render(self) -> str:
        lines = [f"{self.name}: {self.summary}"]
        if self.use_when:
            lines.append("  Use when:")
            for item in self.use_when[:3]:
                lines.append(f"    - {item}")
        if self.avoid_when:
            lines.append("  Avoid when:")
            for item in self.avoid_when[:3]:
                lines.append(f"    - {item}")
        if self.guidance:
            lines.append("  Workflow:")
            for item in self.guidance[:4]:
                lines.append(f"    - {item}")
        if self.resources:
            lines.append("  Resources:")
            for item in self.resources[:3]:
                lines.append(f"    - {item}")
        lines.append(f"  Source: {self.source}")
        if not self.requirements.is_empty():
            requirement_bits = []
            if self.requirements.bins:
                requirement_bits.append(f"bins={', '.join(self.requirements.bins[:4])}")
            if self.requirements.any_bins:
                requirement_bits.append(f"anyBins={', '.join(self.requirements.any_bins[:4])}")
            if self.requirements.env:
                requirement_bits.append(f"env={', '.join(self.requirements.env[:4])}")
            if self.requirements.config:
                requirement_bits.append(f"config={', '.join(self.requirements.config[:4])}")
            if self.requirements.os:
                requirement_bits.append(f"os={', '.join(self.requirements.os[:4])}")
            lines.append(f"  Requires: {'; '.join(requirement_bits)}")
        lines.append(f"  Path: {self.path}")
        return "\n".join(lines)


def _normalize_heading(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _tokenize(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(value or "").lower()))


def _strip_wrapping_quotes(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _parse_jsonish(value: str) -> dict[str, object]:
    text = str(value or "").strip()
    if not text:
        return {}
    candidate = _TRAILING_COMMA_RE.sub(r"\1", text)
    try:
        parsed = json.loads(candidate)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        raw_block = lines[1:index]
        body = "\n".join(lines[index + 1 :])
        data: dict[str, str] = {}
        current_key = ""
        current_lines: list[str] = []
        for raw_line in raw_block:
            if not raw_line.strip():
                if current_key:
                    current_lines.append("")
                continue
            if not raw_line.startswith((" ", "\t")) and ":" in raw_line:
                if current_key:
                    data[current_key] = _strip_wrapping_quotes(" ".join(part for part in current_lines if part).strip())
                key, value = raw_line.split(":", 1)
                current_key = key.strip().lower()
                current_lines = [value.strip()]
                continue
            if current_key:
                current_lines.append(raw_line.strip())
        if current_key:
            data[current_key] = _strip_wrapping_quotes(" ".join(part for part in current_lines if part).strip())
        return data, body
    return {}, text


def _parse_skill_sections(text: str) -> tuple[str, dict[str, list[str]]]:
    title = ""
    sections: dict[str, list[str]] = {"__intro__": []}
    current = "__intro__"
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            current = _normalize_heading(line[3:])
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return title, sections


def _first_paragraph(lines: list[str]) -> str:
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(("- ", "* ")):
            continue
        paragraph.append(stripped)
    return " ".join(paragraph).strip()


def _bullet_lines(lines: list[str]) -> tuple[str, ...]:
    bullets = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        elif stripped.startswith("* "):
            bullets.append(stripped[2:].strip())
    return tuple(item for item in bullets if item)


def _fallback_name(path: Path) -> str:
    return path.parent.name.replace("-", " ").strip() or path.parent.name


def _resource_lines(skill_root: Path) -> tuple[str, ...]:
    resources = []
    for folder in ("scripts", "references", "assets"):
        directory = skill_root / folder
        if not directory.exists():
            continue
        files = [path.relative_to(skill_root) for path in sorted(directory.rglob("*")) if path.is_file()]
        if not files:
            continue
        preview = ", ".join(str(path) for path in files[:3])
        suffix = "" if len(files) <= 3 else f" (+{len(files) - 3} more)"
        resources.append(f"{folder}: {preview}{suffix}")
    return tuple(resources)


def _skill_source(path: Path) -> str:
    if "openclaw-compat" in path.parts:
        return "openclaw-compat"
    return "phantom"


def _parse_skill_requirements(frontmatter: dict[str, str]) -> SkillRequirements:
    metadata = _parse_jsonish(frontmatter.get("metadata", ""))
    openclaw = metadata.get("openclaw") if isinstance(metadata.get("openclaw"), dict) else {}
    requires = openclaw.get("requires") if isinstance(openclaw.get("requires"), dict) else {}
    return SkillRequirements(
        bins=_string_tuple(requires.get("bins")),
        any_bins=_string_tuple(requires.get("anyBins") or requires.get("any_bins")),
        env=_string_tuple(requires.get("env")),
        config=_string_tuple(requires.get("config")),
        os=_string_tuple(openclaw.get("os")),
    )


def parse_bundled_skill(path: Path) -> BundledSkill:
    raw_text = path.read_text(encoding="utf-8")
    frontmatter, text = _parse_frontmatter(raw_text)
    title, sections = _parse_skill_sections(text)
    intro = sections.get("__intro__", [])
    summary = (
        frontmatter.get("description", "")
        or frontmatter.get("summary", "")
        or frontmatter.get("purpose", "")
        or frontmatter.get("overview", "")
        or _first_paragraph(sections.get("summary", []))
        or _first_paragraph(sections.get("overview", []))
        or _first_paragraph(sections.get("purpose", []))
        or _first_paragraph(intro)
        or "Bundled PHANTOM playbook."
    )
    use_when = (
        _bullet_lines(sections.get("use when", []))
        or _bullet_lines(sections.get("when to use", []))
        or _bullet_lines(sections.get("use cases", []))
        or _bullet_lines(sections.get("triggers", []))
    )
    avoid_when = (
        _bullet_lines(sections.get("when not to use", []))
        or _bullet_lines(sections.get("avoid when", []))
        or _bullet_lines(sections.get("do not use", []))
    )
    guidance = (
        _bullet_lines(sections.get("workflow", []))
        or _bullet_lines(sections.get("guidance", []))
        or _bullet_lines(sections.get("playbook", []))
        or _bullet_lines(sections.get("steps", []))
        or _bullet_lines(sections.get("instructions", []))
    )
    return BundledSkill(
        name=frontmatter.get("name", "").strip() or title.strip() or _fallback_name(path),
        summary=summary,
        use_when=use_when,
        avoid_when=avoid_when,
        guidance=guidance,
        resources=_resource_lines(path.parent),
        source=_skill_source(path),
        requirements=_parse_skill_requirements(frontmatter),
        path=path,
    )


def load_bundled_skills() -> list[BundledSkill]:
    if not SKILL_DIR.exists():
        return []
    skills = []
    for path in sorted(SKILL_DIR.rglob("SKILL.md")):
        try:
            skills.append(parse_bundled_skill(path))
        except Exception:
            continue
    return skills


def bundled_skill_summary(limit: int = 8) -> str:
    skills = load_bundled_skills()
    if not skills:
        return "none"
    return "\n".join(skill.summary_line() for skill in skills[:limit])


def available_extension_capabilities() -> set[str]:
    capabilities: set[str] = {"shell", "cli-exec", "env-secrets"}
    for extension in load_extensions():
        if extension.enabled_by_default:
            capabilities.update(extension.capabilities)
            capabilities.add(extension.extension_id)
    return capabilities


def _config_requirement_to_capability(config_path: str) -> str:
    path = str(config_path or "").strip().lower()
    aliases = {
        "channels.telegram": "telegram",
        "channels.whatsapp": "whatsapp",
        "channels.slack": "slack",
        "channels.discord": "discord",
        "channels.bluebubbles": "bluebubbles",
        "plugins.entries.voice-call.enabled": "voice-call",
    }
    for prefix, capability in aliases.items():
        if path == prefix or path.startswith(prefix + "."):
            return capability
    return path.split(".")[-1] if path else ""


def _capability_env_candidates(capability: str) -> tuple[str, ...]:
    value = str(capability or "").strip().lower()
    mapping = {
        "slack": ("PHANTOM_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"),
        "discord": ("PHANTOM_DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"),
    }
    return mapping.get(value, ())


def _support_rank(status: str) -> int:
    return {
        "native": 3,
        "shell-compatible": 2,
        "blocked": 1,
        "unsupported": 0,
    }.get(status, 0)


def assess_skill_support(skill: BundledSkill) -> SkillSupport:
    if skill.source == "phantom":
        return SkillSupport("native", "PHANTOM-native playbook.")

    current_os = sys.platform.lower()
    unsupported_os = tuple(
        item
        for item in skill.requirements.os
        if item and item.lower() not in current_os
    )
    capabilities = available_extension_capabilities()
    missing_config = tuple(
        item
        for item in skill.requirements.config
        if _config_requirement_to_capability(item) not in capabilities
    )
    implied_env = []
    for item in skill.requirements.config:
        capability = _config_requirement_to_capability(item)
        candidates = _capability_env_candidates(capability)
        if candidates and not any(os.environ.get(name) for name in candidates):
            implied_env.append(candidates[0])
    missing_bins = tuple(item for item in skill.requirements.bins if not shutil.which(item))
    missing_env = tuple(dict.fromkeys(
        [item for item in skill.requirements.env if not os.environ.get(item)] + implied_env
    ))
    missing_any_bins = ()
    if skill.requirements.any_bins and not any(shutil.which(item) for item in skill.requirements.any_bins):
        missing_any_bins = skill.requirements.any_bins

    if unsupported_os:
        return SkillSupport(
            "unsupported",
            f"Requires OS support not available here: {', '.join(unsupported_os)}.",
            unsupported_os=unsupported_os,
        )
    if missing_config:
        return SkillSupport(
            "unsupported",
            f"Requires runtime surfaces PHANTOM does not provide yet: {', '.join(missing_config)}.",
            missing_config=missing_config,
        )
    if missing_bins or missing_env or missing_any_bins:
        missing_parts = []
        if missing_bins:
            missing_parts.append(f"install bins: {', '.join(missing_bins)}")
        if missing_any_bins:
            missing_parts.append(f"install one of: {', '.join(missing_any_bins)}")
        if missing_env:
            missing_parts.append(f"set env: {', '.join(missing_env)}")
        return SkillSupport(
            "blocked",
            "; ".join(missing_parts),
            missing_bins=missing_bins,
            missing_any_bins=missing_any_bins,
            missing_env=missing_env,
        )
    return SkillSupport(
        "shell-compatible",
        "Runnable through PHANTOM's shell/runtime compatibility path.",
    )


def skill_support_report() -> dict[str, object]:
    counts = {"native": 0, "shell-compatible": 0, "blocked": 0, "unsupported": 0}
    missing_bins: dict[str, int] = {}
    missing_env: dict[str, int] = {}
    missing_config: dict[str, int] = {}
    for skill in load_bundled_skills():
        support = assess_skill_support(skill)
        counts[support.status] = counts.get(support.status, 0) + 1
        for item in support.missing_bins:
            missing_bins[item] = missing_bins.get(item, 0) + 1
        for item in support.missing_any_bins:
            missing_bins[item] = missing_bins.get(item, 0) + 1
        for item in support.missing_env:
            missing_env[item] = missing_env.get(item, 0) + 1
        for item in support.missing_config:
            missing_config[item] = missing_config.get(item, 0) + 1
    return {
        "counts": counts,
        "missing_bins": tuple(sorted(missing_bins.items(), key=lambda row: (-row[1], row[0]))[:5]),
        "missing_env": tuple(sorted(missing_env.items(), key=lambda row: (-row[1], row[0]))[:5]),
        "missing_config": tuple(sorted(missing_config.items(), key=lambda row: (-row[1], row[0]))[:5]),
    }


def match_bundled_skills(query: str, limit: int = 3) -> list[BundledSkill]:
    skills = load_bundled_skills()
    if not skills:
        return []
    tokens = _tokenize(query)
    if not tokens:
        return skills[:limit]
    scored: list[tuple[int, int, str, BundledSkill]] = []
    for skill in skills:
        haystack = " ".join(
            [
                skill.name,
                skill.summary,
                *skill.use_when,
                *skill.avoid_when,
                *skill.guidance,
                *skill.resources,
            ]
        )
        hay_tokens = _tokenize(haystack)
        score = len(tokens & hay_tokens)
        if score:
            support = assess_skill_support(skill)
            scored.append((score, _support_rank(support.status), skill.name.lower(), skill))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [skill for _, _, _, skill in scored[:limit]]


def bundled_skill_context(query: str, limit: int = 2) -> str:
    matches = match_bundled_skills(query, limit=limit)
    if not matches:
        return ""
    lines = ["BUNDLED PLAYBOOK GUIDANCE:"]
    for skill in matches:
        lines.append(skill.render())
    return "\n".join(lines)
