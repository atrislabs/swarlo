"""A good mutation that makes task_key conflicts hub-wide."""

from pathlib import Path
import os


TARGET = Path(os.environ["EXPERIMENT_TARGET"])
CLAIM_START = "    async def claim(self, hub_id: str, member: Member, channel: str,\n"
CLAIM_END = "\n    async def get_open_claims"
FIXED_METHODS = '''    async def claim(self, hub_id: str, member: Member, channel: str,
                    task_key: str, content: str) -> ClaimResult:
        existing = await self.get_open_claims(hub_id, task_key=task_key)
        if existing:
            return ClaimResult(
                claimed=False, conflict=True,
                existing_claim=existing[0],
                message=f"Already claimed by {existing[0].member_name}",
            )
        post = await self.create_post(hub_id, member, channel, content,
                                      kind="claim", task_key=task_key, status="open")
        return ClaimResult(
            claimed=True, conflict=False,
            post_id=post.post_id, channel=channel, kind="claim",
        )

    async def report(self, hub_id: str, member: Member, channel: str,
                     task_key: str, status: str, content: str,
                     parent_id: str | None = None) -> Post:
        existing = await self.get_open_claims(hub_id, task_key=task_key)
        if existing and existing[0].member_id != member.member_id:
            raise PermissionError(
                f"Task {task_key} is claimed by {existing[0].member_name}"
            )

        kind = "result" if status == "done" else "failed"
        post = await self.create_post(hub_id, member, channel, content,
                                      kind=kind, task_key=task_key, status=status)
        self.conn.execute(
            "UPDATE posts SET status = ? WHERE hub_id = ? AND task_key = ? AND kind = 'claim' AND status = 'open' AND member_id = ?",
            (status, hub_id, task_key, member.member_id),
        )
        self.conn.commit()

        if parent_id:
            await self.reply(hub_id, member, parent_id, content)

        return post
'''


text = TARGET.read_text(encoding="utf-8")
start = text.index(CLAIM_START)
end = text.index(CLAIM_END, start)
TARGET.write_text(text[:start] + FIXED_METHODS + text[end:], encoding="utf-8")

print("applied good claim-scope proposal")
