# PHANTOM

[![CI](https://github.com/borhen68/phantom/actions/workflows/ci.yml/badge.svg)](https://github.com/borhen68/phantom/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Teachable AI agent for controlled delegation. PHANTOM plans first, learns your workflows, and executes with human approval when it matters.

PHANTOM is built for people who need to delegate real work without surrendering control:

- ask-first CLI and chat experience instead of surprise execution
- plan approval before action
- persistent workflow learning from human demonstrations
- chief-of-staff memory that can ingest raw work signals into people, projects, and commitments
- parallel task orchestration with replanning
- auditable traces, replay, and rollback-friendly execution
- live activity UI plus a persistent gateway with first-class sessions and diagnostics
- extension registry, bundled playbook catalog, and OpenClaw-compatible imported skills
- browser/operator sessions with live attach, resumed-state verification, and recovery
- Telegram and WhatsApp entry points to the same runtime
- Groq / OpenAI-compatible / Anthropic provider support

Engineering reference: [`docs/ENGINEERING_REFERENCE.md`](docs/ENGINEERING_REFERENCE.md)
Contributing guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
Security policy: [`SECURITY.md`](SECURITY.md)

## Why PHANTOM

Most agent projects optimize for one of two things:

- instant autonomy with little control
- polished hosted UX with limited ownership
- large coordination frameworks that are hard to reason about

PHANTOM is aimed at a different category:

`controlled delegation for consequential work`

Use PHANTOM when the task matters enough that you want:

- a clear plan before action
- approval points for risky steps
- memory that improves from your workflows over time
- traces you can inspect when something goes wrong
- autonomy that compounds instead of surprising you

The core ideas behind PHANTOM are:

- `controlled delegation`: delegate real work without losing understanding
- `workflow learning`: teach the system once and let future runs improve
- `structured skepticism`: challenge weak reasoning before bad actions cascade
- `auditable execution`: plan, checkpoint, trace, replay, and recover

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

This opens PHANTOM chat. You can type a task directly or choose memory, briefing, demos, signals, skills, and evals from the interactive hub.

Guided setup:

```bash
.venv/bin/python phantom.py --onboard
```

Extension registry:

```bash
.venv/bin/python phantom.py --extensions
```

Bundled playbook catalog:

```bash
.venv/bin/python phantom.py --skills
```

PHANTOM now ships both:

- native PHANTOM playbooks for controlled delegation, workflow learning, messaging, and chief-of-staff work
- an imported OpenClaw compatibility catalog so we can reuse a much broader skill surface while we keep building PHANTOM-native tooling
- support-status tracking on imported skills so PHANTOM can tell you which ones are `native`, `shell-compatible`, `blocked`, or still `unsupported`

Compatibility runtimes now include first-class tools for:

- `github_cli` for structured `gh` operations
- `tmux_session` for structured tmux session control
- `slack_channel` for structured Slack channel operations
- `discord_channel` for structured Discord channel operations
- `browser_session` plus session-aware `browser_workflow` for persistent browser state, resume/attach-style operator flows, resumed-session verification, and auto re-anchoring

Direct task mode:

```bash
.venv/bin/python phantom.py "analyze the current workspace and summarize the main modules"
```

Doctor:

```bash
.venv/bin/python phantom.py --doctor
```

Persistent gateway:

```bash
.venv/bin/python phantom.py --serve-gateway --gateway-port 8787
```

Plan approval mode:

```bash
.venv/bin/python phantom.py --approve-plan "analyze the current workspace and summarize the main modules"
```

Live activity page:

```bash
.venv/bin/python phantom.py --live-ui --approve-plan "analyze the current workspace and summarize the main modules"
```

## First Commands

```bash
# Ask what you want to do
.venv/bin/python phantom.py

# Run a concrete task
.venv/bin/python phantom.py "review the current workspace and explain the architecture"

# Show the plan before PHANTOM acts
.venv/bin/python phantom.py --approve-plan "audit this repository and summarize risks"

# Watch a live dashboard while PHANTOM runs
.venv/bin/python phantom.py --live-ui --approve-plan "review this repository and explain the architecture"

# Require approval for the plan and risky tool actions
.venv/bin/python phantom.py --confirm "refactor this repository"

# Run offline evals
.venv/bin/python phantom.py --evals

# Check runtime setup and missing dependencies
.venv/bin/python phantom.py --doctor

# Inspect memory
.venv/bin/python phantom.py --memory

# Ingest a raw message, meeting note, or document summary into chief-of-staff memory
.venv/bin/python phantom.py --ingest-signal "We will send the launch summary before Friday." --signal-kind message --signal-source telegram --signal-title "Nadia follow-up" --signal-metadata '{"people":[{"name":"Nadia","relationship":"manager"}],"project":{"name":"Launch","status":"active"},"counterparty":"Nadia","due_at":"Friday"}'

# Inspect generated skills
.venv/bin/python phantom.py --skills
```

## What It Can Do

- plan a goal into dependency-aware tasks
- execute tasks with shell, file, memory, web, and browser tools
- critique its own reasoning before bad steps cascade
- replan when tasks fail or get blocked
- persist memory across runs
- learn from human demonstrations and surface matching procedures
- use a structured bundled playbook catalog with frontmatter, references, and PHANTOM-native workflow guidance
- load a broad imported OpenClaw-compatible skill catalog with runtime support classification
- ingest raw work signals into chief-of-staff memory and extract people, projects, and commitments
- stream a live activity page showing the current agent, task graph, tool calls, and run timeline
- expose a persistent HTTP gateway with session history, health, and doctor endpoints
- replay bounded browser workflows through Playwright with persistent sessions, live browser attach, resumed-state verification, drift reports, and auto re-anchoring
- expose the same runtime through Telegram and WhatsApp

PHANTOM also takes a fast lane for tiny local tasks. For example, a one-file workspace architecture review now avoids the full planner/executor/critic loop and can complete in about a second instead of burning dozens of model calls.

## When To Reach For PHANTOM

PHANTOM is strongest when:

- the task has real consequences if done wrong
- the workflow repeats often enough to benefit from learning
- you want the agent to ask, explain, and remember
- you care about what happened during the run, not just the final answer

## Human Control

PHANTOM is designed to work with humans, not around them.

- `python phantom.py` with no goal asks what you want first
- `--approve-plan` shows the plan and waits for approval
- `--confirm` requires approval for the plan and risky tool actions
- messaging users get a prompt instead of silent failure on greetings or empty/image-only messages
- messaging DMs default to pairing, so unknown senders cannot trigger runs until you approve them

This gives you a cleaner progression:

1. human provides intent
2. PHANTOM proposes a plan
3. human approves
4. PHANTOM executes autonomously

## Teach Mode

PHANTOM can learn repeated workflows from humans, match them back to future tasks, and reuse the executable parts.

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

## Browser Operator Mode

PHANTOM now has a more serious browser/operator lane instead of treating every browser run as stateless automation.

- persistent browser sessions with saved storage state
- live attach to an existing Chrome / Chromium debugging endpoint
- resumed-session verification before PHANTOM continues acting
- drift reports that show expected vs current page state
- automatic re-anchoring back to the last known good page when recovery is safe
- fallback selectors and fallback verification selectors for brittle UI steps

This means PHANTOM can now do more than replay browser steps. It can resume, verify, recover, and stop cleanly when the UI changed too much.

## Messaging

PHANTOM can run behind Telegram and WhatsApp webhooks.

```bash
.venv/bin/python phantom.py --serve-messaging --messaging-port 8080
```

Messaging behavior:

- concrete text task -> run it
- `/start`, `/help`, `hi`, `hello` -> ask what the user wants
- empty or image-only message -> ask for a concrete text task or image caption
- unknown sender -> receive a pairing code instead of triggering a run

Pairing commands:

```bash
.venv/bin/python phantom.py --pairings
.venv/bin/python phantom.py --approve-pairing telegram ABC123
.venv/bin/python phantom.py --allowlist
```

If you want public inbound DMs instead, opt in explicitly:

```bash
export PHANTOM_MESSAGING_DM_POLICY=open
```

## Gateway

PHANTOM can also run as a lightweight control plane with first-class sessions.

```bash
.venv/bin/python phantom.py --serve-gateway --gateway-host 127.0.0.1 --gateway-port 8787
```

Gateway endpoints:

- `POST /sessions` to start a run with a goal and optional workspace/scope override
- `GET /sessions` to list recent sessions
- `GET /sessions/<id>` to inspect a session snapshot
- `GET /sessions/<id>/events` to stream session events
- `GET /doctor` to inspect runtime health
- `GET /healthz` for a simple health check

## Extensions

PHANTOM now has a manifest-based extension registry under `extensions/`.

Current built-in manifests cover:

- browser operator capabilities
- chief-of-staff memory capabilities
- messaging ingress and pairing
- GitHub CLI runtime support
- tmux runtime support
- Slack runtime support
- Discord runtime support

Use:

```bash
.venv/bin/python phantom.py --extensions
```

This is the foundation for a stricter future extension SDK so new connectors and operator features do not have to be hardcoded into core.

## Chief-Of-Staff Signal Ingestion

PHANTOM can store raw work signals and turn them into structured memory.

```bash
.venv/bin/python phantom.py --ingest-signal "We will send the launch summary before Friday." \
  --signal-kind message \
  --signal-source telegram \
  --signal-title "Nadia follow-up" \
  --signal-metadata '{"people":[{"name":"Nadia","relationship":"manager"}],"project":{"name":"Launch","status":"active"},"counterparty":"Nadia","due_at":"Friday"}'

.venv/bin/python phantom.py --signals
.venv/bin/python phantom.py --brief "launch summary for Nadia"
```

This is the ingestion foundation for future email, calendar, docs, and chat connectors.

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
extensions/           extension manifests and runtime capability registry
skills/               PHANTOM-native playbooks and imported compatibility skills
```

## Development

```bash
python3 -m unittest discover -s tests -v
.venv/bin/python phantom.py --evals
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local workflow details.

## License

MIT. See [`LICENSE`](LICENSE).
