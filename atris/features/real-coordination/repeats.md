# Repeat sources on hub `atris`, last 24 hours

Snapshot: 2026-10-15T11:44:47Z. Reads `~/.swarlo/atris.db` only (`?mode=ro`), never writes to it.
Repeat key matches `judge.py`: same member, same first 80 characters, with digits replaced by `#`.

## Totals

- 166 posts in the window. The hub's `repeat_count` sums to 167, so it folded just 1 exact repeat (a `right-rooms` message).
- 46 posts repeat an earlier one under the judge's key, a rate of **0.28**. The judge passes at 0.10 or lower.
- The orchestrator posted 48 times: 47 `nudge` and 1 `escalation`. None of them have a task_key. It accounts for 43 of the 46 repeats.

## Top 5 repeat sources

| # | member | kind | posts | repeats | task_key | sample (numbers → `#`) |
|---|--------|------|------:|--------:|----------|------------------------|
| 1 | orchestrator | nudge | 25 | 24 | none | `[orchestrator] # local branch(es) are older than #h and ahead of origin/master: codex/member-operate/answers(# ahead, #h old), ...` (2026-10-14 20:30 → 10-15 09:46) |
| 2 | orchestrator | nudge | 16 | 15 | none | `[orchestrator] # commit(s) ahead of origin/master on master. PUSH RULE: completed long-running tasks must be merged to master; ...` (10-14 12:21 → 19:59) |
| 3 | orchestrator | nudge | 4 | 3 | none | The same text as #2. It counts as a separate group only because of the judge bug described below (10-14 11:50 → 10-15 11:17). |
| 4 | judge (through the bridge) | result | 3 | 2 | none | `cmp-life judge not passed: not yet: turns.md: Keshav has not answered a turn yet` (#experiments) |
| 5 (tie) | judge (through the bridge) | result | 2 | 1 | none | `cmp-real-coordination judge not passed: not yet · repeats #.#, members #, closed # · next: cut repeat posts; ...` (#experiments) |
| 5 (tie) | orchestrator | nudge | 2 | 1 | none | `@executor idle for #+ min. Your lane: backend/tests/, atris/features/. Find work or report blockers.` |

None of the repeat sources has a task_key, so the hub's claim and task logic never touches them.

## What this tells us

1. **Fixing the orchestrator's nudges fixes the noise.** Its two status messages ("N local branches ahead" and "N commits ahead") make up 42 of the 46 repeats. Their numbers change from post to post, so the hub's fold, which only catches exact matches, misses all of them. Without the orchestrator, the window has 3 repeats in 118 posts (0.03).
2. **The judge has a small bug.** It cuts the text to 80 characters before it replaces the numbers. When the commit count goes from 1 digit to 2, the cut lands in a different place, and one message gets split into two groups (#2 and #3 above). The judge counts 18 repeats there; the correct number is 19. The fix is to replace numbers first, then cut: `re.sub(r"\d+", "#", content)[:80]`.
3. **The bridge repeats some judge results.** The bridge posts every `judge_ran` event, so an unchanged "not passed" verdict lands again each time the judge reruns. That's small now (3 repeats), but it grows with every rerun.
4. **The judge counts itself as a working member.** `judge` (member type `agent`) posted 17 `result` posts through the bridge. `judge.py` only excludes `orchestrator` as a bot, so the judge's own verdicts count toward "3+ members claiming or reporting".

## Would the executor's uncommitted fold pass the judge? (replayed 2026-10-15 ~11:50Z)

The executor has an uncommitted change to `swarlo/sqlite_backend.py`. It folds any post without a task_key when the same member and kind repeat the same first 80 characters (numbers ignored) within 24h. Claims and results are left alone. I replayed the hub's real posts from the last 48 hours through that rule, kept only the rows that would have been stored, and computed the judge's 24-hour repeat rate on them. Script: `/tmp/researcher-repeats/replay.py` (read-only; it imports the rule's key function from the repo).

| rule | rows stored (24h) | repeats | rate | who still repeats |
|------|------:|------:|------:|-------------------|
| today (exact-match fold only) | 171 | 46 | 0.269 | orchestrator 43, judge 3 |
| executor's rule (cut to 80, then ignore numbers) | 127 | 3 | **0.024** | judge 3 |
| same rule, ignore numbers then cut to 80 | 127 | 3 | 0.024 | judge 3 |

- **Once the hub runs it, the executor's change alone passes the repeats bar** (0.024 against 0.10). It removes all 43 orchestrator repeats, and the orchestrator itself needs no changes.
- Cutting before ignoring numbers doesn't matter here: the judge uses the same key, so both sides split the same groups the same way. It's still worth fixing in both places together, but nothing depends on it.
- The 3 leftover repeats are the judge's own unchanged "not passed" verdicts. The bridge posts them as `result`, and results are never folded. To clear them, the bridge would need to skip a `judge_ran` whose summary matches the last one for that campaign.
- The rule's 18 tests pass (`pytest -q tests/test_repeat_fold.py`) on the system `python3` and on both `atrisos-backend` virtual environments. The executor's last log entry said they were blocked on "No module named pytest"; that no longer reproduces.
- Not done here, and it needs the executor or Keshav: commit the change and restart the local hub. Old rows stay in the 24h window until they age out, so the judge's live number falls over the next day rather than all at once.

### Update ~11:50Z: committed, but the live hub isn't running it yet

- The fold was committed as `1f97c56` at 2026-10-15 04:47:15 -0700 (11:47Z).
- The live hub (`swarlo serve --port 8090 --db ~/.swarlo/atris.db`) started at **03:44:55 local**, about an hour *before* that commit (from `ps -eo pid,lstart,command`).
- The hub runs from `atrisos-backend/venv`, which installs swarlo in editable mode from this checkout (`direct_url.json` points to `file:///Users/keshavrao/arena/empire/swarlo`, `"editable": true`). But `swarlo serve` calls `uvicorn.run(app, ...)` with no reload (`swarlo/__main__.py:2630`). Python loaded the old `sqlite_backend` at startup, so the fold is **not live**.
- The board agrees: in the last 24h only 1 post has `repeat_count > 1`, the exact-match `right-rooms` message from before. The orchestrator's latest nudge (11:17Z) is older than the commit, so no post has tested the new fold yet.
- To make it live, restart the hub on port 8090 with the same command. The README allows this as long as a result post says so. The judge's rate then falls as the old orchestrator rows age out of the 24h window, so expect a pass about 24h after the restart. I didn't restart it: this mission is read-only, and the fold belongs to the executor's mission.

## Campaign members who posted claims or results through the bridge (last 24h)

Bridge posts are identified by `campaign_event` in the post metadata, or by a task_key shaped like `cmp-*/<mission>`. Bridge claims carry no metadata, so the task_key is the only tag on them. 112 posts match.

| member | claims | claims done | open | stale/failed | results | failed | campaigns |
|--------|------:|-----:|-----:|-----:|-----:|-----:|-----------|
| researcher | 8 | 4 | 2 | 2 | 4 | 1 | cmp-life, cmp-right-rooms, cmp-campaigns-live |
| engine-manager | 5 | 4 | 0 | 1 | 12 | 0 | cmp-ships-across-atris, cmp-campaigns-live |
| host | 4 | 4 | 0 | 0 | 4 | 0 | cmp-life, cmp-right-rooms |
| jarvis | 3 | 2 | 1 | 0 | 2 | 1 | cmp-life, cmp-right-rooms |
| backend-feature-owner | 2 | 0 | 0 | 2 | 0 | 0 | cmp-ships-across-atris |
| builder | 1 | 1 | 0 | 0 | 1 | 0 | cmp-budget-optional |
| validator | 1 | 1 | 0 | 0 | 1 | 0 | cmp-life |
| executor | 1 | 0 | 0 | 1 | 0 | 1 | cmp-real-coordination |
| auto-improver | 1 | 0 | 0 | 1 | 0 | 1 | cmp-real-coordination |
| linguist | 1 | 0 | 0 | 1 | 0 | 0 | cmp-campaigns-live |
| atris-frontend | 1 | 0 | 0 | 1 | 0 | 0 | cmp-ships-across-atris |
| ios-engineer | 1 | 0 | 0 | 1 | 0 | 0 | cmp-ships-across-atris |
| judge | 0 | 0 | 0 | 0 | 17 | 0 | (judge_ran, no task_key) |

12 non-bot members claimed or reported through the bridge, and 16 claims closed as done.

Caveats:
- The bridge posts mission results under the event's actor. For `cmp-ships-across-atris` that actor is `engine-manager`, even for missions started by other members (atris-navigator, validator, interface-dev and others). Those members show briefings but no results.
- Two `engine-manager` posts about `cmp-campaigns-live/m-cadence` (10:38 claim, 10:40 "Shipped as atris #1333") read as hand-written, not bridge-generated. Claims carry no bridge tag, so I can't confirm this from the database.

## Rerun

```
python3 atris/features/real-coordination/judge.py
sqlite3 "file:$HOME/.swarlo/atris.db?mode=ro" "SELECT member_id, kind, count(*), sum(task_key IS NOT NULL) FROM posts WHERE hub_id='atris' AND created_at > strftime('%Y-%m-%dT%H:%M:%S','now','-1 day') GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10"
```
