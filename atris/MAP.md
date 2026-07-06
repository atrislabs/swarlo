# MAP.md

Last updated: 2026-04-13

## Core

- `README.md:1` — protocol overview, CLI usage, API, and design notes
- `pyproject.toml:1` — package metadata, console entrypoint, runtime deps
- `swarlo/__init__.py:1` — package root, exports `SwarloClient`, `SwarloError`, and core types (`Member`, `Post`, `Reply`, `ClaimResult`, `extract_mentions`)
- `swarlo/__main__.py:1` — CLI: `serve`, `join`, `read`, `claims`, `post`, `claim`, `report`, `ping`, `mine`, `score`, `idle`, `suggest`, `init`, `install-hook`, `doctor`
- `swarlo/client.py:1` — Python client: `join`, `read`, `claims`, `post`, `claim`, `report`, `assign`, `touch`, `expire`, `retry`, `briefing`, `liveness`, `score`, `idle`, `suggest`, `ping`, `ready`, `claim_next`, `mine`, `wait_for`, `summary`, `channels`, `members`, `reply`, `claim_file`, `file_claims`, `health`

## Protocol

- `swarlo/types.py:1` — `Member`, `Post`, `Reply`, `ClaimResult` dataclasses, `extract_mentions` helper
- `swarlo/backend.py:1` — `SwarloBackend` interface contract
- `swarlo/sqlite_backend.py:1` — SQLite reference backend for board state
- `swarlo/_briefing.py:1` — task-guided post ranking: `v0_random` (falsification baseline), `v1_regex` (keyword/path overlap), `v2_tfidf` (TF-IDF cosine), `v3_prf_tfidf` (pseudo-relevance feedback), `v4_prf_gated` (gated PRF)
- `swarlo/_precommit_hook_source.py:1` — canonical pre-commit hook source string, used by `install-hook` CLI and `init`

## Server

- `swarlo/server.py:1` — FastAPI app: register, health, channels, posts, claim, claim-file, file-claims, assign, report, touch, ping, ready, mine, idle, briefing, liveness, expire, retry, claims, replay, summary, replies, suggest, members, prune, score, git push/fetch/commits/commits/{hash}/commits/{hash}/children/leaves/lineage/diff
- `swarlo/git_dag.py:1` — git bundle / DAG layer: `init`, `commit_exists`, `get_commit_info`, `unbundle`, `create_bundle`, `diff`, `show_file`

## Scripts & Examples

- `CHANGELOG.md:1` — release history
- `examples/demo.py:1` — 3-agent coordination demo
- `scripts/bench_briefing.py:1` — briefing quality and token-efficiency benchmark
- `scripts/bench_briefing_results.json:1` — benchmark results (recall@5 across scorer tiers)
- `scripts/monitor.py:1` — board monitor daemon, polls activity and flags stale claims
- `scripts/swarlo-precommit-hook:1` — standalone pre-commit hook, blocks commits on files claimed by other agents

## Tests

- `tests/test_swarlo.py:1` — board protocol and SQLite backend behavior
- `tests/test_cli.py:1` — CLI surface and round-trip behavior
- `tests/test_dag.py:1` — DAG and git bundle behavior
- `tests/test_api.py:1` — API route tests: FastAPI routes, auth, status codes, request validation
- `tests/test_client.py:1` — SwarloClient integration tests: full client → server → backend path
- `tests/test_new_features.py:1` — file claims, briefing, liveness, scoring feature tests
- `tests/test_briefing_scorers.py:1` — scorer unit tests: pure-Python scorer edge cases without a live server
- `tests/test_integration.py:1` — e2e 3-agent server test: register, claim/report/conflict cycle, board state verification
- `tests/test_coordination_loop.py:1` — e2e coordination lifecycle: 3 agents, task lifecycle, conflict handling

## Atris Layer

- `atris/atris.md:1` — repo workflow contract for agents
- `atris/TODO.md:1` — active work queue
- `atris/experiments/README.md:1` — experiment pack schema and keep/revert rules
- `atris/experiments/summary-quality/program.md:1` — first live summary-quality experiment against `sqlite_backend.py`
- `atris/experiments/summary-quality/measure.py:1` — summary-quality replay metric
- `atris/experiments/summary-quality/loop.py:1` — summary-quality keep/revert loop
- `atris/experiments/summary-quality/results.tsv:1` — experiment log; `0.8000` → reverted `0.8000` → kept `1.0000`
- `atris/experiments/claim-scope/program.md:1` — live claim-scope experiment for hub-wide `task_key` uniqueness
- `atris/experiments/validate.py:1` — experiment structure validator
- `atris/experiments/benchmark_validate.py:1` — validator benchmark
- `atris/experiments/benchmark_runtime.py:1` — runtime benchmark
- `atris/experiments/claim-scope/measure.py:1` — replay metric for cross-channel duplicate claims and foreign report blocking
- `atris/experiments/claim-scope/loop.py:1` — keep/revert loop for hub-wide claim uniqueness; latest validated keep reached `1.0000`
- `atris/experiments/claim-scope/results.tsv:1` — append-only claim-scope experiment log; baseline `0.5000` → reverted `0.5000` → kept `1.0000`
- `atris/experiments/worker-routing/program.md:1` — routing experiment program: improve builder/validator task routing
- `atris/experiments/worker-routing/candidate.py:1` — bounded routing target: decide whether a `builder` or `validator` should claim a task
- `atris/experiments/worker-routing/measure.py:1` — objective replay metric: 8 hardcoded role-task cases (builder×4 + validator×4), outputs JSON score
- `atris/experiments/worker-routing/loop.py:1` — keep/revert loop for routing proposals; latest validated keep reached `1.0000`
- `atris/experiments/worker-routing/results.tsv:1` — append-only routing experiment log; `0.5000` → `0.8750` kept → re-run `0.5000` → `1.0000` kept

## Good first self-improvement lanes

- `swarlo/__main__.py:1` — worker/operator ergonomics, command UX
- `swarlo/sqlite_backend.py:1` — board semantics, claim/report performance, summary quality
- `swarlo/server.py:1` — protocol edges, auth flow, reply/report behavior
- `swarlo/git_dag.py:1` — DAG correctness and transport robustness
