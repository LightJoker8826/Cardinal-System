# Cardinal System — Claude Operating Manual

Read this file and obey it before every task in this project.

## Session startup (mandatory)

1. Read [`primer.md`](primer.md) — where we left off, blockers, next action
2. Read [`tasks/lessons.md`](tasks/lessons.md) — user corrections as rules
3. Skim [`claude-memory.md`](claude-memory.md) for recent commit context

## Session shutdown (mandatory)

Rewrite [`primer.md`](primer.md) completely: where we left off, in progress, blocked, next session first action, active plans.

## 5-step Kayaba intake (every request)

Before writing code, running audits, or executing plans:

| Step | Name | Action |
|------|------|--------|
| **1** | Doctrine / Axiom | What law governs this? What must never be violated? |
| **2** | Persistence / History | What state must be remembered? What carries biography forward? |
| **3** | Sovereignty / Observer | What does the architect observe vs what does the system decide? |
| **4** | Plan + Tests | File list, checklist, adversarial tests |
| **5** | God-View Verification | Success visible from dashboard/tests/reports without code inspection |

If the brief is incomplete after step 1, **ask questions** before step 4. Never assume missing scope.

**Execute only on explicit user approval** after the plan is shown.

## Task templates

### Security audit

1. Doctrine: no secrets in git/frontend; auth rate-limited; Taboo-style input rejection
2. Persistence: findings in `docs/security_audit.md`; fixes versioned
3. Sovereignty: report vulnerabilities; architect approves fixes
4. Plan: scan API routes, `.env`, hardcoded tokens, payload limits
5. God-View: written report; no silent production changes

### Kayaba build

1. Doctrine: Taboo Index is absolute law
2. Persistence: `entity_registry`, `encounter_memory`, `pain_memory`
3. Sovereignty: dashboard God-view; system decides consequences
4. Plan: axiom emergency → fluctlight memory → dashboard panels → tests
5. God-View: panels live during `sim_runner --watch`

### Fable 5 prompt

Use [`docs/fable5_prompts.md`](docs/fable5_prompts.md). Language: **"The system will enforce Y"** — not "I want X."

## Memory files

| File | Role |
|------|------|
| `primer.md` | Session handoff snapshot (rewritten each session) |
| `claude-memory.md` | Append-only commit chronicle (git hook) |
| `tasks/lessons.md` | User corrections → permanent rules |

## Git hooks

```bash
git config core.hooksPath .githooks
```

The `post-commit` hook appends date, time, hash, and message to `claude-memory.md`.

## Lessons protocol

When the user corrects you, append to `tasks/lessons.md`:

```markdown
- [YYYY-MM-DD] Rule: <what to always/never do>
```
