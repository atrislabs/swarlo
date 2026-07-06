"""A good mutation that dedupes open claims in the summary."""

from pathlib import Path
import os


TARGET = Path(os.environ["EXPERIMENT_TARGET"])
START = "    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str:\n"
END = "\n    # ── DAG"
FIXED_METHOD = '''    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? ORDER BY created_at DESC LIMIT ?",
            (hub_id, limit * 3),
        ).fetchall()

        lines = []
        open_claims = []
        count = 0
        for r in rows:
            kind = r["kind"]
            name = r["member_name"]
            ch = r["channel"]
            content = r["content"][:150].replace("\\n", " ")

            if kind == "claim" and r["status"] == "open":
                open_claims.append(f"  - {name}: {content}")
                continue

            kind_tag = kind.upper() if kind in ("claim", "result", "failed", "escalation") else ""
            lines.append(f"  #{ch} {name}: {kind_tag + ' ' if kind_tag else ''}{content}")
            count += 1
            if count >= limit:
                break

        if not lines and not open_claims:
            return ""

        parts = ["\\nFLEET BOARD (Swarlo):"]
        parts.extend(lines)
        if open_claims:
            parts.append("\\nOPEN CLAIMS (do not duplicate):")
            parts.extend(open_claims)

        return "\\n".join(parts)
'''


text = TARGET.read_text(encoding="utf-8")
start = text.index(START)
end = text.index(END, start)
TARGET.write_text(text[:start] + FIXED_METHOD + text[end:], encoding="utf-8")

print("applied good summary-quality proposal")
