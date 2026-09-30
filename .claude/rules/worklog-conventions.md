# Worklog Conventions

For each change, create ONE worklog Markdown file and append phase results sequentially.
Full template with inline guidance: `.claude/worklog-template.md`
Copy it as the starting point for every new worklog.

## Location
`.claude/worklogs/`

## Filename convention
`YYYYMMDD-HHMMSS-<slug>-<rand4>.md`
Example: `.claude/worklogs/20260310-142000-longhorn-prereqs-b3x1.md`

## Worklog header (MUST be at the very top)
```yaml
---
id: "YYYYMMDD-HHMMSS-<slug>-<rand4>"
title: "<short human title>"
phase: "research|plan|review|implement|ship|done"
status: "in_progress|blocked|done"
created_at: "YYYY-MM-DDTHH:MM:SS+01:00"
updated_at: "YYYY-MM-DDTHH:MM:SS+01:00"
---
```

## Worklog structure (read & append-only, phases in order)
Exception: during Phase 3 the `## 2. plan` section may be edited to apply review findings; each edit is recorded in `## 3. review` ("Plan updates applied in section 2").

- `## 1. research`
- `## 2. plan` (Markdown plan — see template)
- `## 3. review` (updated plan + findings table)
- `## 4. implement` (summary, commands, results)
- `## 5. check implementation` (test results, verification against plan)
- `## 6. ship` (final verification, release notes if needed)

Record every executed command and its outcome (pass/fail + key output).
