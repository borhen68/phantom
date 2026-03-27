---
name: signal-ingestion
description: "Turn raw messages, meeting notes, and documents into chief-of-staff memory. Use when PHANTOM should extract people, projects, commitments, deadlines, or counterparties from incoming signals."
metadata: { "phantom": { "emoji": "📥", "category": "memory" } }
---

# Signal Ingestion

## When to Use

- A new message, meeting note, or document summary should update PHANTOM's working memory.
- The user wants PHANTOM to infer commitments, projects, people, or deadlines from raw text.
- The next step depends on turning unstructured signals into durable context.

## When Not to Use

- The user only wants a one-time summary and does not want memory updated.
- The source text is too ambiguous to store reliably without confirmation.
- The signal contains sensitive information that should not be persisted without approval.

## Workflow

- Store the raw signal with source, kind, title, and metadata first.
- Extract people, projects, commitments, deadlines, and counterparties conservatively.
- Prefer explicit commitments over speculative interpretation.
- Feed the extracted context into briefings and future task planning.

## References

- Use `references/extraction-guidelines.md` when the signal is ambiguous.
