"""Helpers for reading values out of Tally XML."""

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# XML 1.0 forbids most control characters. Tally emits them both as numeric
# entities (&#4;) and raw (e.g. \x05 inside CLASSNAME), which breaks parsers.
_RAW_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NUMERIC_ENTITY = re.compile(r"&#(x?[0-9a-fA-F]+);")


def _drop_illegal_entity(match: re.Match[str]) -> str:
    code_str = match.group(1)
    try:
        code = int(code_str[1:], 16) if code_str[:1] in "xX" else int(code_str)
    except ValueError:
        return match.group(0)
    return "" if code < 32 and code not in (9, 10, 13) else match.group(0)


def sanitize_xml(text: str) -> str:
    """Remove characters that are illegal in XML 1.0."""
    return _RAW_CONTROL.sub("", _NUMERIC_ENTITY.sub(_drop_illegal_entity, text))


def text(elem: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    """Stripped text of a child element; `default` when missing or blank."""
    if elem is None:
        return default
    value = elem.findtext(tag)
    if value is None:
        return default
    value = value.strip()
    return value or default


def parse_decimal(value: str | None) -> Decimal | None:
    """Parse Tally numbers like ' 1,234.50', '-0.38' or '12'."""
    if value is None:
        return None
    cleaned = value.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def parse_quantity(value: str | None) -> tuple[Decimal, str | None] | None:
    """Parse ' 15 Ctn' -> (Decimal('15'), 'Ctn')."""
    if not value or not value.strip():
        return None
    match = _NUMBER.search(value.replace(",", ""))
    if not match:
        return None
    unit = value.replace(",", "")[match.end() :].strip() or None
    return Decimal(match.group(0)), unit


def parse_rate(value: str | None) -> tuple[Decimal, str | None] | None:
    """Parse '1310.63/Ctn' -> (Decimal('1310.63'), 'Ctn')."""
    if not value or not value.strip():
        return None
    amount, _, unit = value.partition("/")
    number = parse_decimal(amount)
    if number is None:
        return None
    return number, unit.strip() or None


def parse_date(value: str | None) -> date | None:
    """Parse Tally's YYYYMMDD dates."""
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def format_amount(value: Decimal) -> str:
    """Money with exactly 2 decimals, rounded half-up: Decimal('1179.5') -> '1179.50'."""
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def format_number(value: Decimal) -> str:
    """Plain number without exponent or trailing zeros: Decimal('3.00') -> '3'."""
    normalized = value.normalize()
    return format(
        normalized.quantize(Decimal(1)) if normalized == normalized.to_integral() else normalized, "f"
    )
