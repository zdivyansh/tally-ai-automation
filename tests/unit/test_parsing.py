from datetime import date
from decimal import Decimal

import pytest

from tally_ai.tally.parsing import (
    format_amount,
    format_number,
    parse_date,
    parse_decimal,
    parse_quantity,
    parse_rate,
    sanitize_xml,
)


def test_sanitize_removes_raw_and_entity_control_chars() -> None:
    assert sanitize_xml("<A>Default\x05Class &#4; Primary</A>") == "<A>DefaultClass  Primary</A>"


def test_sanitize_keeps_legal_entities() -> None:
    assert sanitize_xml("<A>&#10;&#x41;&amp;</A>") == "<A>&#10;&#x41;&amp;</A>"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 1,234.50", Decimal("1234.50")),
        ("-0.38", Decimal("-0.38")),
        ("12", Decimal(12)),
        ("", None),
        (None, None),
    ],
)
def test_parse_decimal(raw: str | None, expected: Decimal | None) -> None:
    assert parse_decimal(raw) == expected


def test_parse_quantity_and_rate() -> None:
    assert parse_quantity(" 15 Ctn") == (Decimal(15), "Ctn")
    assert parse_quantity("1.0000 Pcs") == (Decimal("1.0000"), "Pcs")
    assert parse_quantity("-1253 Ctn") == (Decimal(-1253), "Ctn")
    assert parse_rate("1310.63/Ctn") == (Decimal("1310.63"), "Ctn")
    assert parse_rate("") is None


def test_dates() -> None:
    assert parse_date("20260930") == date(2026, 9, 30)
    assert parse_date("garbage") is None


def test_formatting() -> None:
    assert format_amount(Decimal("1179.5")) == "1179.50"
    assert format_amount(Decimal("0.285")) == "0.29"  # half-up, not banker's rounding
    assert format_number(Decimal("3.00")) == "3"
    assert format_number(Decimal("6.870")) == "6.87"
    assert format_number(Decimal("1E+1")) == "10"
