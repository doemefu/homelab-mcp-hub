---
name: start-task
description: Starts the next milestone according to project history. Use when starting Phase 1 of a new milestone or when the user says "start next task" or "proceed with next milestone".
---

When starting the task, always read - think - propose:

1. Thoroughly read the recent worklogs (`.claude/worklogs/`), `.claude/memory/MEMORY.md`, the status table in `README.md`, `INTERFACES.md`, and the contract sections of `docs/080-mcp-hub.md` (in the `doemefu/homelab` repo) that the task touches before starting anything. Check for an `in_progress` worklog and resume it if present.
2. Ask the user about anything that remains unclear.
3. Invoke subagents at least in Phase 3 for review (`@.claude/agents/plan-reviewer`) and Phase 6 for documentation (`@.claude/agents/doc-auditor`), and whenever else it makes sense.
4. Create the worklog file from `.claude/worklog-template.md` and keep documenting thoughts, errors, and findings continuously to avoid repeating mistakes.
5. End the milestone in a buildable, shippable state and write a newest-on-top block in `.claude/memory/MEMORY.md`.
