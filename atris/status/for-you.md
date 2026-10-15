# For you

## DECISION: let the campaign bridge check run on this Mac's default Python
- what: the campaign bridge is built and its 18 tests pass, but its frozen check runs plain `python3`. Here that's Homebrew Python 3.15, which has no test tools, so the check fails before any test runs ("No module named pytest", exit 1). With the repo's own `.venv` it passes (18 passed, exit 0). I can't change the frozen check, and installing into your global Python is outside my lane, so it's your call.
- option A (one command, reversible): install the test tools into your user-level Python 3.15. Dry run confirms all 19 packages have 3.15 wheels (pytest 9.1.1, fastapi 0.143.1, uvicorn 0.54.0, pydantic 2.14.1 + compiled core). This touches your user site-packages only:
  `python3 -m pip install --user --break-system-packages pytest fastapi uvicorn`
  undo: `python3 -m pip uninstall --break-system-packages pytest fastapi uvicorn`
- option B (no machine change): revise the mission's check to use the repo venv:
  `.venv/bin/python3 -m pytest -q tests/test_campaign_bridge.py && .venv/bin/python3 -c 'import swarlo.campaign_bridge'`
- also pending: the bridge commit is on local main, 1 ahead of origin, not pushed (campaign rules: no remote push without asking). Reply "push" to land it.
- recommend: B. It keeps your global Python clean and matches how the rest of the suite already runs.
- reply: A / B, and push / hold

## DECISION: the hub-restart check asks for a page the hub has never had
- what: the live board on port 8090 is already running the new repeat-folding code. It was restarted at 04:51:36 PT, one second after that change was committed, and a repeat status post folded right after. I posted a result to the board saying so. The mission's frozen check still fails: its last step loads the hub's front page (`/`), and the hub has never served one (404). Its health page (`/api/health`) returns 200. I can't change the frozen check.
- option A (no code change): revise the check's last step to `curl -sf http://localhost:8090/api/health >/dev/null`. The rest of the check already passes.
- option B (code + another restart): add a small front page to the hub that returns 200, commit it, and restart the hub again. That briefly disconnects the agents on the board, and the restart has to come after the commit so the start-time check still passes.
- recommend: A. The hub is already doing what the mission wanted, and B restarts a live board only to satisfy the check.
- reply: A / B
