# TODO.md

> Working task queue for this project. Target state = 0.
> Note: Daily tasks live in `atris/logs/YYYY/YYYY-MM-DD.md`

---

## Backlog

---

## In Progress

---

## Completed

- `claim-scope` improved `sqlite_backend.py` with a real keep/revert loop.
  Outcome: baseline `0.5000`, bad proposal reverted, good proposal kept at `1.0000`.
- `summary-quality` improved `sqlite_backend.summarize_for_member()` with a real keep/revert loop.
  Outcome: baseline `0.8000`, bad proposal reverted, good proposal kept at `1.0000`.
- Moved `AGENTS.md` and `CLAUDE.md` under `atris/` so the repo-local Atris workspace owns the agent boot path.
  Outcome: the repo keeps the same instructions, but the Atris layer is now the source of truth.

---
