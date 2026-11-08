"""Asking an App and waiting for its answer, or for what it waits for (FR-INT-01, 03; WP 3.7)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aios.controlplane.asking import answer_text, ask, outcome
from aios.core.errors import NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.ids import new_ulid
from aios.core.manifests import load_manifests
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
PEOPLE_DESK = ROOT / "tests" / "fixtures" / "apps" / "people-desk"
LEAD = Identity("user:lead@acme", "acme", frozenset({"member", "support-lead"}))
AUDITOR = Identity("user:audrey@acme", "acme", frozenset({"auditor"}))
WAITER = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: waiter, version: 0.1.0}
spec:
  entrypoints: [{type: chat, workflow: held}]
  workflows: [held]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: held, version: 0.1.0}
spec:
  output: "${reply.output}"
  steps:
    - {id: reply, type: wait, signal: customer-reply}
"""


@pytest.mark.parametrize(
    ("output", "text"),
    [
        (None, ""),
        ("plain", "plain"),
        ({"answer": "yes", "sources": []}, "yes"),
        ({"reply": "hi"}, "hi"),
        ({"message": 3, "text": "the text"}, "the text"),  # the first that is text
        ([1, 2], "```json\n[\n  1,\n  2\n]\n```"),
        ({"total": 3}, '```json\n{\n  "total": 3\n}\n```'),
    ],
)
def test_an_output_as_text(output: Any, text: str) -> None:
    assert answer_text(output) == text


async def test_a_run_says_what_it_waits_for_people_or_a_signal() -> None:
    async with LocalRuntime.from_app(PEOPLE_DESK) as runtime:
        asked = await ask(
            runtime.runtime, LEAD, "people-desk", "Refund order 1001 $120", timeout=10
        )
        assert asked.status == "waiting"
        [task] = await runtime.runtime.human.tasks.in_run("acme", asked.run_id)  # type: ignore[union-attr]
        assert asked.waiting == [f"task {task.id} (refunds.review) by role:support-lead"]
        assert asked.text.startswith(f"Run {asked.run_id} is waiting for task {task.id}")

        await runtime.runtime.apply(load_manifests(WAITER))
        held = await ask(runtime.runtime, LEAD, "waiter", "hi", timeout=10)
        assert held.waiting == ["a signal at step reply"]
        await runtime.runtime.signal(
            "waiter",
            held.run_id,
            "customer-reply",
            "thanks",
            tenant="acme",
            principal=LEAD.principal,
        )
        async with asyncio.timeout(10):  # until the run has taken the signal
            while True:
                done = await outcome(runtime.runtime, LEAD, "waiter", held.run_id, timeout=5)
                if not done.waiting:
                    break
                await asyncio.sleep(0.02)
        assert (done.status, done.text, done.output) == ("completed", "thanks", "thanks")
        assert done.view()["trace"] == f"/v1/apps/waiter/runs/{held.run_id}"


async def test_who_may_ask_and_about_what() -> None:
    async with LocalRuntime.from_app(PEOPLE_DESK) as runtime:
        with pytest.raises(PolicyDeniedError):
            await ask(runtime.runtime, AUDITOR, "people-desk", "hi", timeout=1)
        with pytest.raises(NotFoundError):
            await ask(runtime.runtime, LEAD, "nobody", "hi", timeout=1)
        with pytest.raises(NotFoundError, match="no run"):
            await outcome(runtime.runtime, AUDITOR, "people-desk", new_ulid(), timeout=1)
        stranger = Identity("user:x@acme", "acme", frozenset({"no-such-role"}))
        with pytest.raises(PolicyDeniedError):
            await outcome(runtime.runtime, stranger, "people-desk", new_ulid(), timeout=1)


async def test_a_cancelled_run_says_so() -> None:
    async with LocalRuntime.from_app(PEOPLE_DESK) as runtime:
        await runtime.runtime.apply(load_manifests(WAITER))
        held = await ask(runtime.runtime, LEAD, "waiter", "hi", timeout=10)
        await runtime.runtime.cancel("waiter", held.run_id, tenant="acme", principal=LEAD.principal)
        async with asyncio.timeout(10):
            while True:
                done = await outcome(runtime.runtime, LEAD, "waiter", held.run_id, timeout=5)
                if done.status != "waiting":
                    break
                await asyncio.sleep(0.02)
        assert done.status == "cancelled"
        assert done.text.startswith(f"Run {held.run_id} cancelled")
