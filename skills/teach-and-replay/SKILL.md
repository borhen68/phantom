---
name: teach-and-replay
description: "Capture a human workflow, structure it into reusable steps, and replay the safe parts later. Use when the user wants PHANTOM to learn a repeated process instead of improvising from scratch every time."
metadata: { "phantom": { "emoji": "🎓", "category": "workflow-learning" } }
---

# Teach and Replay

## When to Use

- The user is demonstrating a repeated workflow that should be remembered.
- A future run would benefit from matching and replaying known steps before using the general executor.
- The task has stable procedures with checkpoints, browser steps, shell steps, or memory updates.

## When Not to Use

- The task is clearly one-off and not worth storing as a reusable procedure.
- The workflow is mostly private reasoning with no repeatable operational steps.
- The user is asking for a one-shot answer rather than workflow capture or replay.

## Workflow

- Capture the task summary, structured steps, expected results, risk level, and any screenshots or browser state.
- Prefer explicit steps with clear verification and approval boundaries.
- When a future task matches strongly, try replay first if the procedure is replayable and reliable.
- If replay fails, preserve the failure context and fall back to the general executor instead of hiding the miss.
- Update the procedure when the human corrects a changed step.

## References

- Use `references/procedure-patterns.md` for what makes a good reusable procedure.
