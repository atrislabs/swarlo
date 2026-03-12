# Atris Skills

Minimal skill set for the `swarlo` repo.

Keep this lean. The goal is to make Swarlo agent-friendly without dragging the full generic Atris skill catalog into the protocol repo.

## Included

- `atris` — load repo context, follow MAP/TODO/workflow
- `autopilot` — run bounded plan/do/review loops until acceptance criteria pass
- `backend` — keep backend changes small and boring
- `swarlo` — use the Swarlo protocol, CLI, and experiment lanes correctly

## Claude integration

`.claude/skills/` symlinks point at these local skills so Claude Code can load them directly inside this repo.
