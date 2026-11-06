"""Who can answer an approval, how it closes, and what a webhook says (FR-HUM-04, 05, 07)."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest

from aios.core.catalog import ApprovalRequested
from aios.core.errors import (
    ApprovalClosedError,
    ApprovalForbiddenError,
    ApprovalRequiredError,
    NotFoundError,
)
from aios.core.human import Approval, selected
from aios.core.identity import Identity
from aios.core.ids import ulid_for
from aios.core.ports import CallSite, NoApprovals, Proposal
from aios.fabric import InMemoryEventBus, Scope
from aios.human import ApprovalWebhook, HumanService, InMemoryApprovalStore, signature
from aios.human.service import PRINCIPAL, _approval_id  # pyright: ignore[reportPrivateUsage]
from aios.human.webhook import notice
from aios.testing import make_event, task_request
from aios.testing.human import ALICE, LEAD, request

LATER = datetime.now(UTC) + timedelta(hours=1)
THE_LEAD = Identity(LEAD, "acme", frozenset({"member", "lead"}))


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus(validate_payloads=True) as bus:
        yield bus


@pytest.fixture
def human(bus: InMemoryEventBus) -> HumanService:
    return HumanService(bus, InMemoryApprovalStore(bus))


async def _pending(human: HumanService, **fields: Any) -> Approval:
    approval = Approval.of(request(expires_at=fields.pop("expires_at", LATER), **fields))
    await human.store.add(approval)
    return approval


async def _types(bus: InMemoryEventBus) -> list[str]:
    return [s.event.type async for s in bus.replay(Scope("*"), "approval.*")]


async def _until(read: Callable[[], Awaitable[object]], expected: object) -> None:
    async with asyncio.timeout(10):
        while True:
            if await read() == expected:
                return
            await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- who can answer


@pytest.mark.parametrize(
    ("selector", "principal", "roles", "expected"),
    [
        ("role:lead", LEAD, {"lead"}, True),
        ("role:lead", LEAD, {"member"}, False),
        ("role:lead", LEAD, set[str](), False),
        ("user:lead", LEAD, set[str](), True),
        ("user:lead@acme", LEAD, set[str](), True),
        ("user:other", LEAD, {"lead"}, False),
        ("group:leads", LEAD, {"leads"}, False),  # groups arrive in WP 3.5
    ],
)
def test_a_selector_names_a_role_or_a_user(
    selector: str, principal: str, roles: set[str], expected: bool
) -> None:
    assert selected(selector, Identity(principal, "acme", frozenset(roles))) is expected


async def test_a_named_approver_can_answer(human: HumanService, bus: InMemoryEventBus) -> None:
    approval = await _pending(human)
    decided = await human.decide(THE_LEAD, approval.id, approve=True, comment="looks right")
    assert (decided.status, decided.approved, decided.by) == ("granted", True, LEAD)
    assert decided.comment == "looks right"
    assert await _types(bus) == ["approval.granted"]


async def test_a_denial_is_recorded(human: HumanService, bus: InMemoryEventBus) -> None:
    approval = await _pending(human)
    decided = await human.decide(THE_LEAD, approval.id, approve=False)
    assert (decided.status, decided.approved) == ("denied", False)
    assert await _types(bus) == ["approval.denied"]


async def test_an_answer_keeps_the_runs_context(human: HumanService, bus: InMemoryEventBus) -> None:
    """If it took the approver's identity, the resumed task would act for the wrong person."""
    approval = await _pending(human)
    await human.decide(THE_LEAD, approval.id, approve=True)
    [stored] = [s async for s in bus.replay(Scope("*"), "approval.granted")]
    assert stored.event.principal == LEAD
    assert stored.event.onbehalfof == ALICE
    assert stored.event.runid == approval.run
    assert stored.event.causationid == approval.request.id


async def test_whoever_an_action_is_for_cant_approve_it(
    human: HumanService, bus: InMemoryEventBus
) -> None:
    """Even holding the right role (FR-HUM-04)."""
    approval = await _pending(human, onbehalfof=LEAD)
    with pytest.raises(ApprovalForbiddenError, match="whoever"):
        await human.decide(THE_LEAD, approval.id, approve=True)
    assert await _types(bus) == []


