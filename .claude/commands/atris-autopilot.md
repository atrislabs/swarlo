---
description: PRD-driven autonomous execution - give it a task, it loops until done
arguments:
  - name: task
    description: What to build (e.g., "Add dark mode toggle")
    required: true
  - name: max-iterations
    description: Max loops before stopping (default 10)
    required: false
---

# Atris Autopilot

Autonomous mode. Loop until task complete or max iterations.

## Setup State

```bash
mkdir -p .claude
cat > .claude/atris-autopilot.state.md << 'STATEEOF'
---
iteration: 1
max_iterations: ${2:-10}
completion_promise: <promise>COMPLETE</promise>
---

$1
STATEEOF
```

## Task: $1

## Process (each iteration)

1. **PLAN** — Read MAP.md, identify ONE thing to do
2. **DO** — Implement it, commit
3. **REVIEW** — Check acceptance criteria

## Rules

- ONE thing per iteration
- Check MAP.md before touching code
- Search before assuming not implemented
- When done: `<promise>COMPLETE</promise>`

## Start

Read atris/MAP.md. Begin iteration 1.