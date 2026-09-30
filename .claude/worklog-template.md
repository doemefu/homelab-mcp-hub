---
id: "YYYYMMDD-HHMMSS-<slug>-<rand4>"
title: "<short human title>"
phase: "research"
status: "in_progress"
created_at: "YYYY-MM-DDTHH:MM:SS+01:00"
updated_at: "YYYY-MM-DDTHH:MM:SS+01:00"
---

<!--
  WORKLOG RULES
  - This file is append-only. Do not rewrite earlier phase notes.
    Exception: Phase 3 may edit the "2. plan" section to apply review findings;
    list every such edit under "Plan updates applied in section 2" in "3. review".
  - Update `phase` and `updated_at` after each major step.
  - Set `phase: done` and `status: done` only after Phase 6 is complete.
  - Record every command and outcome (pass/fail + key output).
  - Keep all notes in English.
  - This log must explain decisions and findings so future changes avoid repeated mistakes.
  - Remove all HTML comments before finalizing the worklog.
-->

## 1. research

<!--
  Goal: understand current state, constraints, and open questions.
  Capture facts only, no implementation yet.
-->

**Goal:** <!-- one sentence -->

**Current state:**
- <!-- relevant files and behavior -->
- <!-- known limitations / baseline -->

**Repo areas inspected:**
- `<!-- path -->` - <!-- observation -->

**Git history notes:**
- <!-- relevant commit or "none relevant" -->

**Assumptions:**
- A1: <!-- assumption --> [high/medium/low]

**Open questions:**
- Q1: <!-- question --> [blocker/non-blocker]

**Research summary (required, 3 bullets):**
- <!-- fact 1 -->
- <!-- fact 2 -->
- <!-- fact 3 -->

---

## 2. plan

<!--
  Goal: produce an executable plan with alternatives and explicit tests.
  Include all options considered (chosen + rejected).
-->

**Goal:** <!-- one sentence: what must be true when done -->

### Context

**Summary:** <!-- 2-4 sentences -->

**Assumptions:**

| ID | Assumption | Confidence | Implication if wrong |
|----|------------|------------|----------------------|
| A1 | <!-- ... --> | high/medium/low | <!-- ... --> |

**Open questions:**

| ID | Question | Severity |
|----|----------|----------|
| Q1 | <!-- ... --> | blocker/non-blocker |

### Options considered

**Option 1 - [name] (chosen)**
- What:
- Pros:
- Cons:

**Option 2 - [name] (rejected)**
- What:
- Pros:
- Rejected because:

### Changes

**C1 - [short label]**
Files: `<!-- path -->`
- <!-- concrete step -->
- <!-- concrete step -->

**C2 - [short label]**
Files: `<!-- path -->`
- <!-- concrete step -->

### Tests

**Unit:**
```bash
uv run pytest tests/unit/<test_module>.py -v
```

**Contract (gate G6):**
```bash
uv run pytest tests/contract -v
```

**Integration:**
```bash
uv run pytest tests/integration -v
```

**Full verification:**
```bash
uv sync --locked && uv run ruff check && uv run ruff format --check && uv run mypy --strict src && uv run pytest
```

### Validation (proves goal is met)

```bash
# example:
# uv run pytest tests/contract -v
# docker build -t mcp-hub:dev . && scripts/smoke_image.sh mcp-hub:dev
```

### Risks

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|------------|--------|------------|
| R1 | <!-- ... --> | low/med/high | low/med/high | <!-- ... --> |

### Ship notes

- Docs to verify: `README.md`, `CONTRIBUTING.md`, `INTERFACES.md`, `DEPLOYMENT.md`, `CHANGELOG.md`
- User actions required: <!-- none or list -->
- Follow-ups: <!-- none or list -->

**Plan summary (required, 3 bullets):**
- <!-- scope -->
- <!-- test strategy -->
- <!-- main risk -->

---

## 3. review

<!--
  Goal: challenge the plan before implementation.
  Invoke plan-reviewer and apply fixes in section 2.
-->

**plan-reviewer invoked:** yes / no

**Findings:**

| ID | Severity | Finding | Action taken |
|----|----------|---------|--------------|
| F1 | blocker/warning/info | <!-- ... --> | <!-- plan update --> |

**Architectural questions raised:**
- <!-- Q1 -->
- <!-- Q2 -->

**Plan updates applied in section 2:**
<!-- Required record of every edit made to section 2 during this phase (the only allowed rewrite of earlier notes). -->
- <!-- list updates or "none" -->

**Review summary (required, 3 bullets):**
- <!-- key blocker/warning -->
- <!-- key plan fix -->
- <!-- readiness statement -->

---

## 4. implement

<!--
  Goal: execute plan changes and record exact command results.
  If TDD applies: Red -> Green -> Refactor.
-->

**Implementation approach:**
- TDD mode: yes / no
- If no, reason:

**Changes made:**
- C1: `<!-- file -->` - <!-- what changed -->
- C2: `<!-- file -->` - <!-- what changed -->

**Commands and results:**

```bash
$ <command>
# pass/fail + key output
```

```bash
$ <command>
# pass/fail + key output
```

**TDD evidence (required when TDD mode is yes):**
- Red (failing test first): <!-- test name + failing output summary -->
- Green (minimal code): <!-- what made test pass -->
- Refactor: <!-- what improved without behavior change -->

**Implementation summary (required, 3 bullets):**
- <!-- implemented scope -->
- <!-- test status -->
- <!-- unresolved item or "none" -->

---

## 5. check implementation

<!--
  Goal: verify implementation against plan and report issues by severity.
  Critical issues block ship.
-->

**requesting-code-review invoked:** yes / no

**Verification against plan:**
- [ ] Planned changes completed
- [ ] Planned tests executed
- [ ] Validation command successful
- [ ] No secret exposure/logging risks
- [ ] No unrelated refactors

**Findings:**

| ID | Severity | Finding | Resolution |
|----|----------|---------|------------|
| CI1 | critical/high/medium/low | <!-- ... --> | <!-- ... --> |

**Lessons learned (required):**
- <!-- what happened --> -> Prevent next time by: <!-- guardrail -->
- <!-- what happened --> -> Prevent next time by: <!-- guardrail -->
- <!-- what happened --> -> Prevent next time by: <!-- guardrail -->

**Check summary (required, 3 bullets):**
- <!-- quality status -->
- <!-- remaining risk -->
- <!-- ship gate decision -->

---

## 6. ship

<!--
  Goal: finalize release notes, docs, and memory entry.
  Invoke doc-auditor and apply required documentation updates.
-->

**doc-auditor invoked:** yes / no

**Final verification:**
- [ ] Validation command passed
- [ ] Integration checks completed (or marked not applicable)
- [ ] Documentation updates completed

**Docs updated (per doc-auditor):**
- [ ] `README.md` - <!-- section or "not affected" -->
- [ ] `INTERFACES.md` - <!-- section or "not affected" -->
- [ ] `DEPLOYMENT.md` - <!-- section or "not affected" -->

**Decision log (required):**

| ID | Decision | Rationale | Impact |
|----|----------|-----------|--------|
| D1 | <!-- ... --> | <!-- ... --> | <!-- ... --> |

**Final summary (required, 3 bullets):**
- <!-- what changed -->
- <!-- how verified -->
- <!-- follow-up -->

**User actions required:**
- <!-- none or list -->

**Follow-ups:**
- <!-- item -> owner -->

**Memory entry added at top of `.claude/memory/MEMORY.md`:** yes / no
