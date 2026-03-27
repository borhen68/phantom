# OpenClaw Engineering Notes For PHANTOM

This note captures the engineering patterns worth preserving from the local
OpenClaw checkout at `openclaw-main/` before removing it.

The goal is not to clone OpenClaw feature-for-feature. The goal is to keep the
best engineering logic and apply it in a PHANTOM-native way.

## What OpenClaw Does Exceptionally Well

### 1. Product Surfaces Are Backed By Real Control Planes

OpenClaw is not just a CLI with agent prompts. It has:

- a gateway/control plane
- first-class sessions
- onboarding
- doctor/diagnostics
- browser/operator surfaces
- plugin manifests and plugin SDK boundaries

This is why the product feels operational rather than experimental.

### 2. Extension Boundaries Are Enforced, Not Just Documented

Representative files:

- `openclaw-main/scripts/check-extension-plugin-sdk-boundary.mjs`
- `openclaw-main/scripts/check-no-extension-src-imports.ts`
- `openclaw-main/scripts/check-plugin-sdk-exports.mjs`

Engineering lesson:

- keep extension code behind a stable SDK
- prevent plugin code from importing arbitrary core internals
- enforce boundaries in CI with static checks

### 3. Onboarding Is Treated As Core Engineering

Representative references:

- `openclaw-main/README.md`
- onboarding/wizard docs in `openclaw-main/docs`

Engineering lesson:

- setup friction is an engineering problem, not just documentation
- strong systems still feel broken if users cannot reach a known-good config quickly

### 4. Security Defaults Are Protective

Representative references:

- DM pairing in `openclaw-main/README.md`
- pairing helpers under `openclaw-main/src/channels/plugins`

Engineering lesson:

- public messaging and remote control paths should default to restricted access
- approval and allowlist flows must be designed into the runtime, not bolted on later

### 5. Browser Control Is Treated As A First-Class Runtime

Representative references:

- `openclaw-main/docs/tools/browser.md`
- `openclaw-main/docs/tools/browser-login.md`
- `openclaw-main/src/plugin-sdk/browser-runtime.ts`

Engineering lesson:

- browser automation should have session/profile management, snapshots, action semantics, troubleshooting, and login guidance
- “browser access” is a product subsystem, not a single tool call

### 6. Skills Are A Product Layer, Not Just Generated Code

Representative references:

- `openclaw-main/skills/`
- `openclaw-main/README.md` skills sections

Engineering lesson:

- a bundled skill/playbook catalog helps the system start useful before it learns anything
- runtime-created skills and repo-shipped skills should coexist

### 7. Ops Tooling Is Part Of The Architecture

Representative surface:

- `openclaw-main/scripts/`

The repo includes many checks and helper scripts for:

- boundary enforcement
- security invariants
- startup/gateway smokes
- browser/sandbox setup
- release and packaging workflows

Engineering lesson:

- mature systems codify architecture rules in scripts instead of relying on memory

## What PHANTOM Already Borrowed

These OpenClaw-inspired ideas are already implemented in PHANTOM:

- persistent gateway/control plane
- doctor command and runtime diagnostics
- DM pairing / allowlist for messaging
- bundled repo-level skill/playbook catalog
- onboarding command

Relevant PHANTOM files:

- `core/gateway.py`
- `core/doctor.py`
- `integrations/messaging.py`
- `core/skill_catalog.py`
- `skills/`
- `core/onboard.py`
- `phantom.py`

## Highest-Leverage Logic Still Worth Stealing

### P1. Extension System

OpenClaw has a serious plugin platform. PHANTOM should add a smaller, stricter
version:

- `extensions/<name>/phantom.plugin.json`
- stable PHANTOM SDK entry points
- manifest-based loading
- capability registration (provider, channel, connector, browser, diagnostics)

Why this matters:

- avoids hardcoding every future integration into core
- keeps chief-of-staff connectors maintainable
- lets browser/connectors grow without turning PHANTOM into a monolith

### P1. Architecture Guard Scripts

PHANTOM should adopt CI guard scripts inspired by OpenClaw’s scripts folder.

Recommended first checks:

- prevent future extensions from importing random core internals
- detect conflict markers
- enforce simple module-size limits for known hot spots
- smoke-test doctor/onboard/gateway entry points

### P1. Managed Operator Mode

PHANTOM already has browser workflow support, but not a full operator runtime.

Borrowed ideas to implement:

- persistent browser profile/session
- attach/resume flows
- snapshot + action + trace workflow
- login/MFA pause/resume
- troubleshooting UX

### P2. Workspace + Skill Distribution Model

OpenClaw separates bundled skills, managed skills, and workspace skills.
PHANTOM should move toward:

- bundled repo skills
- runtime-created executable skills
- project/workspace-local playbooks

### P2. Better Operational Scripts

PHANTOM should add scripts for:

- startup smoke tests
- gateway smoke tests
- release checks
- architecture checks

## What Not To Copy Blindly

### 1. Raw Breadth

OpenClaw’s enormous provider/channel breadth is impressive, but copying it all
would slow PHANTOM down and blur the product.

### 2. Surface Area Without A Differentiator

PHANTOM should not try to win by having more logos or more plugins. The stronger
lane is:

- controlled delegation
- workflow learning
- chief-of-staff memory
- operator execution with proof

### 3. Complexity Before Need

OpenClaw has many layers because it serves a very broad product. PHANTOM should
steal the engineering discipline, not the accidental complexity.

## Recommended Order

1. Build PHANTOM extension manifests + loader
2. Add extension boundary guard scripts
3. Build managed operator/browser mode
4. Expand bundled skill catalog
5. Add more ops scripts and release smokes

## Short CTO Summary

What OpenClaw is best at from an engineering perspective:

- platform discipline
- boundary enforcement
- onboarding and diagnostics
- browser/runtime productization
- plugin surface design

What PHANTOM should steal:

- the engineering logic behind those systems
- not the whole breadth of the product

The most important next engineering move for PHANTOM is:

`build a real extension system with strict boundaries`
