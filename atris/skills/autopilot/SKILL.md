---
name: autopilot
description: Autonomous plan-do-review loop for bounded Swarlo tasks.
version: 1.0.0
---

# Autopilot Skill

Use for tasks that can be finished with a clear acceptance check.

## Loop

1. Read `atris/MAP.md`
2. Pick one bounded task
3. Implement it
4. Validate it
5. Keep going until the acceptance check passes or you hit a real blocker

## In this repo

Good autopilot lanes:

- CLI polish
- backend protocol fixes
- worker routing
- experiment harness improvements

Bad autopilot lanes:

- vague architecture debates
- broad rewrites with no metric
