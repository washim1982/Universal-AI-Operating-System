"""Cron for schedule entry points (FR-APP-02): the expressions people really write."""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aios.core.schedule import Cron


def at(day: int, hour: int = 0, minute: int = 0, *, month: int = 6) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# 1 June 2026 is a Monday, and 7 June a Sunday.
@pytest.mark.parametrize(
    ("expression", "when", "fires"),
    [
        ("* * * * *", at(1, 3, 17), True),
        ("0 9 * * *", at(1, 9), True),
        ("0 9 * * *", at(1, 9, 1), False),
        ("0 9 * * *", at(1, 10), False),
        ("*/15 * * * *", at(1, 3, 45), True),
        ("*/15 * * * *", at(1, 3, 44), False),
        ("0 9-17 * * *", at(1, 17), True),
        ("0 9-17 * * *", at(1, 18), False),
        ("0 9-17/2 * * *", at(1, 11), True),
        ("0 9-17/2 * * *", at(1, 10), False),
        ("0,30 * * * *", at(1, 1, 30), True),
        ("0 0 1 * *", at(1), True),
        ("0 0 1 * *", at(2), False),
        ("0 0 1 1 *", at(1, month=1), True),
        ("0 0 1 1 *", at(1, month=2), False),
        ("0 0 * * 1", at(1), True),
        ("0 0 * * 1", at(2), False),
        ("0 0 * * 0", at(7), True),
        ("0 0 * * 7", at(7), True),
        ("0 0 * * 1-5", at(5), True),
        ("0 0 * * 1-5", at(6), False),
    ],
)
def test_a_schedule_fires_when_it_says(expression: str, when: datetime, *, fires: bool) -> None:
    assert Cron.parse(expression).fires_at(when) is fires


def test_a_day_and_a_weekday_either_will_do() -> None:
    """The oddest rule in cron, and the one that surprises people if it is got wrong."""
    cron = Cron.parse("0 0 13 * 1")
    assert cron.fires_at(at(13))  # a Saturday, but the 13th
    assert cron.fires_at(at(1))  # the 1st, but a Monday
    assert not cron.fires_at(at(2))


def test_a_stepped_star_is_still_any_day() -> None:
    cron = Cron.parse("0 0 */2 * 1")
    assert cron.fires_at(at(1))  # Monday, and an odd day
    assert not cron.fires_at(at(3))  # an odd day, not Monday: both must hold


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",
        "*/0 * * * *",
        "*/x * * * *",
        "a * * * *",
        "1- * * * *",
        ", * * * *",
        "-1 * * * *",
    ],
)
def test_nonsense_is_refused(expression: str) -> None:
    with pytest.raises(ValueError, match="cron"):
        Cron.parse(expression)


@given(st.datetimes(timezones=st.just(UTC)))
def test_every_minute_is_every_minute(when: datetime) -> None:
    assert Cron.parse("* * * * *").fires_at(when)


@given(st.integers(0, 59), st.integers(0, 23))
def test_a_time_fires_in_that_minute_only(minute: int, hour: int) -> None:
    cron = Cron.parse(f"{minute} {hour} * * *")
    fired = [(h, m) for h in range(24) for m in range(60) if cron.fires_at(at(1, h, m))]
    assert fired == [(hour, minute)]
