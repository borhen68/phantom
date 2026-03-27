# OpenClaw Compatibility Skills

This directory preserves the imported OpenClaw skill catalog inside PHANTOM.

Why it exists:

- keep the full skill breadth available before deleting the original OpenClaw checkout
- let PHANTOM match against a much larger catalog immediately
- separate imported compatibility skills from PHANTOM-native playbooks

Important:

- these skills were authored for OpenClaw, not PHANTOM
- many are still useful as prompt-time guidance
- some refer to tools or runtime surfaces PHANTOM does not fully support yet

The loader tags these entries as `openclaw-compat` so the CLI and planner can distinguish
them from PHANTOM-native skills.
