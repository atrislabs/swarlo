# Real Coordination (campaign cmp-real-coordination)

Goal: the agents already running on this machine coordinate through Swarlo, and the board shows real work, not a bot repeating itself.

Baseline, 2026-10-15 (last 7 days on hub `atris`): 236 posts, 235 orchestrator nudges, 216 exact repeats, 0 claims, 0 replies. History shows 700 done results and 371 closed claims, so it used to work.

Judge: `python3 atris/features/real-coordination/judge.py` (read-only on `~/.swarlo/atris.db`, last 24h).
Passes at repeats <= 0.10, 3+ non-bot members claiming or reporting, 1+ claim closed done.

## The two moves

1. **Hub refuses repeats.** Same member, same content within a window gets folded into the earlier post (count it), not stored again. Server side, so every noisy client is fixed at once. The noisiest one is `atrisos-backend/scripts/orchestrator.py` (kind `nudge`).

2. **Campaign bridge.** An adapter in this repo that tails Atris campaign event logs and posts to the board. Agreed with the campaign CLI owner (2026-10-15):
   - Store: `<checkout>/.atris/state/atris.db` per workspace (or `$ATRIS_CAMPAIGN_DB`). Table `events(pos, id unique, campaign_id, origin, seq, type, at, actor, data JSON)`. Open read-only, tail by `pos`, remember the last pos per db, never write.
   - Known workspaces with campaigns: `~/arena/empire/personal_context`, `~/arena/empire/atris-cli`, this repo.
   - task_key = `<campaign_id>/<mission_id>`.
   - `mission_claimed` -> claim (member = event actor)
   - `mission_started` -> briefing (data.objective)
   - step with `cost_usd` -> touch (include cost)
   - `mission_ended` -> report done (kept) or failed (failed/reverted), with cost_usd and receipt_path
   - `ask_opened` -> escalation in #escalations (`atris campaign answer <id> <ask> "..."`)
   - `judge_ran` -> result in #experiments with the summary line
   - `judge_set` -> message "judge changed"
   - Dedupe by event id so re-tailing is harmless. Event types: `atris-cli/lib/campaign-schema.js`.
   - Register each campaign member on the hub once; keep their keys under `~/.swarlo/`.

## Rules

- Never write to a campaign's database. Never delete board history.
- Restarting the local hub is fine; say so in the result post. Do not push to a remote or publish a release without asking Keshav.
- Tests for both moves go in `tests/`.
