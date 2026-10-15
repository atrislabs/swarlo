"""Disk hygiene for a swarlo home directory (normally ``~/.swarlo``).

rotate_logs(home, max_mb=10)
    Every ``*.log`` directly in ``home`` that is bigger than ``max_mb`` is
    copied to ``<name>.log.1`` (keeping only its newest ``max_mb`` bytes, and
    replacing any older ``.1``) and then truncated in place. Truncating rather
    than renaming keeps the inode, so daemons that hold the log open in append
    mode keep writing to the live file.

keep_backups(home, keep=3)
    Deletes all but the ``keep`` newest ``atris.db.backup*`` files directly in
    ``home``, newest by mtime (backup suffixes mix epoch and date stamps, so
    names do not sort). ``atris.db`` itself never matches the pattern.

Both return the paths they acted on.
"""

from __future__ import annotations

import os
from pathlib import Path

_CHUNK = 1 << 20


def rotate_logs(home, max_mb: float = 10) -> list[Path]:
    home = Path(home)
    cap = int(max_mb * 1024 * 1024)
    rotated = []
    for log in sorted(home.glob("*.log")):
        if not log.is_file() or log.is_symlink() or log.stat().st_size <= cap:
            continue
        backup = log.with_name(log.name + ".1")
        tmp = log.with_name(log.name + ".1.tmp")
        with open(log, "r+b") as src:
            size = os.fstat(src.fileno()).st_size
            src.seek(max(0, size - cap))
            with open(tmp, "wb") as dst:
                while chunk := src.read(_CHUNK):
                    dst.write(chunk)
            src.truncate(0)
        os.replace(tmp, backup)
        rotated.append(log)
    return rotated


def keep_backups(home, keep: int = 3) -> list[Path]:
    home = Path(home)
    backups = [p for p in home.glob("atris.db.backup*") if p.is_file() and not p.is_symlink()]
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = backups[max(0, keep):]
    for p in removed:
        p.unlink()
    return removed
