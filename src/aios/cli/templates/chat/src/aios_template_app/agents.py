"""The agent of Aios Template App."""

from pydantic import BaseModel

from aios import Context, agent


class Question(BaseModel):
    message: str


class Answer(BaseModel):
    answer: str


@agent("assistant-agent", capability="answering")
async def answer(ctx: Context, task: Question) -> Answer:
    return await ctx.model.generate(
        task="reasoning",
        system="Answer briefly and accurately.",
        messages=[ctx.user(task.message)],
        output=Answer,
    )
