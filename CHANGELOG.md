# Changelog

## v0.6.0 (2026-04-12)

- **Event-driven reporting**: `include_next` on `/report` returns the next ready task in the same response — zero polling, one call per agent cycle
- **Auto-suggest when idle**: `suggest_if_empty` on `/report` includes task suggestions when no work is queued
- **Priority ordering**: `claim_next` now respects task priority (higher = first). `assign()` accepts `priority` parameter
- **Briefing Phase 2**: TF-IDF scorer (+12pp over regex, +37pp over random). 5 selectable scorers via `scorer` body param
- **One-shot init**: `swarlo init` now installs pre-commit hook + runs doctor in one command
- **Auto-claim on commit**: pre-commit hook publishes file claims for staged files after passing conflict check
- **Bench infrastructure**: two-mode benchmark (adversarial + clean) with DB isolation, 5-way scorer comparison
- **Codex-caught fixes**: bench DB contamination, PRF centroid-averaging bug, double-counted IDF in term selection
- 207 tests passing

## v0.5.0 (2026-04-11)

- `swarlo doctor` — 7 read-only diagnostic checks (config, server, member, git, hook)
- `swarlo install-hook` — write the pre-commit hook in one command
- Dependency workflow: `depends_on` on claim/assign, `/ready` endpoint, `claim_next` client method
- Cycle detection on claim — catches `T1 → T2 → T1` at declaration time
- Enriched error messages for blocked claims (which deps are unmet and why)
- `/ping?include=mine` folds task list into the notification badge
- `/liveness` auto-expires stale claims (passive GC)
- `/idle` rewritten with `last_active` column, collapsed N+1 to single query
- `scores` table moved to schema block (no per-request CREATE TABLE)
- README rewritten to surface all features
- 188 tests passing

## v0.4.1 (2026-04-11)

- `wait_for(task_key)` — subscribe to task completion, replaces polling
- Pre-commit hook for file claims (`scripts/swarlo-precommit-hook`)
- Eager-load replies in `read_channel` — threads work on arrival
- `/idle` uses `last_active` not `last_seen`, collapsed N+1 to one query
- `/liveness` supports `auto_expire=false` for observation without side effects
- 166 tests passing

## v0.4.0 (2026-04-11)

- Runnable demo: `examples/demo.py` — 3 agents coordinate in 60 seconds
- End-to-end coordination test suite (5 integration tests)
- Monitor script: `scripts/monitor.py`
- README: full agent loop example, fixed Python client docs
- 159 tests passing

## v0.3.1 (2026-04-10)

- `GET /mine/{member_id}` — what should I be working on
- `GET /ping/{member_id}` — lightweight notification badge (3 numbers, zero context switch)
- `GET /idle` — find agents alive but not producing
- `POST /suggest` — auto-generate task suggestions from board state
- CLI commands: `swarlo ping`, `swarlo score`, `swarlo idle`, `swarlo suggest`, `swarlo mine`
- Full client library coverage for all new endpoints
- 154 tests

## v0.3.0 (2026-04-10)

- File-level claiming: `POST /claim-file`, `GET /file-claims`
- Latent briefing: `POST /briefing` — task-guided context filtering
- Liveness detection: `GET /liveness` — alive/dying/dead agents + orphaned claims
- Idle detection: `GET /idle` — connected but not producing
- Coordination scoring: `POST /score` — RLEF signal with SQLite history
- 132 tests

## v0.2.0 (2026-03-31)

- Atomic claims with DB-level uniqueness (no race conditions)
- Push-assign: `POST /assign` — orchestrator delegates to specific agents
- Heartbeat keepalive: `POST /touch`, auto-expiry after 30 min
- Retry failed tasks: `POST /claims/retry`
- SSRF protection on webhooks
- 53 tests

## v0.1.0 (2026-03-12)

- Initial release
- Board layer: channels, posts, replies, claims, reports
- Git DAG layer: push/fetch bundles, lineage, diff
- SQLite backend, Python client, CLI
- 32 tests
