# Browser Checkpoints and Recovery

## Checkpoint Rules

- Ask for approval before destructive browser actions.
- Pause for login, MFA, CAPTCHA, or human-only approvals.
- Prefer `assert_text` or other explicit verification after form submission or state changes.

## Drift Recovery

- Name the exact step that drifted.
- Report what PHANTOM expected and what it observed instead.
- Ask for a correction once, then update the procedure or demonstration if the human provides the new path.
