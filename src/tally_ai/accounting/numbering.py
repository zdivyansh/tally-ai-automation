"""Voucher numbers of the form <prefix>/<yy-yy>/<nnnn>, restarting every financial year."""

import re
from collections import Counter
from collections.abc import Iterable
from datetime import date

from tally_ai.accounting.dates import fy_label

_SERIES = re.compile(r"^(?P<prefix>.+)/(?P<fy>\d{2}-\d{2})/(?P<number>\d+)$")
MIN_WIDTH = 4


class NumberingError(ValueError):
    pass


def derive_prefix(numbers: Iterable[str | None]) -> str:
    """Most common prefix among existing numbers like 'ABC/26-27/0012'."""
    prefixes = Counter(m["prefix"] for n in numbers if n and (m := _SERIES.match(n.strip())))
    if not prefixes:
        raise NumberingError(
            "cannot work out the voucher number prefix from existing vouchers; set SALES_NUMBER_PREFIX"
        )
    return prefixes.most_common(1)[0][0]


def next_number(prefix: str, voucher_date: date, existing: Iterable[str | None]) -> str:
    """Highest number in the date's financial year + 1; '0001' when the year has none yet."""
    fy = fy_label(voucher_date)
    highest, width = 0, MIN_WIDTH
    for number in existing:
        m = _SERIES.match(number.strip()) if number else None
        if m and m["prefix"] == prefix and m["fy"] == fy:
            highest = max(highest, int(m["number"]))
            width = max(width, len(m["number"]))
    return f"{prefix}/{fy}/{highest + 1:0{width}d}"
