"""A bad mutation that drops useful board context."""

from pathlib import Path
import os


TARGET = Path(os.environ["EXPERIMENT_TARGET"])
START = "    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str:\n"
END = "\n    # ── DAG"
BAD_METHOD = '''    async def summarize_for_member(self, hub_id: str, member_id: str, limit: int = 10) -> str:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE hub_id = ? ORDER BY created_at DESC LIMIT ?",
            (hub_id, limit * 3),
        ).fetchall()

        open_claims = []
        for r in rows:
            if r["kind"] == "claim" and r["status"] == "open":
                content = r["content"][:150].replace("\\n", " ")
                open_claims.append(f"  - {r['member_name']}: {content}")

        if not open_claims:
            return ""

        parts = ["\\nFLEET BOARD (Swarlo):", "\\nOPEN CLAIMS (do not duplicate):"]
        parts.extend(open_claims)
        return "\\n".join(parts)
'''


text = TARGET.read_text(encoding="utf-8")
start = text.index(START)
end = text.index(END, start)
TARGET.write_text(text[:start] + BAD_METHOD + text[end:], encoding="utf-8")

print("applied bad summary-quality proposal")
