# scorecards.md — Endgame Results

> Append-only. One line per closed endgame. Records outcome metrics from the horizon.

---
- **[2026-04-13] activate-rl-loop** — shipped: 1/1 — wall-clock: 20.6h — halt: 0% — reward: 0 — lessons: 0
- **[2026-04-13] map-and-test-health** — shipped: 0/0 — wall-clock: 21.8h — halt: 100% — reward: -2 — lessons: 2
- **[2026-07-10] improve-3x-liveness-spiral** — shipped: 3/3 — verify: 424 tests pass — reward: +13 — ticks: tower-tolerant → migrate last_seen/metadata → doctor hears schema drift
- **[2026-07-10] improve-dock-cli-routes** — shipped: 1/1 — verify: tests pass — reward: +5 — GET /unclaimed /xp /scores + per_agent_xp on score
- **[2026-07-10] improve-handoff-route** — shipped: 1/1 — verify: tests pass — reward: +5 — mount GET /handoff_trail (walk already existed)
- **[2026-07-10] improve-cli-404-hint** — shipped: 1/1 — verify: 437 tests — reward: +4 — 404 on missing routes now says restart hub
- **[2026-07-10] improve-doctor-api-surface** — shipped: 1/1 — verify: 437 tests — reward: +4 — doctor warns when OpenAPI lacks unclaimed/xp/scores/handoff + client methods
