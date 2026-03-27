# PHANTOM Bundled Skills

This directory holds PHANTOM's bundled playbooks: structured, repo-owned skills that
improve planning and execution for common PHANTOM-native workflows.

Each skill lives in its own folder and follows this layout:

```text
skill-name/
├── SKILL.md
├── references/   # optional reference material loaded on demand
├── scripts/      # optional deterministic helpers
└── assets/       # optional output-time files
```

These bundled playbooks are different from runtime-created executable Python skills
under `~/.phantom/skills/<scope>/`. Bundled playbooks are part of the product itself.

Design goals:

- trigger on clear descriptions, not vague names
- stay concise in `SKILL.md`
- push detailed material into `references/` and `scripts/`
- give PHANTOM sharper workflow defaults without bloating core prompts

Current catalog areas:

- repository and architecture review
- browser/operator work
- workflow teaching and replay
- messaging operations and pairing-aware routing
- chief-of-staff briefings and signal ingestion

Catalog sources:

- native PHANTOM skills live directly under `skills/<name>/`
- imported OpenClaw compatibility skills live under `skills/openclaw-compat/<name>/`

Imported compatibility skills are intentionally kept separate so we can reuse the breadth
of the OpenClaw catalog without confusing it for PHANTOM-native runtime support.