async def test_a_local_server_still_cant_approve_for_its_own_initiator(
    human: HumanService,
) -> None:
    approval = await _pending(human, onbehalfof=LEAD)
    with pytest.raises(ApprovalForbiddenError):
        await human.decide(THE_LEAD, approval.id, approve=True, any_approver=True)


async def test_someone_the_rule_doesnt_name_cant_answer(human: HumanService) -> None:
    approval = await _pending(human)
    admin = Identity("user:ada@acme", "acme", frozenset({"tenant-admin", "platform-admin"}))
    with pytest.raises(ApprovalForbiddenError, match="isn't for this caller"):
        await human.decide(admin, approval.id, approve=True)


async def test_any_approver_is_only_for_a_local_server(human: HumanService) -> None:
    approval = await _pending(human)
    anyone = Identity("user:anonymous", "acme")
    decided = await human.decide(anyone, approval.id, approve=True, any_approver=True)
    assert decided.status == "granted"


async def test_another_tenants_approval_doesnt_exist(human: HumanService) -> None:
    approval = await _pending(human)
    outsider = Identity(LEAD, "globex", frozenset({"lead"}))
    with pytest.raises(NotFoundError):
        await human.decide(outsider, approval.id, approve=True)


async def test_the_inbox_shows_what_this_caller_can_decide(human: HumanService) -> None:
    mine = await _pending(human)
    await _pending(human, approvers=("role:finance",))
    await _pending(human, onbehalfof=LEAD)
    assert [a.id for a in await human.inbox(THE_LEAD)] == [mine.id]


# --------------------------------------------------------------------------- closing


async def test_a_closed_approval_cant_be_answered(human: HumanService) -> None:
    approval = await _pending(human)
    await human.decide(THE_LEAD, approval.id, approve=True)
    with pytest.raises(ApprovalClosedError, match="already granted"):
        await human.decide(THE_LEAD, approval.id, approve=False)


async def test_two_people_answering_at_once(human: HumanService, bus: InMemoryEventBus) -> None:
    approval = await _pending(human)
    other = Identity("user:rae@acme", "acme", frozenset({"lead"}))
    results = await asyncio.gather(
        human.decide(THE_LEAD, approval.id, approve=True),
        human.decide(other, approval.id, approve=False),
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, Approval)]) == 1
    assert len([r for r in results if isinstance(r, ApprovalClosedError)]) == 1
    assert len(await _types(bus)) == 1


async def test_an_answer_that_lost_the_race_says_so(
    human: HumanService, bus: InMemoryEventBus
) -> None:
    """Both read the row as pending. Only the store can tell them apart."""
    approval = await _pending(human)
    await human.decide(THE_LEAD, approval.id, approve=True)

    async def as_it_was(tenant: str, approval_id: str) -> Approval:
        return approval

    human.store.get = as_it_was  # type: ignore[method-assign]
    with pytest.raises(ApprovalClosedError, match="a moment ago"):
        await human.decide(THE_LEAD, approval.id, approve=False)
    assert await _types(bus) == ["approval.granted"]


