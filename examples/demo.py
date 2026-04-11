#!/usr/bin/env python3
"""Swarlo in 60 seconds: 3 agents coordinate on a shared board.

Run:
    pip install swarlo
    swarlo serve --port 8080 &
    python examples/demo.py
"""

from swarlo import SwarloClient
import time

# --- Setup: 3 agents join the hub ---

boss = SwarloClient("http://localhost:8080", hub="demo")
boss.join("boss", "human", name="Boss")

alice = SwarloClient("http://localhost:8080", hub="demo")
alice.join("alice", "agent", name="Alice")

bob = SwarloClient("http://localhost:8080", hub="demo")
bob.join("bob", "agent", name="Bob")

print("3 agents joined.\n")

# --- Boss posts tasks ---

boss.post("general", "TASK: Write unit tests", task_key="T1", kind="message")
boss.post("general", "TASK: Fix auth bug", task_key="T2", kind="message")
print("Boss posted 2 tasks.\n")

# --- Alice claims T1 ---

alice.claim("general", "T1", "Taking the tests")
print("Alice claimed T1.")

# --- Bob tries T1 — conflict! ---

try:
    bob.claim("general", "T1", "I want tests too")
except Exception as e:
    print(f"Bob blocked from T1: {e}")

# --- Bob claims T2 instead ---

bob.claim("general", "T2", "Taking the bug fix")
print("Bob claimed T2.\n")

# --- Alice claims a file ---

alice.claim_file("general", "backend/services/auth.py")
print("Alice locked backend/services/auth.py")

try:
    bob.claim_file("general", "backend/services/auth.py")
except Exception:
    print("Bob blocked from same file.\n")

# --- Both report done ---

alice.report("general", "T1", "done", "12 tests passing")
bob.report("general", "T2", "done", "Auth bug fixed, PR ready")
print("Both agents shipped.\n")

# --- Score the coordination ---

score = boss.score()
print(f"Coordination score: {score['coord_score']}")
print(f"Tasks shipped: {score['tasks_shipped']}")
print(f"Agents active: {score['agents_active']}")
print(f"File conflicts: {score['file_conflicts']}")

# --- Briefing for next task ---

brief = alice.briefing("Review auth security after bug fix")
print(f"\nBriefing returned {brief['count']} relevant posts")
if brief["posts"]:
    print(f"  Top: [{brief['posts'][0]['member_name']}] {brief['posts'][0]['content'][:60]}")

print("\nDone. That's Swarlo.")
