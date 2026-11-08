"""An OpenAI-compatible chat endpoint for AIOS Apps (FR-INT-03, FR-APP-02; WP 3.7).

Point an OpenAI client at ``https://<aios>/openai/v1``, with an AIOS API key as its key, and use
an App's name as the model:

    client = OpenAI(base_url="https://aios.example.com/openai/v1", api_key=AIOS_API_KEY)
    reply = client.chat.completions.create(model="ai-researcher", messages=[...])

Each request is a run of the App's chat entry point, for the caller: their roles, the App's
policies, budgets and approvals apply, and the run is traced like any other. The last user message
is the run's ``message``, and the whole conversation is its ``messages``. The reply is the run's
answer. A run that waits for a person, or takes longer than the server waits, comes back as a
message that says so, with the run's id; the run goes on. ``stream: true`` sends the answer when
it is there, with keep-alive comments meanwhile, since an App answers whole.

An App answers with its own tools, so a request that offers the client's tools is refused rather
than quietly ignored. ``X-AIOS-Session`` joins an earlier run's conversation and
``X-AIOS-Data-Class`` labels the run, as ``session`` and ``data_class`` do on ``/v1``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from aios.controlplane import Runtime
from aios.controlplane.asking import Asked, chat_apps, outcome, start
from aios.core.envelope import DataClass
from aios.core.errors import NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.ids import is_ulid
from aios.core.policy import PolicyAction

KEEPALIVE_SECONDS = 10.0
"""A comment this often while a streamed run works, so that proxies keep the connection."""


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatRequest(BaseModel):
    """The fields AIOS reads; OpenAI's others (temperature and so on) are the App's to choose."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, description="The App's name")
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    tools: list[Any] | None = None


