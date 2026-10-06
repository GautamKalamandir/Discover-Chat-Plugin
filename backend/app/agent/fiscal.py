"""Turns relative periods into explicit date ranges (FISCAL_YEAR_START_MONTH, Q13c = April)."""

from datetime import date, timedelta

from app.agent.models import TimeRange
from app.core.errors import AppError, ErrorCode


def fiscal_year_start(day: date, start_month: int) -> date:
    year = day.year if day.month >= start_month else day.year - 1
    return date(year, start_month, 1)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def resolve_period(time: TimeRange, today: date, start_month: int) -> tuple[date, date]:
    """Inclusive (start, end)."""
    fy = fiscal_year_start(today, start_month)
    month = today.replace(day=1)
    match time.period:
        case "current_fy":
            return fy, _add_months(fy, 12) - timedelta(days=1)
        case "last_fy":
            previous = _add_months(fy, -12)
            return previous, fy - timedelta(days=1)
        case "fytd":
            return fy, today
        case "current_month":
            return month, _add_months(month, 1) - timedelta(days=1)
        case "last_month":
            return _add_months(month, -1), month - timedelta(days=1)
        case "last_n_months":
            n = time.n or 3
            return _add_months(month, -n), month - timedelta(days=1)  # n complete months
        case "current_year":
            return date(today.year, 1, 1), date(today.year, 12, 31)
        case "last_year":
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        case "explicit":
            if time.start is None or time.end is None or time.start > time.end:
                raise AppError(
                    ErrorCode.QUESTION_INVALID,
                    "Please give a valid date range (start before end).",
                    422,
                )
            return time.start, time.end
    raise AssertionError(f"unhandled period {time.period}")  # pragma: no cover


def describe_range(start: date, end: date) -> str:
    return f"{start:%d %b %Y} to {end:%d %b %Y}"
