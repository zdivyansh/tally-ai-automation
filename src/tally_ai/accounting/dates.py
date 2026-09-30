"""Financial years and parsing of dates as users write them ('kal', '25th', '25/09')."""

import re
from datetime import date, timedelta

MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}

_RELATIVE = {
    "aaj": 0,
    "today": 0,
    "abhi": 0,
    # In a sales message "kal" refers to a past sale: yesterday
    "kal": -1,
    "yesterday": -1,
    "parso": -2,
    "parson": -2,
}


def fy_start(day: date) -> date:
    """1 April of the Indian financial year containing `day`."""
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def fy_end(day: date) -> date:
    return date(fy_start(day).year + 1, 3, 31)


def fy_label(day: date) -> str:
    """'26-27' for any date from 1-Apr-2026 to 31-Mar-2027."""
    start = fy_start(day).year
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(value: str) -> int:
    year = int(value)
    return 2000 + year if year < 100 else year


def parse_user_date(text: str | None, today: date) -> date | None:
    """Parse a date phrase relative to `today`. Returns None if it cannot be understood.

    Day-only and day+month forms use the current month / year.
    """
    if text is None or not text.strip():
        return today
    t = " ".join(text.lower().replace(",", " ").split())
    t = re.sub(r"\b(ko|ka|ki|ke|on|dated|date|tarikh|tareekh)\b", " ", t).strip()
    t = " ".join(t.split())

    if t in _RELATIVE:
        return today + timedelta(days=_RELATIVE[t])

    # 2026-09-25
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        return _safe_date(int(m[1]), int(m[2]), int(m[3]))

    # 25/09, 25-09-2026, 25.09.26
    if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?", t):
        return _safe_date(_year(m[3]) if m[3] else today.year, int(m[2]), int(m[1]))

    # 25 sep, 25th september 2026, sep 25
    if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)(?: (\d{2}|\d{4}))?", t):
        month = MONTHS.get(m[2])
        if month:
            return _safe_date(_year(m[3]) if m[3] else today.year, month, int(m[1]))
    if m := re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{2}|\d{4}))?", t):
        month = MONTHS.get(m[1])
        if month:
            return _safe_date(_year(m[3]) if m[3] else today.year, month, int(m[2]))

    # 25, 25th
    if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", t):
        return _safe_date(today.year, today.month, int(m[1]))

    return None


def date_warnings(day: date, today: date) -> list[str]:
    warnings = []
    if day > today:
        warnings.append(f"Date {day:%d-%b-%Y} is in the future.")
    if not fy_start(today) <= day <= fy_end(today):
        warnings.append(f"Date {day:%d-%b-%Y} is outside the current financial year ({fy_label(today)}).")
    return warnings
