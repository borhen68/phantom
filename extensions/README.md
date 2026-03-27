# PHANTOM Extensions

This directory is the start of PHANTOM's manifest-based extension system.

Each extension lives in its own folder and declares a `phantom.plugin.json`
manifest. The manifest is used for:

- discovery
- capability reporting
- planning/execution context
- future extension loading boundaries

This is intentionally smaller than OpenClaw's plugin platform. The goal is to
give PHANTOM a clean growth path without hardcoding every future connector and
operator feature into core.
