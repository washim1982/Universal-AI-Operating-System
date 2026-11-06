"""One agent reads, and one acts on what it was told, with no judgment at all.

The second one is what a model looks like once an injection has worked: ``Fetch <url>`` fetches,
and anything else writes a note. Nothing here is a defence. Those are all in the platform.
"""

import re
from typing import Any

from aios.core.errors import ApprovalDeniedError, ToolDeniedError
from aios.kernel import Context, agent

_URL = re.compile(r"https?://\S+")


@agent("lookup-agent", capability="kb.lookup")
async def lookup(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    message = str(task.get("message", ""))
    found = await ctx.memory.recall(message, scopes=["app"])
    return {"message": message, "read": ctx.quote(found.content, source="memory") if found else ""}


@agent("act-agent", capability="kb.act")
async def act(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    target = _URL.search(str(task.get("message", "")))
    try:
        if target is not None:
            page = await ctx.tools.invoke("http.page.fetch", url=target[0])
            return {"reply": "Fetched it.", "page": page}
        receipt = await ctx.tools.invoke(
            "filesystem.file.write",
            path=f"notes/{ctx.run_id}.txt",
            content=f"{task.get('message')}\n",
            rationale="The knowledge base said to.",
        )
    except ApprovalDeniedError as no:
        return {"reply": "I wasn't allowed to do that.", "why": no.code}
    except ToolDeniedError as no:
        return {"reply": "That was refused.", "why": no.code, "detail": str(no)}
    return {"reply": "Noted.", "receipt": receipt}
