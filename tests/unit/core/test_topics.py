import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aios.core.errors import InvalidTopicError
from aios.core.topics import (
    EventKind,
    kind_of,
    matches,
    max_match_length,
    min_match_length,
    validate_name,
    validate_pattern,
    validate_topic,
)


@pytest.mark.parametrize(
    "topic", ["task.completed", "agent.lifecycle.suspended", "research.plan.created", "a-b.c_d.e1"]
)
def test_valid_topics(topic: str) -> None:
    assert validate_topic(topic) == topic


@pytest.mark.parametrize(
    "topic",
    ["task", "", "Task.completed", "task..completed", "task.*", "task.>", ".task", "task.-x"],
)
def test_invalid_topics(topic: str) -> None:
    with pytest.raises(InvalidTopicError):
        validate_topic(topic)


def test_topics_have_a_token_limit() -> None:
    with pytest.raises(InvalidTopicError):
        validate_topic(".".join(["a"] * 17))


@pytest.mark.parametrize("pattern", [">", "*", "task.*", "task.requested.research.>", "*.*.done"])
def test_valid_patterns(pattern: str) -> None:
    assert validate_pattern(pattern) == pattern


@pytest.mark.parametrize("pattern", ["", ">.task", "task.>.x", "task.**", "Task.*"])
def test_invalid_patterns(pattern: str) -> None:
    with pytest.raises(InvalidTopicError):
        validate_pattern(pattern)


def test_names_reject_wildcards() -> None:
    assert validate_name("research.web") == "research.web"
    with pytest.raises(InvalidTopicError):
        validate_name("research.*")


@pytest.mark.parametrize(
    ("pattern", "subject", "expected"),
    [
        ("task.completed", "task.completed", True),
        ("task.*", "task.completed", True),
        ("task.*", "task.requested.research.web", False),
        ("task.>", "task.requested.research.web", True),
        ("task.>", "task", False),
        (">", "anything.at.all", True),
        ("*.*.created", "research.plan.created", True),
        ("*.*.created", "research.created", False),
        ("research.*", "research", False),
    ],
)
def test_matching(pattern: str, subject: str, expected: bool) -> None:
    assert matches(pattern, subject) is expected


def test_matching_ignores_the_logical_length_limits() -> None:
    # Physical patterns are a scope prefix plus a valid logical pattern, so they can be longer.
    long = ".".join(["x" * 20] * 20)
    assert matches(f"{long}.>", f"{long}.y")
    with pytest.raises(InvalidTopicError):
        matches("task.>.x", "task.a.x")  # syntax is still checked


def _reference(pattern: str, subject: str) -> bool:
    """An independent regex implementation of the wildcard semantics."""
    regex = r"\.".join(
        r"[^.]+(\.[^.]+)*" if token == ">" else "[^.]+" if token == "*" else re.escape(token)
        for token in pattern.split(".")
    )
    return re.fullmatch(regex, subject) is not None


_tokens = st.sampled_from(["a", "b", "c"])
_subjects = st.lists(_tokens, min_size=1, max_size=5).map(".".join)
_patterns = st.lists(st.sampled_from(["a", "b", "*"]), min_size=0, max_size=4).flatmap(
    lambda head: st.sampled_from([head, [*head, ">"]]).filter(bool).map(".".join)
)


@given(_patterns, _subjects)
def test_matching_agrees_with_a_reference_implementation(pattern: str, subject: str) -> None:
    assert matches(pattern, subject) is _reference(pattern, subject)


@given(_patterns, _subjects)
def test_match_length_bounds(pattern: str, subject: str) -> None:
    if matches(pattern, subject):
        length = len(subject.split("."))
        assert length >= min_match_length(pattern)
        upper = max_match_length(pattern)
        assert upper is None or length <= upper


def test_kind() -> None:
    assert kind_of("task.requested") is EventKind.COMMAND
    assert kind_of("tool.call.requested") is EventKind.COMMAND
    assert kind_of("task.completed") is EventKind.FACT
    assert kind_of("requested.thing.created") is EventKind.FACT
