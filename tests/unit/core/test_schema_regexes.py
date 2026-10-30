"""The regexes published in the JSON Schemas must agree with the Python validators."""

import re
from collections.abc import Callable

from hypothesis import given
from hypothesis import strategies as st

from aios.core.errors import InvalidTopicError
from aios.core.topics import (
    NAME_REGEX,
    PATTERN_REGEX,
    TOPIC_REGEX,
    validate_name,
    validate_pattern,
    validate_topic,
)

_candidates = st.lists(
    st.sampled_from(["a", "b1", "x-y", "z_z", "*", ">", "", "A", "-a", "a-"]),
    min_size=1,
    max_size=18,
).map(".".join)


def _accepts(validator: Callable[[str], str], value: str) -> bool:
    try:
        validator(value)
    except InvalidTopicError:
        return False
    return True


@given(_candidates)
def test_topic_regex(value: str) -> None:
    assert (re.fullmatch(TOPIC_REGEX, value) is not None) is _accepts(validate_topic, value)


@given(_candidates)
def test_name_regex(value: str) -> None:
    assert (re.fullmatch(NAME_REGEX, value) is not None) is _accepts(validate_name, value)


@given(_candidates)
def test_pattern_regex(value: str) -> None:
    assert (re.fullmatch(PATTERN_REGEX, value) is not None) is _accepts(validate_pattern, value)
