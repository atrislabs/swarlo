---
name: atris
description: Atris workflow for the swarlo repo. Load MAP/TODO, use the repo experiment system, keep diffs small.
version: 1.0.0
---

# Atris Skill

Use this in the `swarlo` repo.

## Load first

- `atris/atris.md`
- `atris/PERSONA.md`
- `atris/MAP.md`
- `atris/TODO.md`

## Workflow

- Read `atris/MAP.md` before searching.
- Claim work in `atris/TODO.md`.
- Use `atris/experiments/` for bounded keep/revert loops.
- Prefer the smallest real diff, then validate.

## Rules

- Keep changes tight.
- Do not add broad scaffolding unless it directly helps Swarlo.
- Update `MAP.md` if the repo structure changes.
