# Security Policy

## Scope

PHANTOM is an autonomous agent runtime with tool execution, persistent memory, and generated skills. Treat it as a sensitive local automation system.

## Reporting

If you find a security issue, please report it privately before opening a public issue.

Until a dedicated security inbox exists, use GitHub private reporting if available for the repository.

## High-Risk Areas

- generated skill execution
- shell and file tool safety boundaries
- messaging webhook authentication
- secret loading and redaction
- browser automation against privileged systems

## Current Security Posture

PHANTOM includes:

- scoped local state
- tool confirmation controls
- webhook signature verification
- provider secret redaction
- allowlist-based skill validation
- Linux sandbox preference for generated skills

PHANTOM does not yet provide full hostile-code isolation. Do not treat it as a hardened multi-tenant execution platform.
