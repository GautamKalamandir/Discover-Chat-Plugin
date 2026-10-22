"""The periods a /sales-summary compares (ADR 0016 §4, design §4.1).

Latest day with data D and financial year start month F:
- `day` D; `mtd` 1st of D's month to D; `fytd` 1st of F in D's financial year to D;
- `months_fy` each month of the financial year up to D (the last one up to D);
- `month_ends_fy` the last day of each of those months (D for the current one): closing stock;
- every `*_ly` period is the same range one year earlier, so a month to date is always compared
  with the same days last year.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Range = tuple[date, date]
BasePeriod = Literal["day", "mtd", "fytd", "months_fy", "month_ends_fy"]
PeriodKey = Literal[
    "day",
    "day_ly",
    "mtd",
    "mtd_ly",
    "fytd",
    "fytd_ly",
    "months_fy",
    "months_fy_ly",
    "month_ends_fy",
]
BASE_PERIODS: tuple[BasePeriod, ...] = ("day", "mtd", "fytd", "months_fy", "month_ends_fy")


def is_monthly(key: str) -> bool:
    """One figure per month of the financial year."""
    return key.startswith(("months_fy", "month_ends_fy"))


def shift_year(day: date, years: int = -1) -> date:
    """The same day `years` years away; 29 Feb becomes 28 Feb."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


@dataclass(frozen=True)
class SalesPeriods:
    day: date
    fiscal_start_month: int

    @property
    def fy_start(self) -> date:
        year = self.day.year if self.day.month >= self.fiscal_start_month else self.day.year - 1
        return date(year, self.fiscal_start_month, 1)

    def ranges(self, key: PeriodKey) -> list[tuple[str | None, Range]]:
        """(month key or None, range) for one period. Months are keyed by this year's month, so a
        month and the same month last year line up."""
        last_year = key.endswith("_ly")
        base = key.removesuffix("_ly")
        if base == "day":
            spans: list[tuple[str | None, Range]] = [(None, (self.day, self.day))]
        elif base == "mtd":
            spans = [(None, (self.day.replace(day=1), self.day))]
        elif base == "fytd":
            spans = [(None, (self.fy_start, self.day))]
        elif base == "month_ends_fy":
            spans = [(f"{start:%Y-%m}", (end, end)) for start, end in self._months()]
        else:
            spans = [(f"{start:%Y-%m}", (start, end)) for start, end in self._months()]
        if last_year:
            spans = [(m, (shift_year(a), shift_year(b))) for m, (a, b) in spans]
        return spans

    def label(self, key: PeriodKey) -> str:
        """How the period is described to the agent and the reader."""
        spans = self.ranges(key)
        if key == "month_ends_fy":
            return f"the last day of each month, {spans[0][1][0]:%d %b %Y} to {self.day:%d %b %Y}"
        if is_monthly(key):
            first, last = spans[0][1][0], spans[-1][1][1]
            return f"each month, {first:%b %Y} to {last:%d %b %Y}"
        start, end = spans[0][1]
        return f"{start:%d %b %Y}" if start == end else f"{start:%d %b %Y} to {end:%d %b %Y}"

    def _months(self) -> list[Range]:
        months: list[Range] = []
        start = self.fy_start
        while start <= self.day:
            end = min(month_end(start), self.day)
            months.append((start, end))
            start = month_end(start) + timedelta(days=1)
        return months
