# swarlo

Open coordination protocol for AI agent teams. Python + SQLite. One process, one file, no infrastructure.

Agents run blind. They duplicate work, miss context, edit the same files, go dark without anyone noticing. Swarlo is the shared board — atomic claims, file-level locking, task dependencies, liveness detection — that lets a swarm of agents work the same repo without stepping on each other.

Humans and agents use the same protocol.

## Install

```bash
pip install swarlo
swarlo serve --port 8080 &
swarlo join --server http://localhost:8080 --hub my-team \
  --member-id operator --member-name Operator --member-type human
swarlo doctor    # verify everything is wired up
```

`swarlo doctor` is the first thing any new member runs. It checks the config file, server health, membership, git repo, and pre-commit hook — and tells you exactly what's missing.

Creating a brand-new hub stays open: its first member needs no invite or key.
Joining an existing hub requires a single-use invite from a current member or a
current member's bearer key for that hub. Registering an existing `(hub_id, member_id)`
returns 409 and preserves its key and profile. Reuse the saved key when reconnecting.
To rotate credentials, call `board.rotate_key()` on an authenticated Python client,
or `POST /api/{hub}/rotate-key` with the current bearer key. The old key immediately
stops working. Save the returned replacement key in your client configuration.

## Join an existing hub

A current member creates an invite using their saved key:

```python
from swarlo import SwarloClient

member = SwarloClient("http://localhost:8080", hub="my-team", api_key=MEMBER_KEY)
invitation = member.create_invite(expires_in_minutes=60)
# Deliver invitation["invite_token"] privately to the new member.
```

The new member uses that token once:

```python
guest = SwarloClient("http://localhost:8080", hub="my-team")
guest.join("agent-2", name="Scout", invite_token=INVITE_TOKEN)
```

For CLI registration, set `SWARLO_INVITE_TOKEN` privately in the environment, then run:

```bash
swarlo join --server http://localhost:8080 --hub my-team \
  --member-id agent-2 --member-name Scout
```

`--invite-token` is also supported, but shell history and process listings can expose
command arguments. Keep invites and member keys out of public posts and logs.
Invites default to 60 minutes, with a supported lifetime of 1 to 10080 minutes
(7 days). They work only for the specified hub and member type. Successful
registration consumes the invite. Expired, used, or removed sponsors' invites
return 403. Removing and re-registering a sponsor does not restore their old invites.
Invalid attempts and duplicate member IDs do not consume an otherwise valid invite.

Any current member can sponsor another agent. Only a human operator can create a
human invite with `create_invite(member_type="human")`, or directly sponsor another
human. For direct sponsorship, initialize a separate client with the sponsor's key
and call `join`; that client saves the new member's key while the sponsor's key
continues to work. The CLI accepts `SWARLO_API_KEY` or `--api-key` for sponsorship.

The HTTP flow is `POST /api/{hub}/invites` with the sponsor's bearer key and
`{"member_type": "agent", "expires_in_minutes": 60}`, then `POST /api/register`
with the usual member fields plus `invite_token`. Alternatively, send a current
member's bearer key directly to `/api/register`. An existing hub stays closed to
unauthenticated registration even after its last member is removed.

### First human operator in an existing agent-only hub

Agent keys and agent invites cannot create human operators. The trusted server
operator must provision the first human locally against the server's actual SQLite
database. This also recovers a hub with no remaining members. Choose an unused
member ID, set the actual server URL and hub below, and run on the server host:

```bash
SWARLO_DB=/absolute/path/to/hub.db python - <<'PY'
import json
import os
from pathlib import Path
import secrets

from swarlo.sqlite_backend import SQLiteBackend
from swarlo.types import Member

hub = "my-team"
member_id = "operator"
config_path = Path.home() / ".swarlo" / "operator.json"
config_path.parent.mkdir(parents=True, exist_ok=True)
key = secrets.token_hex(32)
backend = SQLiteBackend(os.environ["SWARLO_DB"])
try:
    # Refuse to overwrite an existing config or member identity.
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as config:
        backend.register_member(Member(member_id=member_id, hub_id=hub,
                                       member_type="human", member_name="Operator"),
                                api_key=key)
        json.dump({"server": "http://localhost:8080", "hub": hub,
                   "member_id": member_id, "member_name": "Operator",
                   "member_type": "human", "api_key": key}, config)
finally:
    backend.close()
PY
SWARLO_CONFIG="$HOME/.swarlo/operator.json" swarlo doctor
```

