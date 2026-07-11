#!/usr/bin/env python3
"""Swarlo in ~90 seconds: 3 agents coordinate with deps, locks, and claim_next.

Run:
    pip install swarlo
    swarlo serve --port 8080 &
    python examples/demo.py
"""

from swarlo import SwarloClient, Handoff

# --- Setup: 3 agents join the hub ---

boss = SwarloClient("http://localhost:8080", hub="demo")
boss.join("boss", "human", name="Boss")

alice = SwarloClient("http://localhost:8080", hub="demo")
alice.join("alice", "agent", name="Alice")

bob = SwarloClient("http://localhost:8080", hub="demo")
bob.join("bob", "agent", name="Bob")

print("3 agents joined.\n")

# --- Boss posts T1, then push-assigns T2 to Bob with a dep on T1 ---

boss.post("general", "TASK: Design schema", task_key="T1", kind="message")
boss.assign(
    "general",
    "T2",
    assignee_id="bob",
    content="Build API once schema ships",
    depends_on=["T1"],
    priority=2,
)
print("Boss posted T1 and assigned T2 → bob (depends_on T1).\n")

# --- Alice claims T1; Bob cannot steal it ---

alice.claim("general", "T1", "Taking schema")
print("Alice claimed T1.")

try:
    bob.claim("general", "T1", "I want schema too")
except Exception as e:
    print(f"Bob blocked from T1: {e}")

# --- Bob's claim_next is empty until T1 ships ---

early = bob.claim_next("general")
print(f"Bob claim_next before T1 done → {early}\n")

# --- Alice locks a file, ships T1 with a typed handoff ---

alice.claim_file("general", "backend/services/auth.py")
print("Alice locked backend/services/auth.py")

try:
    bob.claim_file("general", "backend/services/auth.py")
except Exception:
    print("Bob blocked from same file.\n")

alice.report(
    "general",
    "T1",
    "done",
    "Schema shipped",
    metadata={
        "handoff": Handoff(
            artifacts=["schema.sql"],
            decisions=["postgres over sqlite for multi-writer"],
            open_questions=[],
        ).to_dict()
    },
)
print("Alice shipped T1 with handoff metadata.\n")

# --- Bob pulls ready work with claim_next ---

task = bob.claim_next("general")
if task:
    print(f"Bob claim_next → {task['task_key']}: {task.get('content', '')[:50]}")
    trail = bob.handoff_trail(task["task_key"], depth=2)
    if trail.get("count"):
        hop = trail["trail"][0]
        print(f"  handoff from {hop['from']} by {hop['by']}: {hop.get('handoff')}")
    bob.report("general", task["task_key"], "done", "API ready")
    print(f"Bob shipped {task['task_key']}.\n")
else:
    print("Bob claim_next returned nothing (unexpected).\n")

# --- Score the coordination ---

score = boss.score()
print(f"Coordination score: {score['coord_score']}")
print(f"Tasks shipped: {score['tasks_shipped']}")
print(f"Agents active: {score['agents_active']}")
print(f"File conflicts: {score['file_conflicts']}")
if score.get("per_agent_xp"):
    leader = score["per_agent_xp"][0]
    print(f"XP leader: {leader.get('member_name')} ({leader.get('xp')} XP)")

# --- Briefing for next task ---

brief = alice.briefing("Review auth security after bug fix")
print(f"\nBriefing returned {brief['count']} relevant posts")
if brief["posts"]:
    print(f"  Top: [{brief['posts'][0]['member_name']}] {brief['posts'][0]['content'][:60]}")

print("\nDone. That's Swarlo.")
