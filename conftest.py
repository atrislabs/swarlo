"""Ensure `import swarlo` in tests resolves to this tree, not an installed copy."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ambient SWARLO_* env vars (per-process identity, shared config overrides)
# must not leak into tests: credential resolution reads them before config.
for _var in (
    "SWARLO_CONFIG",
    "SWARLO_TOKEN",
    "SWARLO_API_KEY",
    "SWARLO_MEMBER_ID",
    "SWARLO_MEMBER_NAME",
    "SWARLO_SERVER",
    "SWARLO_HUB",
    "SWARLO_INVITE_TOKEN",
    "SWARLO_HOOK_AUTO_CLAIM",
):
    os.environ.pop(_var, None)
