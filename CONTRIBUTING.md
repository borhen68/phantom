# Contributing to PHANTOM

Thanks for contributing.

## Local Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

## Run Checks

```bash
python3 -m unittest discover -s tests -v
.venv/bin/python phantom.py --evals
```

## Contribution Guidelines

- keep changes small and reviewable
- preserve local-first and human-control behavior
- add or update tests for behavior changes
- do not weaken safety checks to land a feature quickly
- document new CLI flags and environment variables in `README.md`

## Areas That Need Work

- planner grounding and plan quality
- provider reliability and benchmark harnesses
- stronger generated-skill isolation
- browser workflow robustness
- better product demos and example workflows
