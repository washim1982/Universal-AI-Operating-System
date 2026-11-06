"""Cron expressions for schedule entry points (FR-APP-02): minute hour day month weekday, in UTC.

Each field takes ``*``, a number, ``a-b``, ``*/n`` or ``a-b/n``, and commas between them. As in
cron, when a day of the month and a weekday are both given, either one matching is enough. Weekdays
run from 0 (Sunday) to 6, and 7 is also Sunday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("minute", "hour", "day of month", "month", "weekday")


def _field(text: str, low: int, high: int, name: str) -> frozenset[int]:
    found: set[int] = set()
    for part in text.split(","):
        spread, _, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() else 0 if step_text else 1
        if step < 1:
            raise ValueError(f"cron {name}: bad step in {part!r}")
        if spread == "*":
            first, last = low, high
        else:
            first_text, dash, last_text = spread.partition("-")
            if not first_text.isdigit() or (dash and not last_text.isdigit()):
                raise ValueError(f"cron {name}: can't read {part!r}")
            first, last = int(first_text), int(last_text) if dash else int(first_text)
        if not low <= first <= last <= high:
            raise ValueError(f"cron {name}: {part!r} is outside {low}-{high}")
        found.update(range(first, last + 1, step))
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        fields = expression.split()
        if len(fields) != len(_RANGES):
            raise ValueError(f"cron needs 5 fields, not {len(fields)}: {expression!r}")
        parsed = [
            _field(text, low, high, name)
            for text, (low, high), name in zip(fields, _RANGES, _NAMES, strict=True)
        ]
        minutes, hours, days, months, weekdays = parsed
        return cls(
            minutes,
            hours,
            days,
            months,
            frozenset(0 if day == 7 else day for day in weekdays),
            fields[2].startswith("*"),
            fields[4].startswith("*"),
        )

    def fires_at(self, when: datetime) -> bool:
        """Whether this minute of UTC is one the schedule names."""
        weekday = (when.weekday() + 1) % 7  # Python's Monday is 0, and cron's Sunday is
        on_day = when.day in self.days
        on_weekday = weekday in self.weekdays
        day_fits = (
            on_day or on_weekday
            if not self.any_day and not self.any_weekday
            else on_day and on_weekday
        )
        return (
            when.minute in self.minutes
            and when.hour in self.hours
            and when.month in self.months
            and day_fits
        )
