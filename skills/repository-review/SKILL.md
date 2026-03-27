---
name: repository-review
description: "Review a local repository and explain the architecture in proportion to the actual codebase. Use when the user asks for an architecture review, repo walkthrough, module summary, or engineering overview of the current workspace."
metadata: { "phantom": { "emoji": "🧭", "category": "engineering" } }
---

# Repository Review

## When to Use

- The user asks for an architecture review, repo walkthrough, codebase explanation, or module summary.
- The workspace should be inspected locally before any remote fetch or web search.
- The codebase may be tiny, incomplete, or not a git repository.

## When Not to Use

- The task is to change code, write a patch, or fix a bug rather than explain architecture.
- The user needs line-by-line review findings on a diff; use review mode instead.
- The task depends mainly on hosted docs or external systems rather than the local workspace.

## Workflow

- Start with a local inventory and determine whether the workspace is a single-file script, a small package, or a larger system.
- Ground the explanation in actual entry points, module boundaries, data flow, integrations, and runtime surfaces.
- Avoid clone, pull, branch, or git-history steps unless the workspace proves they are necessary.
- Scale the explanation to the project size; do not inflate a tiny repo into an imaginary architecture.
- Prefer naming the real files and responsibilities over generic framework talk.

## References

- Use `references/review-checklist.md` for the walkthrough checklist when the workspace is medium or large.