class Refused(Exception):
    """An answer in OpenAI's error shape."""

    def __init__(self, status: int, message: str, kind: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.kind, self.code = status, message, kind, code

    def response(self) -> JSONResponse:
        body = {
            "error": {"message": self.message, "type": self.kind, "param": None, "code": self.code}
        }
        headers = {"WWW-Authenticate": "Bearer"} if self.status == 401 else None
        return JSONResponse(body, status_code=self.status, headers=headers)


def _refused(exc: HTTPException) -> Refused:
    kind = {401: "authentication_error", 403: "permission_error"}.get(
        exc.status_code, "invalid_request_error"
    )
    return Refused(exc.status_code, str(exc.detail), kind)


def _text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None or isinstance(content, str):
        return content or ""
    return "".join(str(part.get("text", "")) for part in content if part.get("type") == "text")


def openai_routes(runtime: Runtime, *, wait_seconds: float) -> APIRouter:  # noqa: PLR0915
    router = APIRouter(prefix="/openai/v1", tags=["openai"])

    async def caller(request: Request, action: PolicyAction, app: str | None = None) -> Identity:
        gate = request.app.state.gate
        try:
            identity: Identity = await gate.identify(
                request.headers.get("authorization"), request.headers.get("x-aios-tenant")
            )
        except HTTPException as exc:
            raise _refused(exc) from exc
        decision = await runtime.authorize(identity, action, **({"app": app} if app else {}))
        if not decision.allowed:
            raise Refused(
                403, decision.reason or f"{action.value} is not allowed", "permission_error"
            )
        return identity

    def chat_app(tenant: str, name: str) -> None:
        if name not in chat_apps(runtime, tenant):
            raise Refused(
                404, f"no App {name!r} to chat with", "invalid_request_error", "model_not_found"
            )

    @router.get("/models")
    async def models(request: Request) -> Any:
        """The Apps the caller can chat with, as models."""
        try:
            identity = await caller(request, PolicyAction.APP_READ)
        except Refused as refused:
            return refused.response()
        return {
            "object": "list",
            "data": [_model(name) for name in chat_apps(runtime, identity.tenant)],
        }

    @router.get("/models/{model}")
    async def model(model: str, request: Request) -> Any:
        """One App the caller can chat with, as a model."""
        try:
            identity = await caller(request, PolicyAction.APP_READ)
            chat_app(identity.tenant, model)
        except Refused as refused:
            return refused.response()
        return _model(model)

    @router.post("/chat/completions", response_model=None)
    async def completions(request: Request) -> Any:
        """A chat completion: a run of the App named as the model, and its answer."""
        try:
            return await completion(request)
        except Refused as refused:
            return refused.response()

    async def completion(request: Request) -> Any:
        try:
            asked_for = ChatRequest.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            await caller(request, PolicyAction.APP_READ)  # who is asking comes first
            raise Refused(400, f"not a chat completion: {exc}", "invalid_request_error") from exc
        app = asked_for.model
        identity = await caller(request, PolicyAction.RUN_START, app)
        chat_app(identity.tenant, app)
        if asked_for.tools:
            raise Refused(
                400,
                "an AIOS App answers with its own tools; it doesn't call the client's",
                "invalid_request_error",
                "tools_not_supported",
            )
        users = [m for m in asked_for.messages if m.role == "user"]
        if not users:
            raise Refused(400, "there is no user message to answer", "invalid_request_error")
        message = _text(users[-1].content)
        history: list[JsonValue] = [
            {"role": m.role, "content": _text(m.content)} for m in asked_for.messages
        ]
        session = request.headers.get("x-aios-session")
        if session is not None and not is_ulid(session):
            raise Refused(400, "X-AIOS-Session is a run's session id", "invalid_request_error")
        try:
            data_class = DataClass(request.headers.get("x-aios-data-class", "internal"))
        except ValueError as exc:
            choices = ", ".join(c.value for c in DataClass)
            raise Refused(
                400, f"X-AIOS-Data-Class is one of {choices}", "invalid_request_error"
            ) from exc
        try:
            run_id = await start(
                runtime,
                identity,
                app,
                message,
                input={"message": message, "messages": history},
                session=session,
                data_class=data_class,
                traceparent=request.headers.get("traceparent"),
                via="openai",
                authorized=True,
            )
        except PolicyDeniedError as exc:
            raise Refused(403, str(exc), "permission_error") from exc
        except NotFoundError as exc:
            raise Refused(404, str(exc), "invalid_request_error", "model_not_found") from exc
        answer = outcome(runtime, identity, app, run_id, timeout=wait_seconds, authorized=True)
        headers = {"X-AIOS-Run": run_id}
        if asked_for.stream:
            include_usage = bool((asked_for.stream_options or {}).get("include_usage"))
            stream = _streamed(app, run_id, asyncio.ensure_future(answer), include_usage)
            return StreamingResponse(stream, media_type="text/event-stream", headers=headers)
        asked = await answer
        if asked.status in ("failed", "cancelled"):
            code = str((asked.error or {}).get("code") or asked.status)
            raise Refused(502, asked.text, "server_error", code)
        return JSONResponse(_completion(asked), headers=headers)

    return router


def _model(name: str) -> dict[str, Any]:
    return {"id": name, "object": "model", "created": 0, "owned_by": "aios"}


def _usage(asked: Asked) -> dict[str, int]:
    prompt = int(asked.usage.get("input_tokens") or 0)
    completion = int(asked.usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _aios(asked: Asked) -> dict[str, Any]:
    """What OpenAI's shape has no room for: the run, and how it stands."""
    return {"run_id": asked.run_id, "status": asked.status, "trace": asked.trace}


def _completion(asked: Asked) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{asked.run_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": asked.app,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": asked.text},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage(asked),
        "aios": _aios(asked),
    }


def _data(body: dict[str, Any]) -> str:
    return f"data: {json.dumps(body)}\n\n"


def _chunk(app: str, run_id: str, choices: list[dict[str, Any]], **more: Any) -> str:
    return _data(
        {
            "id": f"chatcmpl-{run_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": app,
            "choices": choices,
            **more,
        }
    )


async def _streamed(
    app: str, run_id: str, answer: asyncio.Future[Asked], include_usage: bool
) -> AsyncIterator[str]:
    try:
        yield _chunk(app, run_id, [{"index": 0, "delta": {"role": "assistant", "content": ""}}])
        while not (await asyncio.wait({answer}, timeout=KEEPALIVE_SECONDS))[0]:
            yield ": the run is still working\n\n"  # a comment, which clients skip
        asked = answer.result()
        if asked.status in ("failed", "cancelled"):
            error = {"message": asked.text, "type": "server_error", "code": asked.status}
            yield _data({"error": error})
            return
        yield _chunk(app, run_id, [{"index": 0, "delta": {"content": asked.text}}])
        yield _chunk(
            app, run_id, [{"index": 0, "delta": {}, "finish_reason": "stop"}], aios=_aios(asked)
        )
        if include_usage:
            yield _chunk(app, run_id, [], usage=_usage(asked))
        yield "data: [DONE]\n\n"
    finally:
        answer.cancel()  # the client left; the run goes on without it


__all__ = ["openai_routes"]