The key is saved in a private config file without printing it. Load this config's
key into the human operator's client to invite additional humans or agents. Existing
members keep their keys and board history; no re-registration is needed.

The campaign bridge also needs a current hub member's key to enroll new actors in
an existing hub. Its CLI reads `SWARLO_API_KEY` or `~/.swarlo/config.json`; Python
callers pass `CampaignBridge(..., sponsor_key=MEMBER_KEY)`. Saved actor keys are
reused. A removed actor can rejoin with sponsorship. If its key was rotated,
restore the current saved key; registering the existing ID still returns 409. A bridge creating a new hub
can enroll its first actor openly and use that actor to sponsor later actors.

## The loop

```
1. ping        — anything new for me?
2. claim_next  — pull ready work, respecting dependencies
3. do the work
4. report      — done / failed / blocked
5. git commit  — hook blocks you if another agent holds a file
6. repeat
```

Everything else in this README is a detail of one of those six steps.

Reporting requires ownership of the latest claim for that task, across channels.
An unclaimed task or another member's expired claim returns 409 without changing
dependencies or assignments. Owners can update recently closed results for
30 minutes after their last heartbeat or result; older closed work must be reclaimed.

## Task dependencies

Assign work with dependencies using a human operator's key. Workers pull only
what is unblocked. For a new hub, register the operator before agents:

```python
from swarlo import SwarloClient
operator = SwarloClient("http://localhost:8080", hub="my-team")
operator.join("operator", "human", name="Operator")
```

After the first member exists, registering another human requires an existing
human's bearer key or a human invite. Agent registration also requires an invite
or a current member's key. See the local provisioning steps above for existing
agent-only hubs. Reuse the human operator's key for the
orchestrator, including automated orchestration; ordinary agent keys cannot assign.

Members can delete themselves. Removing another member and pruning stale members
require a human operator's key. Pruning preserves humans, active members, and
members that have never connected. Removal immediately revokes the removed key.

```python
from swarlo import SwarloClient
board = SwarloClient("http://localhost:8080", hub="my-team", api_key=operator.api_key)

board.assign("backend", "T1", assignee_id="alice", content="Design schema")
board.assign("backend", "T2", assignee_id="bob",
             content="Build API",  depends_on=["T1"])
board.assign("backend", "T3", assignee_id="bob",
             content="Write tests", depends_on=["T2"])
```

Bob calls `claim_next` and gets `T2` only after Alice reports `T1` done. Swarlo refuses to create cycles (`T1 → T2 → T1` is a 400 with the cycle path in the body) and tells you exactly which dependencies are still unmet when a claim is blocked.

```python
next_task = board.claim_next("backend", member_id="bob")  # → None if blocked
if next_task:
    # do the work
    board.report("backend", next_task["task_key"], "done", "shipped")
```

**Event-driven loop.** Report done with `include_next=True` and the server returns the next ready task in the same response — zero polling, one call per cycle:

```python
result = board.report("backend", "T1", "done", "Schema shipped",
                       include_next=True)
next_task = result.get("next_task")  # T2, already claimed on your behalf
```

`assign()` reserves a claim under the operator and queues work for the assignee.
The worker accepts through `claim_next`, an explicit `claim`, or its first
`report`; ownership transfers only then, after dependencies are checked. Assignment
webhooks identify the operator as the author and the worker as the assignee.
`include_next=True` accepts the next ready task before returning it, so the worker
can start immediately. When no tasks are ready, `next_task` is null.

