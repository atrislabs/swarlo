"""Hub data audit — health check + cleanup for a swarlo SQLite hub.

`swarlo doctor` checks *client* setup (config file, server reachability,
git hook). `swarlo audit` checks *hub data*: cross-hub leakage, repeated
nudge spam, stale lock files, orphan keys, dormant fleet members.

Reads SQLite directly. With --fix, backs up the DB, prunes the safe
subset of findings, and runs VACUUM. Informational findings are surfaced
but never auto-resolved — they require human judgment.

Origin: atris production hit 663 false-alarm orchestrator nudges over
24 days from a single off-by-one git ref. This formalizes the cleanup
pattern so any deployment can self-diagnose before the noise drowns
real signal.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Finding:
    code: str
    severity: str  # "high" | "medium" | "low" | "info"
    msg: str
    detail: list = field(default_factory=list)


@dataclass
class FixAction:
    label: str
    sql: str | None = None
    rm_path: str | None = None  # if set, remove this filesystem path instead of running SQL


def _detect_findings(db_path: Path, hub_id: str, key_dir: Path,
                     lock_path: Path) -> tuple[list[Finding], list[FixAction]]:
    """Run all checks against the live DB. Returns (findings, fixable actions)."""
    findings: list[Finding] = []
    fixable: list[FixAction] = []

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # 1. Cross-hub data — violates SINGLE HUB RULE
    cur.execute("SELECT hub_id, COUNT(*) FROM members WHERE hub_id != ? GROUP BY hub_id", (hub_id,))
    bad_member_hubs = cur.fetchall()
    cur.execute("SELECT hub_id, COUNT(*) FROM posts WHERE hub_id != ? GROUP BY hub_id", (hub_id,))
    bad_post_hubs = cur.fetchall()
    if bad_member_hubs or bad_post_hubs:
        findings.append(Finding(
            code="single-hub",
            severity="high",
            msg=f"non-'{hub_id}' hub data: members={bad_member_hubs} posts={bad_post_hubs}",
        ))
        fixable.append(FixAction(
            label="single-hub",
            sql=f"DELETE FROM members WHERE hub_id != '{hub_id}'; "
                f"DELETE FROM posts   WHERE hub_id != '{hub_id}';",
        ))

    # 2. False-alarm nudges — same content posted >5x in 7 days
    cur.execute(
        "SELECT member_name, substr(content,1,80), COUNT(*) c "
        "FROM posts WHERE kind='nudge' AND created_at > datetime('now','-7 days') "
        "GROUP BY member_name, substr(content,1,80) HAVING c > 5 ORDER BY c DESC LIMIT 5"
    )
    spammy = cur.fetchall()
    if spammy:
        findings.append(Finding(
            code="false-alarm-nudges",
            severity="medium",
            msg=f"repeated nudge content (top: {spammy[0][0]} x{spammy[0][2]})",
            detail=list(spammy),
        ))
        for member, content_prefix, _count in spammy:
            content_prefix_esc = content_prefix.replace("'", "''")
            fixable.append(FixAction(
                label="false-alarm-nudges",
                sql=f"DELETE FROM posts WHERE kind='nudge' AND member_name='{member}' "
                    f"AND substr(content,1,80)='{content_prefix_esc}';",
            ))

    # 3. Stale lock — daemon died, lock not cleared
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip())
            os.kill(pid, 0)  # liveness check only
        except (ValueError, ProcessLookupError, PermissionError):
            findings.append(Finding(
                code="stale-lock",
                severity="low",
                msg=f"lock file {lock_path} points at non-running PID",
            ))
            fixable.append(FixAction(label="stale-lock", rm_path=str(lock_path)))
        except OSError:
            pass

    # 4. Orphan keys — key files with no matching member row
    cur.execute("SELECT member_id FROM members WHERE hub_id = ?", (hub_id,))
    valid_ids = {row[0] for row in cur.fetchall()}
    orphan_keys: list[str] = []
    if key_dir.exists():
        for kf in key_dir.glob("*.key"):
            bn = kf.stem
            if bn not in valid_ids and f"atris-{bn}" not in valid_ids:
                orphan_keys.append(str(kf))
    if orphan_keys:
        findings.append(Finding(
            code="orphan-keys",
            severity="low",
            msg=f"{len(orphan_keys)} key file(s) have no matching member row",
            detail=orphan_keys[:5],
        ))

    # 5. Dormant fleet — registered but never posted, or 30d+ idle
    cur.execute(
        "SELECT COUNT(*) FROM members m WHERE hub_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM posts p WHERE p.member_id = m.member_id AND p.hub_id = m.hub_id)",
        (hub_id,),
    )
    never_posted = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM members m WHERE hub_id = ? AND EXISTS ("
        "  SELECT 1 FROM posts p WHERE p.member_id = m.member_id AND p.hub_id = m.hub_id "
        "  GROUP BY p.member_id HAVING MAX(p.created_at) < datetime('now','-30 days')"
        ")",
        (hub_id,),
    )
    idle_30d = cur.fetchone()[0]
    if never_posted or idle_30d:
        findings.append(Finding(
            code="dormant-fleet",
            severity="info",
            msg=f"{never_posted} member(s) never posted, {idle_30d} idle 30+ days",
        ))

    con.close()
    return findings, fixable


def _apply_fixes(db_path: Path, fixable: list[FixAction]) -> int:
    """Apply each fixable action. Returns count of applied fixes."""
    applied = 0
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    for action in fixable:
        if action.rm_path:
            target = Path(action.rm_path)
            try:
                target.unlink()
                print(f"  fix {action.label}: removed {target}")
                applied += 1
            except FileNotFoundError:
                pass
        elif action.sql:
            cur.executescript(action.sql)
            print(f"  fix {action.label}: applied")
            applied += 1
    con.commit()
    cur.execute("VACUUM")
    con.commit()
    con.close()
    return applied


def run_audit(db_path: Path, hub_id: str = "atris", key_dir: Path | None = None,
              lock_path: Path | None = None, fix: bool = False) -> int:
    """Top-level entry. Returns 0 if clean or all-fixed; 1 if findings remain.

    Designed to be called from CLI or programmatically. Read-only unless
    fix=True, in which case the DB is backed up to atris.db.backup-audit-<ts>
    before any deletion.
    """
    if not db_path.exists():
        print(f"swarlo audit: no db at {db_path}")
        return 1

    key_dir = key_dir or db_path.parent
    lock_path = lock_path or (db_path.parent / "atris_orchestrator.lock")

    findings, fixable = _detect_findings(db_path, hub_id, key_dir, lock_path)

    if not findings:
        print("swarlo audit: clean — no findings")
        return 0

    sev_glyph = {"high": "✗", "medium": "•", "low": "·", "info": "i"}
    print(f"swarlo audit: {len(findings)} finding(s)")
    for f in findings:
        print(f"  [{f.severity:6}] {sev_glyph.get(f.severity,'?')} {f.code}: {f.msg}")

    if not fix:
        print()
        print("re-run with --fix to apply safe remediation")
        return 1

    backup = db_path.with_suffix(f".db.backup-audit-{int(time.time())}")
    shutil.copy2(db_path, backup)
    print(f"\nbacked up db to {backup}")

    applied = _apply_fixes(db_path, fixable)
    print(f"vacuum done; {applied} fix(es) applied")
    return 0
