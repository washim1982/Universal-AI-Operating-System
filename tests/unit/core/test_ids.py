from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aios.core.ids import ULID_LENGTH, UlidGenerator, is_ulid, new_ulid, ulid_time


def test_format() -> None:
    value = new_ulid()
    assert len(value) == ULID_LENGTH
    assert is_ulid(value)


def test_strictly_increasing_within_one_millisecond() -> None:
    ids = [new_ulid() for _ in range(2_000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_stays_monotonic_when_the_clock_goes_backwards() -> None:
    generator = UlidGenerator()
    later = generator.new(now_ms=2_000_000)
    assert generator.new(now_ms=1_000) > later


@given(st.integers(min_value=0, max_value=253_402_300_799_999))  # up to datetime.max
def test_encodes_the_timestamp(now_ms: int) -> None:
    value = UlidGenerator().new(now_ms=now_ms)
    assert is_ulid(value)
    assert ulid_time(value) == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=now_ms)


def test_rejects_timestamps_past_the_year_10889() -> None:
    with pytest.raises(OverflowError):
        UlidGenerator().new(now_ms=2**48)


def test_time_of_a_fresh_id_is_now() -> None:
    delta = datetime.now(UTC) - ulid_time(new_ulid())
    assert abs(delta.total_seconds()) < 5


@pytest.mark.parametrize(
    "value",
    ["", "01ARZ3NDEKTSV4RRFFQ69G5FA", "01arz3ndektsv4rrffq69g5fav", "81ARZ3NDEKTSV4RRFFQ69G5FAV"],
)
def test_rejects_non_ulids(value: str) -> None:
    assert not is_ulid(value)
    with pytest.raises(ValueError, match="ULID"):
        ulid_time(value)
