# Loop - Feedback

**Owner:** `team/auto-improver`
**Wiki:** [systems/loops.md](../wiki/systems/loops.md)
**Runner:** `grep -q '## Backlog' atris/TODO.md`

**Protects:** every user pain, bug report, or operator note becomes tracked work
or an explicit no-op decision.

**Signal (green =):** task intake file present with Backlog section (exit 0).

**Check:** `grep -q '## Backlog' atris/TODO.md`

**Cadence:** per tick and weekly triage.

**Feeds:** quality.
**Fed by:** users, support, operators, telemetry.

## Log

- 2026-07-07: Loops scaffold wired — owner auto-improver, intake probe greps `atris/TODO.md` Backlog section.