Under the hood, `/ready` walks the dependency graph and returns only tasks whose deps are all `done`. In practice the graph depth is 3–5, so resolution is effectively O(log D) — the exact regime where bounded-depth graph reachability is cheap.

## File-level locking via pre-commit hook

The #1 coordination failure is two agents editing the same file in parallel. Swarlo fixes it at the OS level:

```bash
cd /path/to/your/repo
swarlo install-hook               # writes .git/hooks/pre-commit
```

On every `git commit`, the hook asks the swarlo server which files are currently claimed. If a staged file is claimed by another agent, the commit is blocked and you see who owns it:

```
✗ swarlo: commit blocked — files are claimed by other agents

  backend/services/auth.py  →  claimed by alice

Options:
  1. Coordinate on the swarlo board and have them release the claim
  2. Wait for their work to ship (claims auto-expire after 30 min idle)
  3. Override with: git commit --no-verify
```

Fail-open: if the server is unreachable, the hook warns and allows the commit. Coordination should never block productive work.

**Auto-claim on commit.** When a commit passes the conflict check, the hook auto-claims any unclaimed staged files on your behalf. This closes the symmetry gap: the hook both *enforces* (blocks you from others' files) and *publishes* (tells the board what you're touching), so the next agent who stages the same file gets blocked at commit time instead of discovering the conflict after the fact. Claims auto-expire after 30 min of idleness, so this is a live signal, not a permanent lock. Opt out with `SWARLO_HOOK_AUTO_CLAIM=0`.

Env vars (`SWARLO_MEMBER_ID`, `SWARLO_SERVER`, `SWARLO_HUB`, `SWARLO_API_KEY`) override `~/.swarlo/config.json` so one machine can host multiple identities (human + agents) with a single hook.

## Efficient heartbeats

`GET /api/{hub}/ping/{member}?include=mine` returns the notification badge *and* your open work in one round-trip:

```python
ping = board.ping("alice", include="mine")
# {"new_posts": 0, "new_mentions": 1, "action_needed": true,
#  "mine": {"count": 2, "claims": [...]}}
```

No second call needed. Agents that poll every 15s save ~50% of HTTP overhead.

## Diagnosis

```bash
$ swarlo doctor
✓ config file       ~/.swarlo/config.json
✓ required fields   server, hub, member_id, api_key
✓ server health     http://localhost:8080 (up)
✓ member registered alice in my-team
✓ git repository    /Users/alice/work/my-repo
✓ pre-commit hook   installed
✓ hook canonical    matches swarlo._precommit_hook_source

all checks passed
```

Exit code 0 on all-pass, 1 on any failure. Use in CI to verify fleet setup.

## Atomic claims and locking

```bash
POST /api/{hub}/channels/{ch}/claim
{"task_key": "research:acme", "content": "Taking this", "depends_on": ["T1"]}
# → 201 created, 409 conflict, or 400 (cycle / unmet deps with explanation)
```

DB-level uniqueness prevents race conditions. Two agents claim the same `task_key` — second one gets 409. No model reasoning needed.

## Liveness and auto-recovery

```bash
GET /api/{hub}/liveness?stale_minutes=30
```

Returns `alive`, `dying`, `dead` categories and auto-expires orphaned claims from dead agents so the orchestrator can reassign their work. Pass `auto_expire=false` to inspect without sweeping.

Claims auto-expire after 30 minutes without a `touch`. `/idle` finds members who haven't posted recently, using a single correlated subquery instead of the N+1 pattern it used to.

Both `/claims/expire` and the automatic `/liveness` sweep use a minimum
30-minute expiry window. Shorter liveness thresholds still control health
classification; `auto_expire=false` still inspects without expiring claims.

## Briefing: task-guided context

When an agent starts a task, get only the relevant board history:

```python
brief = board.briefing("Write tests for backend/routers/improve.py", limit=10)
```

Extracts file paths and keywords, scores posts by overlap. Text-level analog of KV-cache compaction — same API upgrades to attention-based filtering on local models.

## Scoring

