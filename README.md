# PHANTOM

[![CI](https://github.com/borhen68/phantom/actions/workflows/ci.yml/badge.svg)](https://github.com/borhen68/phantom/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Local-first autonomous agent runtime with planning, memory, tool use, human approval, and workflow learning.

PHANTOM is built for people who want an agent they can actually inspect and control:

- ask-first CLI mode instead of surprise execution
- plan approval before action
- parallel task orchestration with replanning
- persistent memory and reusable skills
- teach mode for repeated workflows
- Telegram and WhatsApp chat integrations
- Groq / OpenAI-compatible / Anthropic provider support

Engineering reference: [`docs/ENGINEERING_REFERENCE.md`](docs/ENGINEERING_REFERENCE.md)
Contributing guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
Security policy: [`SECURITY.md`](SECURITY.md)

## Why PHANTOM

Most agent projects are either:

- thin shells around one model call
- opaque hosted products you cannot inspect
- large frameworks that are hard to reason about

PHANTOM tries to stay in the useful middle:

- small enough to audit
- structured enough to harden
- autonomous enough to finish real work
- safe enough to keep a human in control

## Quick Start

### 1. Create a virtual environment

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

### 2. Pick a provider

Anthropic / OpenAI-compatible env vars work, and Groq runs through the OpenAI-compatible path.

Example Groq setup:

```bash
export GROQ_API_KEY="your_key_here"
export PHANTOM_PROVIDER=groq
export PHANTOM_PROVIDER_CHAIN=groq
export PHANTOM_OPENAI_BASE_URL="https://api.groq.com/openai/v1"
export PHANTOM_PLANNING_MODEL="openai/gpt-oss-20b"
export PHANTOM_EXECUTION_MODEL="openai/gpt-oss-120b"
export PHANTOM_CRITIC_MODEL="openai/gpt-oss-20b"
export PHANTOM_SYNTHESIS_MODEL="openai/gpt-oss-20b"
```

### 3. Set local runtime paths

```bash
export PHANTOM_HOME="$PWD/.phantom"
export PHANTOM_WORKSPACE="$PWD"
```

### 4. Run PHANTOM

Ask-first mode:

```bash
.venv/bin/python phantom.py
```

Direct task mode:

```bash
.venv/bin/python phantom.py "analyze the current workspace and summarize the main modules"
```

Plan approval mode:

```bash
.venv/bin/python phantom.py --approve-plan "analyze the current workspace and summarize the main modules"
```

## First Commands

```bash
# Ask what you want to do
.venv/bin/python phantom.py

# Run a concrete task
.venv/bin/python phantom.py "review the current workspace and explain the architecture"

# Show the plan before PHANTOM acts
.venv/bin/python phantom.py --approve-plan "audit this repository and summarize risks"

# Require approval for the plan and risky tool actions
.venv/bin/python phantom.py --confirm "refactor this repository"

# Run offline evals
.venv/bin/python phantom.py --evals

# Inspect memory
.venv/bin/python phantom.py --memory

# Inspect generated skills
.venv/bin/python phantom.py --skills
```

## What It Can Do

- plan a goal into dependency-aware tasks
- execute tasks with shell, file, memory, web, and browser tools
- critique its own reasoning before bad steps cascade
- replan when tasks fail or get blocked
- persist memory across runs
- learn from human demonstrations
- replay bounded browser workflows through Playwright
- expose the same runtime through Telegram and WhatsApp

## Human Control

PHANTOM is designed to work with humans, not around them.

- `python phantom.py` with no goal asks what you want first
- `--approve-plan` shows the plan and waits for approval
- `--confirm` requires approval for the plan and risky tool actions
- messaging users get a prompt instead of silent failure on greetings or empty/image-only messages

This gives you a cleaner progression:

1. human provides intent
2. PHANTOM proposes a plan
3. human approves
4. PHANTOM executes autonomously

## Teach Mode

PHANTOM can learn repeated workflows from humans.

```bash
.venv/bin/python phantom.py --teach "check dashboard health" \
  --teach-summary "human showed the dashboard status flow" \
  --teach-browser-goto https://example.com/dashboard \
  --teach-browser-click "#status-tab" \
  --teach-browser-extract "h1::dashboard_heading"
```

Then:

```bash
.venv/bin/python phantom.py --match-demonstrations "check dashboard health"
.venv/bin/python phantom.py --replay-demonstration 1
.venv/bin/python phantom.py --replay-demonstration 1 --execute-demonstration
```

## Messaging

PHANTOM can run behind Telegram and WhatsApp webhooks.

```bash
.venv/bin/python phantom.py --serve-messaging --messaging-port 8080
```

Messaging behavior:

- concrete text task -> run it
- `/start`, `/help`, `hi`, `hello` -> ask what the user wants
- empty or image-only message -> ask for a concrete text task or image caption

## Safety and Engineering Posture

PHANTOM is still a prototype system, but it is a hardened one.

- typed contracts
- scoped memory
- persistent traces
- offline eval suite
- provider fallback
- plan approval
- checkpointed tool actions
- allowlist-based skill validation
- Linux sandbox preference order for generated skills:
  - `bubblewrap`
  - `nsjail`
  - `unshare --net`
  - plain `python -I` fallback

Current limitation:

- generated skills are safer than before, but this is still not full hostile-code isolation

## Benchmarking

If you want to prove PHANTOM, benchmark it on:

- SWE-bench Lite / Verified for coding
- WebArena-Verified for browser workflows
- GAIA for general tool use

PHANTOM already includes deterministic offline evals:

```bash
.venv/bin/python phantom.py --evals
```

## Project Structure

```text
phantom.py            CLI entry point
phantom_cli.py        installable console entrypoint
core/                 orchestration, routing, providers, contracts
memory/               persistent memory and skill storage
tools/                tool dispatch, safety, browser runtime, skill runner
integrations/         Telegram and WhatsApp webhook runtime
evals/                deterministic engineering evals
tests/                regression suite
docs/                 engineering reference
```

## Development

```bash
python3 -m unittest discover -s tests -v
.venv/bin/python phantom.py --evals
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local workflow details.

## License

MIT. See [`LICENSE`](LICENSE).
