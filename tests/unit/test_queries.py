from collections.abc import Callable
from datetime import date
from decimal import Decimal

import httpx

from tally_ai.tally import TallyClient, TallyQueries
from tally_ai.tally.masters import (
    Group,
    GstDetails,
    GstRate,
    StockGroup,
    StockItem,
    resolve_group_path,
    resolve_item_gst,
)

MockTally = Callable[..., tuple[TallyClient, list[httpx.Request]]]

SPECIFY = "Specify Details Here"
INHERIT = "As per Company/Stock Group"


def test_ledgers_resolve_primary_group_and_aliases(mock_tally: MockTally) -> None:
    client, _ = mock_tally("groups.xml", "ledgers.xml")
    ledgers = {ledger.name: ledger for ledger in TallyQueries(client).ledgers()}

    assert set(ledgers) == {"Example Traders", "CGST"}  # blank names skipped
    trader = ledgers["Example Traders"]
    assert trader.parent == "Retail Customers"
    assert trader.group_path == ("Retail Customers", "Sundry Debtors", "Current Assets")
    assert trader.is_under("Sundry Debtors")
    assert not ledgers["CGST"].is_under("Sundry Debtors")
    assert trader.aliases == ("Ex Trd",)
    assert trader.state == "Jharkhand"
    assert trader.gst_registration_type == "Unregistered"
    assert ledgers["CGST"].gst_duty_head == "CGST"


def test_stock_items_parse_dated_gst(mock_tally: MockTally) -> None:
    client, _ = mock_tally("stock_items.xml")
    items = {item.name: item for item in TallyQueries(client).stock_items()}

    chips = items["Crunchy Chips (300) 5/-"]
    assert chips.base_unit == "Ctn"
    assert chips.hsn == "210690"
    assert chips.gst.is_specified_here
    before, after = chips.gst.rates
    assert (before.igst, before.cgst, before.sgst) == (Decimal(12), Decimal(6), Decimal(6))
    assert (after.igst, after.cgst, after.sgst) == (Decimal(5), Decimal("2.50"), Decimal("2.50"))

    assert chips.gst.rate_on(date(2025, 9, 21)) == before
    assert chips.gst.rate_on(date(2025, 9, 22)) == after
    assert chips.gst.rate_on(date(2017, 6, 30)) is None

    assert not items["Pure Ghee 1 Ltr"].gst.is_specified_here


def rate(on: date, igst: int) -> GstRate:
    half = Decimal(igst) / 2
    return GstRate(applicable_from=on, igst=Decimal(igst), cgst=half, sgst=half)


def test_item_gst_falls_back_to_stock_group() -> None:
    groups = {
        "Dairy Co": StockGroup(name="Dairy Co", parent="Foods", gst=GstDetails(source=INHERIT)),
        "Foods": StockGroup(
            name="Foods", gst=GstDetails(source=SPECIFY, rates=(rate(date(2025, 9, 22), 5),))
        ),
    }
    ghee = StockItem(name="Pure Ghee", parent="Dairy Co", gst=GstDetails(source=INHERIT))
    resolved = resolve_item_gst(ghee, groups, date(2026, 10, 1))
    assert resolved is not None
    assert resolved.igst == Decimal(5)


def test_item_gst_own_rate_wins() -> None:
    groups = {
        "Foods": StockGroup(name="Foods", gst=GstDetails(source=SPECIFY, rates=(rate(date(2020, 1, 1), 18),)))
    }
    item = StockItem(
        name="Chips", parent="Foods", gst=GstDetails(source=SPECIFY, rates=(rate(date(2020, 1, 1), 5),))
    )
    resolved = resolve_item_gst(item, groups, date(2026, 10, 1))
    assert resolved is not None
    assert resolved.igst == Decimal(5)


def test_item_gst_unresolved_returns_none() -> None:
    item = StockItem(name="Loose", parent="Nowhere", gst=GstDetails(source=INHERIT))
    assert resolve_item_gst(item, {}, date(2026, 10, 1)) is None


def test_group_path_handles_cycles_and_unknowns() -> None:
    groups = {"A": Group(name="A", parent="B"), "B": Group(name="B", parent="A")}
    assert resolve_group_path("A", groups) == ("A", "B")
    assert resolve_group_path("Unknown", {}) == ("Unknown",)
    assert resolve_group_path(None, {}) == ()
