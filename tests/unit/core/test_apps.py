"""What a tenant grants an App, and the arithmetic of consent (FR-APP-03)."""

import pytest

from aios.core.apps import Grants
from aios.core.envelope import DataClass

ALL = Grants(
    tools=["mail.send", "mail.read"],
    data_classes=[DataClass.INTERNAL, DataClass.CONFIDENTIAL],
    memory_scopes=["app", "user"],
    hosts=["docs.example.com", "wiki.example.com"],
)


@pytest.mark.parametrize(
    "more",
    [
        {"tools": ["mail.send", "mail.read", "pay.refund.create"]},
        {"data_classes": [DataClass.INTERNAL, DataClass.RESTRICTED]},
        {"memory_scopes": ["app", "global"]},
        {"hosts": ["docs.example.com", "evil.example.com"]},
    ],
)
def test_asking_for_more_of_anything_isnt_covered(more: dict[str, list[str]]) -> None:
    assert not ALL.covers(ALL.model_copy(update=more))
    assert ALL.model_copy(update=more).covers(ALL) or "tools" not in more


def test_what_was_granted_covers_itself_and_less() -> None:
    assert ALL.covers(ALL)
    assert ALL.covers(Grants(tools=["mail.read"], hosts=["docs.example.com"]))
    assert ALL.covers(Grants())


def test_a_pattern_covers_its_tools() -> None:
    assert Grants(tools=["mail.>"]).covers(Grants(tools=["mail.send", "mail.read"]))
    assert not Grants(tools=["mail.send"]).covers(Grants(tools=["mail.>"]))


def test_within_is_covers_turned_round() -> None:
    assert Grants(tools=["mail.read"]).within(ALL)
    assert not ALL.within(Grants(tools=["mail.read"]))


def test_plus_and_without() -> None:
    one = Grants(tools=["a.b.c"], hosts=["x.example.com"])
    two = Grants(tools=["d.e.f"], data_classes=[DataClass.INTERNAL])
    both = one.plus(two)
    assert both.tools == ["a.b.c", "d.e.f"]
    assert both.data_classes == [DataClass.INTERNAL]
    assert both.hosts == ["x.example.com"]
    assert both.without(two) == one
    assert ALL.without(ALL) == Grants()


def test_a_version_gets_what_it_asked_for_and_was_given() -> None:
    wanted = Grants(
        tools=["mail.read", "pay.refund.create"],
        data_classes=[DataClass.INTERNAL, DataClass.RESTRICTED],
        memory_scopes=["user", "global"],
        hosts=["wiki.example.com", "evil.example.com"],
    )
    assert ALL.for_version(wanted) == Grants(
        tools=["mail.read"],
        data_classes=[DataClass.INTERNAL],
        memory_scopes=["user"],
        hosts=["wiki.example.com"],
    )


def test_a_granted_pattern_gives_a_version_its_tools() -> None:
    granted = Grants(tools=["mail.>"])
    assert granted.for_version(Grants(tools=["mail.send"])).tools == ["mail.send"]
