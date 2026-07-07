# Loop - Quality

**Owner:** `team/validator`
**Wiki:** [systems/loops.md](../wiki/systems/loops.md)
**Runner:** `python -m pytest -x -q tests/test_briefing_scorers.py`

**Protects:** shipped changes do not regress coordination scoring or briefing trust.

**Signal (green =):** focused pytest suite passes (16 briefing scorer tests).

**Check:** `python -m pytest -x -q tests/test_briefing_scorers.py`

**Cadence:** per commit and before release.

**Feeds:** feedback, release confidence, owner trust.
**Fed by:** feedback, reviews, incidents, failed checks.

## Log

- 2026-07-07: Loops scaffold wired — owner validator, check `test_briefing_scorers.py` (16 passed, ~0.04s).
