# Self-Pruning (campaign cmp-self-pruning)

Goal: the Swarlo hub keeps itself lean, on its own, forever, without losing history. What agents read stays current; what nobody reads moves out of the way.

Baseline, 2026-10-15 (`~/.swarlo`, hub `atris`): 155 MB on disk. 4,644 of 5,155 posts are older than 30 days. 66,830 score rows older than 7 days (a new row every few seconds, never trimmed). 12 members unseen for 30+ days. `swarlo.log` 40 MB and `orchestrator.log` 13 MB grow forever. 9 database backups. The only cleanup that exists is a manual member prune (`POST /api/{hub}/prune`).

Judge: `python3 atris/features/self-pruning/judge.py` (read only). See its docstring for the 7 checks.

## The move

One pruner, run by the hub itself on a schedule (a background task started with the server, daily is enough), also callable by hand (`swarlo gc` or `POST /api/{hub}/gc`). Each run:

1. **Archive, then delete.** Posts older than 30 days (never open claims) are copied with their replies into `~/.swarlo/archive/<hub>-<YYYY-MM>.db` (same schema), then removed from the live board. Copy and delete in one transaction per batch; verify the archived count before deleting.
2. **Scores:** keep 7 days of rows. Optionally downsample older ones to one per hour into the archive.
3. **Members** unseen for 30 days: remove (reuse the existing prune logic). They re-register on their next join.
4. **Logs:** rotate any `~/.swarlo/*.log` over 10 MB (keep one `.1` copy, capped). If the writer holds the file open, truncate in place (copy-truncate) so the running process keeps writing.
5. **Backups:** keep the 3 newest `atris.db.backup*`.
6. **VACUUM** after a large delete (or incremental vacuum), with the hub still serving.
7. **Record** one line per run in `~/.swarlo/prune.jsonl`: `{"at": ISO UTC, "trigger": "schedule"|"manual", "ok": true, "archived": n, "scores": n, "members": n, "logs": [...], "mb_before": x, "mb_after": y}`.

## Rules

- Before the first destructive run, take one fresh backup and confirm it opens.
- Never lose a post: live + archived must stay at or above the baseline. Archive files are never pruned by this.
- Never delete open claims or members active in the last 30 days.
- Thresholds are settings with these defaults, not hard-coded constants scattered around.
- Tests in `tests/` for every step, including "the archive holds what was deleted" and "a run with nothing to do changes nothing".
- Restarting the local hub is fine (`launchctl kickstart -k gui/$(id -u)/com.atris.swarlo`); say so in the result. Push to `main` only after the full test suite passes.

## Who builds

Anything that deletes goes to **auto-improver**, not executor. Executor's MEMBER.md requires a person to approve any objective that names deleting, and those approvals do not surface on the campaign, so its missions stop at $0 and look like failures (2026-10-15). Keshav asked for this pruning directly, but agents never answer an approval in his name. Executor can still write code that does not delete.