async def test_an_answer_after_the_time_is_refused(human: HumanService) -> None:
    """Even if the sweep hasn't run yet: otherwise the deadline depends on when it does."""
    approval = await _pending(human, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(ApprovalClosedError, match="expired"):
        await human.decide(THE_LEAD, approval.id, approve=True)


async def test_silence_means_no(human: HumanService, bus: InMemoryEventBus) -> None:
    approval = await _pending(human)
    assert await human.expire(LATER + timedelta(seconds=1)) == 1
    closed = await human.get("acme", approval.id)
    assert closed is not None
    assert (closed.status, closed.approved, closed.by) == ("expired", False, PRINCIPAL)
    [stored] = [s async for s in bus.replay(Scope("*"), "approval.expired")]
    assert stored.event.data == {"approval": approval.id, "approved": False}


async def test_only_a_rule_lets_it_through(human: HumanService, bus: InMemoryEventBus) -> None:
    approval = await _pending(human, on_timeout="approve")
    await human.expire(LATER + timedelta(seconds=1))
    closed = await human.get("acme", approval.id)
    assert closed is not None
    assert (closed.status, closed.approved) == ("expired", True)
    [stored] = [s async for s in bus.replay(Scope("*"), "approval.expired")]
    assert stored.event.data == {"approval": approval.id, "approved": True}


async def test_nothing_expires_early(human: HumanService) -> None:
    await _pending(human)
    assert await human.expire(LATER - timedelta(seconds=1)) == 0


async def test_an_ended_run_closes_its_approvals(
    human: HumanService, bus: InMemoryEventBus
) -> None:
    approval = await _pending(human)
    other = await _pending(human)
    await human.start()
    try:
        await bus.publish(
            make_event("run.cancelled", {"reason": "x"}, app="desk", run_id=approval.run)
        )
        await _until(lambda: _types(bus), ["approval.cancelled"])
    finally:
        await human.stop()
    closed = await human.get("acme", approval.id)
    untouched = await human.get("acme", other.id)
    assert closed is not None
    assert closed.status == "cancelled"
    assert untouched is not None
    assert untouched.status == "pending"


async def test_a_request_that_arrives_after_its_run_ended(
    human: HumanService, bus: InMemoryEventBus
) -> None:
    """The two come through separate groups, so the end of the run can be seen first."""
    event = request()
    ended = {"error": {"code": "x", "message": "y"}}
    await bus.publish(make_event("run.failed", ended, app="desk", run_id=event.runid))
    await human._on_request(event)  # pyright: ignore[reportPrivateUsage]
    approval = await human.get("acme", Approval.of(event).id)
    assert approval is not None
    assert approval.status == "cancelled"


async def test_a_redelivered_request_is_kept_once(human: HumanService) -> None:
    event = request()
    await human._on_request(event)  # pyright: ignore[reportPrivateUsage]
    await human._on_request(event)  # pyright: ignore[reportPrivateUsage]
    assert len(await human.find("acme", "pending")) == 1


# --------------------------------------------------------------------------- nowhere to wait


async def test_without_human_os_nobody_can_approve() -> None:
    proposal = Proposal("pay.refund.create", {}, "s", "high", ("role:lead",), ())
    site = CallSite(event=make_event(), agent="a", principal="agent:a@acme", index=0)
    with pytest.raises(ApprovalRequiredError) as raised:
        await NoApprovals().require(proposal, site)
    assert raised.value.approvers == ("role:lead",)


@pytest.mark.parametrize("step", ["refund", None])
async def test_only_a_task_of_a_step_can_wait(
    human: HumanService, bus: InMemoryEventBus, step: str | None
) -> None:
    """A subscribed ``step.completed`` carries a step too, but nothing can resume its handler."""
    proposal = Proposal("pay.refund.create", {}, "s", "high", ("role:lead",), ())
    fact = make_event("step.completed", {"step": "x"}, run_id="01J0000000000000000000000A")
    site = CallSite(event=fact, agent="a", principal="agent:a@acme", index=0, step=step)
    with pytest.raises(ApprovalRequiredError, match="only a workflow step"):
        await human.require(proposal, site)
    assert await _types(bus) == []


def test_two_tasks_get_two_approvals_even_in_the_same_millisecond() -> None:
    """Otherwise the store would keep one of them, and the other would never be asked."""
    proposal = Proposal("pay.refund.create", {"amountUSD": 1}, "s", "high", ("role:lead",), ())
    at = datetime(2026, 11, 1, tzinfo=UTC)
    tasks = [
        task_request("x", step="s").model_copy(update={"id": ulid_for(seed, at)}) for seed in "ab"
    ]
    first, second = (
        _approval_id(  # pyright: ignore[reportPrivateUsage]
            proposal,
            CallSite(event=task, agent="a", principal="agent:a@acme", index=0, step="s"),
        )
        for task in tasks
    )
    assert first != second


# --------------------------------------------------------------------------- webhook


class Receiver:
    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, incoming: httpx2.Request) -> httpx2.Response:
        self.requests.append(incoming)
        return httpx2.Response(self.statuses.pop(0) if self.statuses else 200)


def _hook(receiver: Receiver) -> ApprovalWebhook:
    transport = httpx2.MockTransport(receiver)
    return ApprovalWebhook("https://chat.example/hook", "shh", transport=transport)


async def test_a_webhook_carries_no_payload() -> None:
    """A notification goes outside: it says where to look, not what is in the approval."""
    receiver = Receiver()
    event = request(rationale="the customer is furious", payload={"card": "4242"})
    await _hook(receiver).send(event)
    [sent] = receiver.requests
    body = json.loads(sent.content)
    data = ApprovalRequested.model_validate(event.data)
    assert body["approval"] == data.approval
    assert body["link"] == f"/v1/approvals/{data.approval}"
    assert (body["type"], body["action"], body["risk"]) == (
        "approval.requested",
        "pay.refund.create",
        "high",
    )
    assert b"4242" not in sent.content
    assert b"furious" not in sent.content
    assert b"amountUSD" not in sent.content
    assert "summary" not in body


