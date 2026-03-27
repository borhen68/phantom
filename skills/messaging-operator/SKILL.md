---
name: messaging-operator
description: "Handle Telegram and WhatsApp conversations safely through PHANTOM's messaging runtime. Use when work arrives through chat and must respect pairing, dedupe, and human-first prompts."
metadata: { "phantom": { "emoji": "💬", "category": "messaging" } }
---

# Messaging Operator

## When to Use

- The task is arriving through Telegram or WhatsApp.
- PHANTOM needs to interpret a chat message, maintain conversation scope, or reply with a task prompt.
- The workflow depends on pairing, allowlisting, dedupe, or webhook validation behavior.

## When Not to Use

- The task is local CLI work with no chat transport involved.
- The right move is direct API or browser work after the message has already been normalized.
- The user needs broad social-media automation rather than PHANTOM's current messaging surfaces.

## Workflow

- Respect conversation scope, pairing policy, and dedupe before starting any run.
- Treat greetings, empty messages, and image-only messages as prompts for intent instead of silent failures.
- Keep replies concise and task-oriented.
- Use the same runtime semantics as CLI work once the task is clear.

## References

- Use `references/pairing-policy.md` when messaging access control matters.
