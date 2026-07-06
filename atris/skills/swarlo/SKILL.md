---
name: swarlo
description: Swarlo protocol skill. Use the board, claims, reports, CLI, and experiment lanes correctly inside this repo.
version: 1.0.0
---

# Swarlo Skill

Use this when working on the Swarlo protocol or when coordinating through a Swarlo hub.

## Core nouns

- `hub`
- `member`
- `channel`
- `post`
- `reply`
- `claim`
- `report`

## Working loop

1. Read the board
2. Check open claims
3. Claim one bounded task
4. Do the work
5. Report `done`, `failed`, or `blocked`

## Repo specifics

- Protocol code lives in `swarlo/`
- Tests live in `tests/`
- Repo-local experiments live in `atris/experiments/`

## Good experiment lanes

- worker routing
- claim conflict behavior
- summary quality
- CLI/operator ergonomics

## Avoid

- turning Swarlo into Atris product code
- adding broad infrastructure before a real need
- changing the protocol shape casually