async def test_a_webhook_is_signed_over_its_time_and_body() -> None:
    receiver = Receiver()
    await _hook(receiver).send(request())
    [sent] = receiver.requests
    fields = dict(part.split("=", 1) for part in sent.headers["X-AIOS-Signature"].split(","))
    signed = f"{fields['t']}.".encode() + sent.content
    assert fields["v1"] == hmac.new(b"shh", signed, hashlib.sha256).hexdigest()
    assert signature("shh", int(fields["t"]), sent.content) == sent.headers["X-AIOS-Signature"]
    assert signature("other", int(fields["t"]), sent.content) != sent.headers["X-AIOS-Signature"]


async def test_a_refused_webhook_raises_so_it_is_tried_again() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        await _hook(Receiver(503)).send(request())


def test_a_decision_notice_says_who_needs_to_know_no_more() -> None:
    answered = make_event(
        "approval.granted", {"approval": "A" * 26, "by": LEAD}, app="desk", run_id=None
    )
    message = notice(answered)
    assert message["type"] == "approval.granted"
    assert "by" not in message


async def _delivered(receiver: Receiver, count: int) -> None:
    async def seen() -> bool:
        return len(receiver.requests) >= count

    await _until(seen, True)


async def test_a_webhook_retries_until_the_receiver_takes_it(bus: InMemoryEventBus) -> None:
    receiver = Receiver(503)
    human = HumanService(bus, InMemoryApprovalStore(bus), webhook=_hook(receiver))
    await human.start()
    try:
        await bus.publish(request())
        await _delivered(receiver, 2)
    finally:
        await human.stop()
    assert receiver.requests[0].content == receiver.requests[1].content


async def test_a_dead_webhook_doesnt_stop_the_inbox(bus: InMemoryEventBus) -> None:
    receiver = Receiver(*([503] * 10))
    human = HumanService(bus, InMemoryApprovalStore(bus), webhook=_hook(receiver))
    await human.start()
    try:
        await bus.publish(request())
        await _delivered(receiver, 1)

        async def kept() -> bool:
            return bool(await human.find("acme", "pending"))

        await _until(kept, True)
    finally:
        await human.stop()


async def test_a_sweep_that_fails_keeps_sweeping(bus: InMemoryEventBus) -> None:
    human = HumanService(bus, InMemoryApprovalStore(bus), sweep=0.01)
    calls: list[int] = []
    original = human.expire

    async def flaky(now: datetime | None = None) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database blinked")
        return await original(now)

    human.expire = flaky  # type: ignore[method-assign]
    await human.start()
    try:

        async def twice() -> bool:
            return len(calls) >= 2

        await _until(twice, True)
    finally:
        await human.stop()


def _identity(roles: set[str]) -> Identity:
    return replace(THE_LEAD, roles=frozenset(roles))


async def test_the_view_shows_an_approver_what_was_asked(human: HumanService) -> None:
    """FR-HUM-03: summary, exact payload, risk, policy, rationale and the way to the trace."""
    approval = await _pending(human, rationale="the item arrived broken", tainted=True)
    view = approval.view()
    assert view["summary"] == 'pay.refund.create(amountUSD=120, order="1001")'
    assert view["payload"] == {"amountUSD": 120, "order": "1001"}
    assert view["risk"] == "high"
    assert view["policies"] == ["refunds/approval/0"]
    assert view["rationale"] == "the item arrived broken"
    assert view["tainted"] is True
    assert view["initiator"] == ALICE
    assert view["trace"] == f"/v1/apps/desk/runs/{approval.run}/events"
    assert json.dumps(view)


def test_may_decide_needs_both_a_name_and_a_different_person() -> None:
    approval = Approval.of(request(onbehalfof=LEAD))
    assert not approval.may_decide(_identity({"lead"}))
    other = Approval.of(request(onbehalfof=ALICE))
    assert other.may_decide(_identity({"lead"}))
    assert not other.may_decide(_identity({"member"}))
