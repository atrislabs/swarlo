"""Ensure `import swarlo` in tests resolves to this tree, not an installed copy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
