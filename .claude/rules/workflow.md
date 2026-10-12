# Required Workflow for Every Milestone (MUST FOLLOW)

For every user request that results in a code change, follow this 6-phase workflow and document it in a single worklog Markdown file (see worklog conventions). For each step, invoke the relevant superpowers skill. Pivot back to an earlier phase if things dont work out.

## Phase 1 — research
High-level goal: understand what we're doing, why, and find all relevant code.
- Identify open questions and assumptions; if something is unclear, go one step back or ask the user.
- Inspect the local codebase: search with `rg`, check `git log`, check existing patterns.
- Invoke the superpowers brainstorming skill.
- If it seems to be a big change invoke the agent team as mentioned in the Agent Team section of CLAUDE.md.

## Phase 2 — plan
Produce a concrete plan with alternatives and specific file changes.
- Use the Markdown plan structure from `.claude/worklog-template.md` (section 2. plan).
- Include: goal, all options considered (chosen + rejected with reasons), files to change, step-by-step edits, tests, validation commands, risks & mitigations.
- Invoke the superpowers writing-plans skill.

After writing the plan markdown, Claude MUST:
1. Display a summary of it
2. Present a **checklist for user approval**:
    - [ ] Goal is clear and achievable
    - [ ] All options are realistic
    - [ ] No missing dependencies or risks
    - [ ] Implementation steps are concrete
    - [ ] Tests are defined
3. Wait for user input:
    - "Approved, proceed to Phase 3"
    - "Revise [section name]"
    - "Reject, start over"
4. Only proceed to Phase 3 after explicit approval

## Phase 3 — review
Review the plan for defects (secret exposure, missing handlers, version pinning).
- **Invoke the `plan-reviewer` subagent** on the plan before proceeding to implement.
- Apply findings directly by updating the plan in `## 2. plan` of the worklog. This is the one explicit exception to the append-only worklog rule; record every plan edit under "Plan updates applied in section 2" in `## 3. review`.
- Output (a) the updated plan and (b) a concise findings table: what was found and what changed.
- Use context7 to check for the latest documentation where relevant.
- **GitHub:** move the corresponding issue to **Planned** (see `github-project.md`).

## Phase 4 — implement
Implement the plan:
- **GitHub:** move the corresponding issue to **In implementation** (see `github-project.md`).
- Invoke superpower skills subagent-driven-development and/or executing-plans.
- For test driven implementation invoke superpower skill test-driven-development.
- Use context7 to check for the latest documentation if things are unclear.
- Run lint/check commands from the plan and capture results in the worklog.

## Phase 5 — check implementation
Reviews against plan, reports issues by severity. Critical issues block progress.
- **GitHub:** move the corresponding issue to **In review** (see `github-project.md`).
- Invoke superpowers requesting-code-review Skill.
- Commit, push and open the PR on a feature branch, then wait for the bot review(s) and fix or answer every comment before asking the user to merge.
- **Copilot quota / fallback:** request a Copilot review first. If Copilot does not review (quota exhausted) and no other bot is installed, substitute the `reviewer` subagent + `/code-review`, and disclose in a PR comment which review path was used before merging.

## Phase 6 — ship
- Run integration checks if possible (e.g., deploy to cluster, verify via kubectl).
- **Invoke the `doc-auditor` subagent** to check whether README.md, CONTRIBUTING.md, INTERFACES.md, DEPLOYMENT.md or CHANGELOG.md need updates. Implement all required changes it identifies.
- Provide final summary: what changed, how verified, follow-ups.
- **Required: Insert a new block at the top of `.claude/memory/MEMORY.md`** — decision, worklog link, open items.
- **GitHub:** move the corresponding issue to **Done** (see `github-project.md`).

If anything becomes unclear in any phase, go one step back or ask the user.