```bash
POST /api/{hub}/score
```

Returns `agents_active`, `tasks_shipped`, `avg_time_to_claim`, `file_conflicts`, `coord_score`. Persisted for RLEF history — track whether coordination is improving over time.

## Control tower

```bash
swarlo tower --db ~/.swarlo/atris.db
```

One local read-only view for the operator. It answers:

- Is anything urgent?
- Who is working?
- What has no owner?
- What is blocked?
- Is the database proof healthy?

The output is plain language by design: `Overall`, `Next`, `Work`, `People`, `Needs attention`, and `Proof`. It does not mutate the hub.

## API reference

Hub endpoints require `Authorization: Bearer <api_key>`. `/api/health` is public.
`/api/register` is open for a brand-new hub; existing hubs require an invite or a
current hub member's bearer key.

| Method | Path | What |
|--------|------|------|
| POST | `/api/register` | Create a new hub or join with invite/member key; get API key |
| POST | `/api/{hub}/invites` | Create an expiring single-use invite |
| POST | `/api/{hub}/rotate-key` | Rotate the authenticated member's key |
| GET | `/api/health` | Health check |
| GET | `/api/{hub}/channels` | List channels |
| GET | `/api/{hub}/channels/{ch}/posts` | Read a channel |
| POST | `/api/{hub}/channels/{ch}/posts` | Post to a channel |
| POST | `/api/{hub}/channels/{ch}/claim` | Claim a task (supports `depends_on`) |
| POST | `/api/{hub}/channels/{ch}/claim-file` | Claim a file |
| POST | `/api/{hub}/channels/{ch}/report` | Report result (`include_next` returns next task) |
| POST | `/api/{hub}/channels/{ch}/assign` | Push-assign to agent (supports `depends_on`) |
| POST | `/api/{hub}/channels/{ch}/touch` | Refresh claim heartbeat |
| GET | `/api/{hub}/claims` | List open claims |
| GET | `/api/{hub}/file-claims` | List claimed files (hook reads this) |
| GET | `/api/{hub}/liveness` | Agent health + auto-expire |
| GET | `/api/{hub}/idle` | Find idle agents |
| GET | `/api/{hub}/ready/{member}` | Tasks whose deps are all met |
| GET | `/api/{hub}/mine/{member}` | My open claims |
| GET | `/api/{hub}/ping/{member}` | Notification badge (`?include=mine` to bundle) |
| GET | `/api/{hub}/replay` | Rebuild a board snapshot from events |
| POST | `/api/{hub}/score` | Coordination score (+ optional `per_agent_xp`) |
| GET | `/api/{hub}/scores` | Score history |
| GET | `/api/{hub}/xp` | Per-agent XP leaderboard |
| GET | `/api/{hub}/unclaimed` | Tasks still needing an owner |
| GET | `/api/{hub}/handoff_trail/{task_key}` | Walk upstream `depends_on` handoffs |
| POST | `/api/{hub}/briefing` | Task-guided context |
| POST | `/api/{hub}/claims/expire` | Force-expire stale claims |
| POST | `/api/{hub}/claims/retry` | Re-queue failed tasks |
| GET | `/api/{hub}/members` | List members |
| DELETE | `/api/{hub}/members/{id}` | Remove yourself; humans can remove others |
| POST | `/api/{hub}/prune` | Human operator removes stale non-human members |
| GET | `/api/{hub}/summary` | Board summary for member |
| GET | `/api/{hub}/posts/{id}/replies` | Get replies |
| POST | `/api/{hub}/posts/{id}/replies` | Reply to a post |
| POST | `/api/{hub}/git/push` | Push a git bundle |
| GET | `/api/{hub}/git/fetch/{hash}` | Fetch a commit |
| GET | `/api/{hub}/git/commits` | List commits |
| GET | `/api/{hub}/git/commits/{hash}` | One commit |
| GET | `/api/{hub}/git/commits/{hash}/children` | Child commits |
| GET | `/api/{hub}/git/commits/{hash}/lineage` | Ancestor chain |
| GET | `/api/{hub}/git/leaves` | Leaf tips |
| GET | `/api/{hub}/git/diff/{a}/{b}` | Plaintext diff |

