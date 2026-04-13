# TODO.md

> Working task queue for this project. Target state = 0.
> Note: Daily tasks live in `atris/logs/YYYY/YYYY-MM-DD.md`

---

## Backlog

---

## In Progress

---

## Completed

- **T1–T5:** Fixed 17 broken references in MAP.md — 3 stale descriptions (CLI commands, server endpoints, git_dag methods), 3 stale experiment descriptions (results.tsv progressions, measure.py clarification), 4 missing modules (\_\_init\_\_.py, client.py, \_briefing.py, \_precommit\_hook\_source.py), 6 missing test files, header timestamp updated.
  Outcome: MAP.md now matches actual file contents across all sections.
- Cleaned post-launch repo health.
  Outcome: workflow actions now target the Node 24 majors, PyPI install/build/test were revalidated, and unused preview ballast was removed.
- `claim-scope` improved `sqlite_backend.py` with a real keep/revert loop.
  Outcome: baseline `0.5000`, bad proposal reverted, good proposal kept at `1.0000`.
- `summary-quality` improved `sqlite_backend.summarize_for_member()` with a real keep/revert loop.
  Outcome: baseline `0.8000`, bad proposal reverted, good proposal kept at `1.0000`.
- Moved `AGENTS.md` and `CLAUDE.md` under `atris/` so the repo-local Atris workspace owns the agent boot path.
  Outcome: the repo keeps the same instructions, but the Atris layer is now the source of truth.

---
