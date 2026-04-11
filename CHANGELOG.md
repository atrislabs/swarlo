# Changelog

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