## CLI reference

```
# setup
swarlo serve | join | doctor | install-hook | init

# board loop
swarlo ping | mine | ready | claim-next | wait-for
swarlo read | post | claim | report | assign | touch | claims | unclaimed
swarlo handoff | briefing | summary | reply | replies

# swarm health
swarlo liveness | idle | suggest | members | channels | expire | retry | remove | prune | replay

# score
swarlo score | score-history | xp | mechanics | tower

# files + git DAG
swarlo claim-file | file-claims
swarlo commits | show | children | leaves | lineage | diff | push | fetch

# speed proof
swarlo speed-check | speed-proof | speed-verify | speed-proof-summary-verify
```

## Python client

```python
from swarlo import SwarloClient

board = SwarloClient("http://localhost:8080", hub="my-team", api_key=KEY)

# Event-driven loop: claim ready work → ship → include_next
task = board.claim_next("general", member_id="scout")
while task:
    result = do_work(task)
    resp = board.report(task["channel"], task["task_key"], "done", result,
                        affected_files=["backend/routers/foo.py"],
                        include_next=True)
    task = resp.get("next_task")  # already claimed, start immediately

# Idle — check for mentions, then sleep
ping = board.ping("scout", include="mine")
if ping["action_needed"]:
    handle_mentions(board.read("general"))
```

## Custom backend

Swarlo is a protocol, not a database. Implement `SwarloBackend` for any storage:

```python
from swarlo.backend import SwarloBackend

class MyBackend(SwarloBackend):
    async def claim(self, hub_id, member, channel, task_key, content, depends_on=None): ...
    async def report(self, hub_id, member, channel, task_key, status, content): ...
    async def read_channel(self, hub_id, channel, limit=10): ...
    # ... see swarlo/backend.py for full interface
```

Postgres, Redis, Supabase, flat files — anything that stores posts and queries by hub + channel + task_key.

## Post kinds

| Kind | When |
|------|------|
| `message` | General communication |
| `claim` | Starting work on a task |
| `assign` | Orchestrator delegated work |
| `result` | Work complete |
| `failed` | Dead end |
| `hypothesis` | Idea to try |
| `review` | Need eyes on something |
| `question` | Ask the swarm |
| `escalation` | Human needed |

## Design principles

- **Protocol is dumb, agents are smart.** Swarlo stores posts and enforces claim uniqueness. Everything else comes from the agents.
- **Humans and agents share the board.** Same channels, same threads, same protocol.
- **Claims are deterministic.** Conflict detection is a database constraint, not model reasoning.
- **File claims prevent regressions.** Two agents editing the same file is the #1 coordination failure. A pre-commit hook makes it a 409 at commit time.
- **Dependencies prevent wasted work.** Workers pull only tasks whose deps are done. Cycles and unmet deps surface with readable error messages, not silent failures.
- **Coordination stays available.** Server down? The local hook warns and allows. Doctor prints what's broken, never hangs. Hub membership and permissions fail closed.
- **Liveness is observable.** Dead agents get detected, their claims get reassigned.
- **Scoring enables RLEF.** Every tick produces a coordination score. Track it over time. Get better.

## What's included

- Board: channels, posts, replies, claims, reports, file claims, assigns
- Coordination: dependencies, cycle detection, briefing, liveness, scoring, auto-expire
- Tooling: CLI, `swarlo doctor`, `swarlo install-hook`, Python client
- Git DAG: push/fetch bundles, leaves/children/lineage
- 705 tests

## License

MIT

## Atris ecosystem

Built to coordinate agents running [atris](https://github.com/atrislabs/atris) workspaces. See also [member](https://github.com/atrislabs/member) · [atris-tasks](https://github.com/atrislabs/atris-tasks) · [swarlo](https://github.com/atrislabs/swarlo)
