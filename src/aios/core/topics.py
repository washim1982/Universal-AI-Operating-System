"""Topic grammar (requirements §7.2, specs/topic-grammar.md).

A *logical topic* is what developers see and what the CloudEvents ``type`` holds: lowercase
dot-separated tokens, ``<domain>.<entity…>.<verb>``. Commands end in ``requested``; every other
event is a fact. Patterns use NATS-compatible wildcards: ``*`` matches exactly one token and
``>`` (last token only) matches one or more remaining tokens. Capability and tool names use the
same grammar, so one matcher serves topics, capabilities and tool permissions.

Physical subjects (``<tenant>.<app>.<topic>[.<suffix>]``) are built by the fabric, never by
agent code; see :mod:`aios.fabric.subjects`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema

from aios.core.errors import InvalidTopicError

TOKEN_PATTERN = r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"
DNS_LABEL_PATTERN = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TOKEN_RE = re.compile(TOKEN_PATTERN)
_DNS_LABEL_RE = re.compile(DNS_LABEL_PATTERN)

ONE = "*"
REST = ">"
MAX_TOPIC_LENGTH = 255
MAX_TOKENS = 16
COMMAND_VERB = "requested"

RESERVED_DOMAINS = frozenset(
    {
        "app",
        "run",
        "step",
        "task",
        "agent",
        "model",
        "tool",
        "memory",
        "approval",
        "human",
        "policy",
        "budget",
        "system",
    }
)
"""Domains owned by the platform. Apps use their own domains, e.g. ``research.plan.created``."""

PLATFORM_APP_ID = "aios"
"""Reserved app id for events that belong to a tenant rather than to one app."""


class EventKind(StrEnum):
    COMMAND = "command"
    """Asks for work; consumed by exactly one handler (a consumer group)."""
    FACT = "fact"
    """Records something that happened; broadcast to every subscriber."""


def is_token(value: str) -> bool:
    return _TOKEN_RE.fullmatch(value) is not None


def is_dns_label(value: str) -> bool:
    """Whether ``value`` is a valid tenant or app id (a single topic token, DNS-label style)."""
    return _DNS_LABEL_RE.fullmatch(value) is not None


def _split(value: str, what: str, *, min_tokens: int) -> list[str]:
    if not value or len(value) > MAX_TOPIC_LENGTH:
        raise InvalidTopicError(f"{what} must be 1-{MAX_TOPIC_LENGTH} characters: {value!r}")
    tokens = value.split(".")
    if not min_tokens <= len(tokens) <= MAX_TOKENS:
        raise InvalidTopicError(
            f"{what} must have {min_tokens}-{MAX_TOKENS} dot-separated tokens: {value!r}"
        )
    return tokens


def validate_topic(value: str) -> str:
    """Validate a logical topic (an event type) and return it unchanged."""
    for token in _split(value, "topic", min_tokens=2):
        if not is_token(token):
            raise InvalidTopicError(f"invalid token {token!r} in topic {value!r}")
    return value


def validate_name(value: str, what: str = "name", *, min_tokens: int = 1) -> str:
    """Validate a dotted name with no wildcards (capability, tool or subject suffix)."""
    for token in _split(value, what, min_tokens=min_tokens):
        if not is_token(token):
            raise InvalidTopicError(f"invalid token {token!r} in {what} {value!r}")
    return value


def _check_pattern_tokens(tokens: Sequence[str], value: str, what: str) -> None:
    for index, token in enumerate(tokens):
        if token == REST and index != len(tokens) - 1:
            raise InvalidTopicError(f"'>' must be the last token in {what} {value!r}")
        if token not in (ONE, REST) and not is_token(token):
            raise InvalidTopicError(f"invalid token {token!r} in {what} {value!r}")


def validate_pattern(value: str, what: str = "pattern") -> str:
    """Validate a subscription pattern (tokens, ``*``, and ``>`` as the last token)."""
    _check_pattern_tokens(_split(value, what, min_tokens=1), value, what)
    return value


def kind_of(topic: str) -> EventKind:
    """Commands end in ``requested``; everything else is a fact (FR-EVT-02)."""
    return EventKind.COMMAND if topic.rsplit(".", 1)[-1] == COMMAND_VERB else EventKind.FACT


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> tuple[str, ...]:
    # Syntax only. The length limits apply to logical names; a physical pattern (scope prefix
    # plus a valid logical pattern) may exceed them and must still match.
    tokens = tuple(pattern.split("."))
    _check_pattern_tokens(tokens, pattern, "pattern")
    return tokens


def matches(pattern: str, subject: str) -> bool:
    """Whether ``subject`` (a concrete dotted name) matches ``pattern``."""
    wanted = _compile(pattern)
    tokens = subject.split(".")
    for index, token in enumerate(wanted):
        if token == REST:
            return len(tokens) > index
        if index >= len(tokens) or (token != ONE and token != tokens[index]):
            return False
    return len(tokens) == len(wanted)


def min_match_length(pattern: str) -> int:
    """The fewest tokens a subject can have and still match ``pattern``."""
    return len(_compile(pattern))


def max_match_length(pattern: str) -> int | None:
    """The most tokens a matching subject can have, or ``None`` if unbounded (``>``)."""
    wanted = _compile(pattern)
    return None if wanted[-1] == REST else len(wanted)


# Equivalent regexes, published in the JSON Schemas for non-Python consumers.
_T = TOKEN_PATTERN
TOPIC_REGEX = rf"^{_T}(?:\.{_T}){{1,{MAX_TOKENS - 1}}}$"
NAME_REGEX = rf"^{_T}(?:\.{_T}){{0,{MAX_TOKENS - 1}}}$"
PATTERN_REGEX = rf"^(?:(?:{_T}|\*)\.){{0,{MAX_TOKENS - 1}}}(?:{_T}|\*|>)$"


def _schema(regex: str) -> WithJsonSchema:
    return WithJsonSchema({"type": "string", "pattern": regex, "maxLength": MAX_TOPIC_LENGTH})


Topic = Annotated[str, AfterValidator(validate_topic), _schema(TOPIC_REGEX)]
"""A logical topic / event type, for use as a Pydantic field type."""

TopicPattern = Annotated[str, AfterValidator(validate_pattern), _schema(PATTERN_REGEX)]
"""A subscription pattern, for use as a Pydantic field type."""

CapabilityName = Annotated[
    str, AfterValidator(lambda v: validate_name(v, "capability")), _schema(NAME_REGEX)
]
"""A capability name such as ``research.web``."""

CapabilityPattern = Annotated[
    str, AfterValidator(lambda v: validate_pattern(v, "capability")), _schema(PATTERN_REGEX)
]
"""A capability name or pattern such as ``research.*``."""
