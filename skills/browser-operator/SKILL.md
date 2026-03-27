---
name: browser-operator
description: "Run browser-based work with verification, checkpoints, and recovery when the UI changes. Use when a task depends on a real webpage, dashboard, form, or authenticated browser workflow."
metadata: { "phantom": { "emoji": "🌐", "category": "operator" } }
---

# Browser Operator

## When to Use

- The task depends on a real web page, dashboard, or form.
- PHANTOM must click, fill, wait, extract text, assert text, or capture screenshots.
- Human login, MFA, or explicit approval may be required in the middle of the workflow.

## When Not to Use

- The task can be solved through an API, CLI, or direct file access more reliably than browser automation.
- The user only needs information from a page, not interaction; plain retrieval may be enough.
- The page is inaccessible and no browser session or human handoff is available.

## Workflow

- Prefer stable browser actions and verification over brittle scraping or guessing.
- Verify each meaningful step with text, URL, DOM state, or screenshot evidence before continuing.
- Pause cleanly when login, MFA, destructive approval, or policy confirmation is required.
- If the page drifts from the expected state, report the exact step that changed and ask for correction.
- Reuse taught procedures when a known browser workflow matches strongly enough.

## References

- Use `references/checkpoints.md` for checkpoint and drift-recovery guidance.
