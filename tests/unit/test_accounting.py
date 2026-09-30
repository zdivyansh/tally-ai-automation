from datetime import date
from decimal import Decimal

import pytest

from tally_ai.accounting.dates import date_warnings, fy_label, parse_user_date
from tally_ai.accounting.numbering import NumberingError, derive_prefix, next_number
from tally_ai.accounting.sales_invoice import InvoiceError, LineInput, build_sales_invoice
from tally_ai.tally.masters import GstRate, StockItem
from tally_ai.tally.models import Side

from .agent_fixtures import SALES_LEDGERS, debtor

D = Decimal
TODAY = date(2026, 9, 30)


# ------------------------------------------------------------------ dates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, TODAY),
        ("aaj", TODAY),
        ("kal", date(2026, 9, 29)),
        ("yesterday", date(2026, 9, 29)),
        ("parso", date(2026, 9, 28)),
        ("25th", date(2026, 9, 25)),
        ("25 tarikh", date(2026, 9, 25)),
        ("25/09", date(2026, 9, 25)),
        ("25-09-2026", date(2026, 9, 25)),
        ("25.9.26", date(2026, 9, 25)),
        ("25 sep", date(2026, 9, 25)),
        ("sept 25th 2026", date(2026, 9, 25)),
        ("2026-09-25", date(2026, 9, 25)),
        ("31/02", None),
        ("someday", None),
    ],
)
def test_parse_user_date(text: str | None, expected: date | None) -> None:
    assert parse_user_date(text, TODAY) == expected


def test_financial_year() -> None:
    assert fy_label(date(2026, 4, 1)) == "26-27"
    assert fy_label(date(2027, 3, 31)) == "26-27"
    assert fy_label(date(2026, 3, 31)) == "25-26"
    assert fy_label(date(2099, 12, 1)) == "99-00"


def test_date_warnings() -> None:
    assert date_warnings(TODAY, TODAY) == []
    assert "future" in date_warnings(date(2026, 10, 1), TODAY)[0]
    assert "outside the current financial year" in date_warnings(date(2026, 3, 31), TODAY)[0]


# ------------------------------------------------------------------ numbering

EXISTING = [
    "TST/26-27/0001",
    "TST/26-27/0143",
    "TST/26-27/0143",
    "TST/25-26/0850",
    "12",
    None,
    "X/26-27/9000",
]


def test_next_number_continues_series() -> None:
    assert next_number("TST", date(2026, 9, 30), EXISTING) == "TST/26-27/0144"


def test_new_financial_year_restarts() -> None:
    assert next_number("TST", date(2027, 4, 1), EXISTING) == "TST/27-28/0001"


def test_number_width_is_kept() -> None:
    assert next_number("TST", date(2026, 9, 30), ["TST/26-27/00099"]) == "TST/26-27/00100"


def test_derive_prefix() -> None:
    assert derive_prefix(EXISTING) == "TST"
    with pytest.raises(NumberingError, match="SALES_NUMBER_PREFIX"):
        derive_prefix(["12", None])


# ------------------------------------------------------------------ invoice


def item(name: str) -> StockItem:
    return StockItem(name=name, base_unit="Ctn")


FIVE = GstRate(applicable_from=date(2025, 9, 22), igst=D(5), cgst=D("2.5"), sgst=D("2.5"))


def line(name: str, qty: int, rate: str, disc: str) -> LineInput:
    return LineInput(item=item(name), quantity=D(qty), rate=D(rate), discount_pct=D(disc), gst=FIVE)


# Mirrors a real 5-line invoice (names changed); Tally computed CGST = SGST = 1537.59
REAL_SHAPED = [
    line("A (300) 5/-", 15, "1310.63", "12"),
    line("B (180) 10/-", 9, "1558.44", "6"),
    line("C (180) 10/-", 5, "1558.44", "6"),
    line("D (240) 5/-", 7, "1048.50", "10"),
    line("E (300) 5/-", 14, "1310.63", "6.87"),
]


def test_invoice_matches_tally_rounding() -> None:
    inv = build_sales_invoice(
        party=debtor("Example Traders"),
        company_state="Jharkhand",
        lines=REAL_SHAPED,
        ledgers=SALES_LEDGERS,
        voucher_date=date(2026, 9, 1),
        number="TST/26-27/0345",
    )
    assert inv.line_amounts == [D("17300.32"), D("13184.40"), D("7324.67"), D("6605.55"), D("17088.26")]
    assert inv.taxable == D("61503.20")
    assert [(t.ledger, t.label, t.amount) for t in inv.taxes] == [
        ("CGST", "CGST 2.5%", D("1537.59")),
        ("SGST", "SGST 2.5%", D("1537.59")),
    ]
    assert inv.round_off == D("-0.38")
    assert inv.total == D(64578)
    assert not inv.interstate
    v = inv.voucher
    assert v.side_total(Side.DEBIT) == v.side_total(Side.CREDIT) == D(64578)
    assert {i.accounting_ledger for i in v.inventory} == {"Sales"}
    assert v.inventory[0].godown == "Main Location"
    assert v.place_of_supply == "Jharkhand"


def test_interstate_uses_igst_and_interstate_ledger() -> None:
    inv = build_sales_invoice(
        party=debtor("Far Away Traders", state="Bihar"),
        company_state="Jharkhand",
        lines=[line("A (300) 5/-", 1, "1000", "0")],
        ledgers=SALES_LEDGERS,
        voucher_date=date(2026, 9, 1),
        number="TST/26-27/0001",
    )
    assert inv.interstate
    assert [(t.ledger, t.amount) for t in inv.taxes] == [("IGST", D("50.00"))]
    assert inv.voucher.inventory[0].accounting_ledger == "Inter-State Sale"
    assert inv.total == D(1050)


def test_no_round_off_line_when_total_is_whole() -> None:
    inv = build_sales_invoice(
        party=debtor("Example Traders"),
        company_state="Jharkhand",
        lines=[line("A", 1, "1000", "0")],
        ledgers=SALES_LEDGERS,
        voucher_date=date(2026, 9, 1),
        number="N",
    )
    assert inv.round_off == 0
    assert "Round Off (+/-)" not in {line.ledger for line in inv.voucher.ledgers}


def test_half_rupee_rounds_up() -> None:
    # 10 x 1.05 = 10.50 taxable, no GST -> total 11
    zero = GstRate(applicable_from=date(2020, 1, 1))
    inv = build_sales_invoice(
        party=debtor("Example Traders"),
        company_state="Jharkhand",
        lines=[LineInput(item=item("A"), quantity=D(10), rate=D("1.05"), discount_pct=D(0), gst=zero)],
        ledgers=SALES_LEDGERS,
        voucher_date=date(2026, 9, 1),
        number="N",
    )
    assert inv.taxes == []
    assert (inv.total, inv.round_off) == (D(11), D("0.50"))


def test_party_without_state_is_refused() -> None:
    with pytest.raises(InvoiceError, match="no state"):
        build_sales_invoice(
            party=debtor("No State", state=""),
            company_state="Jharkhand",
            lines=[line("A", 1, "1", "0")],
            ledgers=SALES_LEDGERS,
            voucher_date=date(2026, 9, 1),
            number="N",
        )
