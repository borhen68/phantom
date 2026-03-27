# Repository Review Checklist

Use this checklist when the workspace is large enough that a quick inventory is not enough.

## Inventory

- Identify entry points such as `main`, `app`, `server`, or CLI launchers.
- Note top-level packages and operational surfaces (`core`, `api`, `integrations`, `tools`, `docs`).
- Check whether the repo is a library, app, service, automation runtime, or mixed system.

## Architecture

- Map the control plane: planning, orchestration, runtime, APIs, and user entry points.
- Map the data plane: storage, memory, traces, artifacts, or external dependencies.
- Map safety and policy boundaries: approvals, sandboxing, budgets, auth, messaging, or browser control.

## Output

- Explain the system from outside in: product surface, runtime core, memory/state, integrations.
- Call out the strongest design choices and the main engineering risks separately.
- Keep the explanation proportional to the actual codebase.
