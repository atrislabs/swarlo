# Program

Improve `swarlo/sqlite_backend.py` so `task_key` uniqueness is enforced across the whole hub, not just within one channel. Keep claim conflict semantics deterministic, block foreign cross-channel reports on another member's open task, and preserve one open claim for the original owner.
