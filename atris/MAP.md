# MAP.md

Last updated: 2026-03-12

## Core

- `README.md:1` — protocol overview, CLI usage, API, and design notes
- `pyproject.toml:1` — package metadata, console entrypoint, runtime deps
- `swarlo/__main__.py:1` — thin CLI: `serve`, `join`, `read`, `claims`, `post`, `claim`, `report`

## Protocol

- `swarlo/types.py:1` — `Member`, `Post`, `Reply`, `ClaimResult` dataclasses
- `swarlo/backend.py:1` — `SwarloBackend` interface contract
- `swarlo/sqlite_backend.py:1` — SQLite reference backend for board state

## Server

- `swarlo/server.py:1` — FastAPI app, auth, register endpoint, board routes
- `swarlo/git_dag.py:1` — git bundle / DAG layer for push, fetch, leaves, children, lineage, diff

## Tests

- `tests/test_swarlo.py:1` — board protocol and SQLite backend behavior
- `tests/test_cli.py:1` — CLI surface and round-trip behavior
- `tests/test_dag.py:1` — DAG and git bundle behavior

## Atris Layer

- `atris/atris.md:1` — repo workflow contract for agents
- `atris/TODO.md:1` — active work queue
- `atris/experiments/README.md:1` — experiment pack schema and keep/revert rules
- `atris/experiments/validate.py:1` — experiment structure validator
- `atris/experiments/benchmark_validate.py:1` — validator benchmark
- `atris/experiments/benchmark_runtime.py:1` — runtime benchmark

## Good first self-improvement lanes

- `swarlo/__main__.py:1` — worker/operator ergonomics, command UX
- `swarlo/sqlite_backend.py:1` — board semantics, claim/report performance, summary quality
- `swarlo/server.py:1` — protocol edges, auth flow, reply/report behavior
- `swarlo/git_dag.py:1` — DAG correctness and transport robustness
