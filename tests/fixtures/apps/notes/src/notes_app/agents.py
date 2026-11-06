"""The notes App's one agent. Which of its tools it may really use is the tenant's choice."""

from __future__ import annotations

from typing import Any

from aios import Context

VERSION = "1.0.0"


async def work(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    words = str(task.get("message") or "").split(maxsplit=1)
    action = words[0].lower() if words else "hello"
    if action == "write":
        text = words[1] if len(words) > 1 else ""
        await ctx.tools.invoke("filesystem.file.write", path="notes.txt", content=text)
        return {"version": VERSION, "done": "written"}
    if action == "read":
        return {
            "version": VERSION,
            "notes": await ctx.tools.invoke("filesystem.file.read", path="notes.txt"),
        }
    return {"version": VERSION, "greeting": task.get("greeting")}
