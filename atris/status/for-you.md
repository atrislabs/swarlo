# For You

## DECISION: let the repeat-fold check actually run
- blocked: the mission's check runs plain `python3`, which on this Mac is Homebrew Python 3.15 with no test tools installed, so it fails before running a single test. The feature itself is done: the same tests pass (163 of 163) under a Python that has the tools.
- why I didn't fix it: the fix is either installing packages into your system Python (outside this repo, and it overrides Homebrew's guard against that) or changing how the harness launches the check (tool policy). Neither is mine to self-land.
- option A (one command, reversible with pip uninstall): `/opt/homebrew/opt/python@3.15/bin/python3.15 -m pip install --user --break-system-packages pytest pytest-asyncio fastapi uvicorn httpx`
- option B (cleaner): have the harness run this repo's checks with `.venv/bin` first on PATH. Caveat: `.venv` currently borrows pytest from atrisos-backend's 3.12 venv through a hidden path link, so it's fragile too.
- reply: A / B / other
