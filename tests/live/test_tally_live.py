"""Tests against a real Tally server (settings from .env).

Run with:  TALLY_LIVE=1 uv run pytest -m live -v

The voucher tests create a Sales voucher numbered TEST/CLAUDE/<timestamp> and
always delete it again. Party / item / ledgers are picked from the company, or
set TALLY_TEST_PARTY / TALLY_TEST_ITEM explicitly.

The voucher date is today, or TALLY_TEST_DATE (YYYY-MM-DD). Tally in
Educational mode only accepts the 1st, 2nd and 31st of a month.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tally_ai.config import Settings
from tally_ai.tally import (
    InventoryLine,
    LedgerLine,
    Side,
    TallyClient,
    TallyImportError,
    TallyQueries,
    Voucher,
    VoucherKind,
)
from tally_ai.tally.masters import GstRate, StockItem, resolve_item_gst
from tally_ai.tally.models import round_money
from tally_ai.tally.parsing import parse_decimal, text
from tally_ai.tally.xml_builder import tdl_string

pytestmark = pytest.mark.live

TODAY = date.fromisoformat(os.environ["TALLY_TEST_DATE"]) if os.getenv("TALLY_TEST_DATE") else date.today()


@pytest.fixture(scope="module")
def client() -> Iterator[TallyClient]:
    with TallyClient.from_settings(Settings()) as c:
        yield c


@pytest.fixture(scope="module")
def queries(client: TallyClient) -> TallyQueries:
    return TallyQueries(client)


@dataclass(frozen=True)
class Setup:
    party: str
    item: StockItem
    gst: GstRate
    sales_ledger: str
    cgst: str
    sgst: str
    round_off: str


@pytest.fixture(scope="module")
def setup(queries: TallyQueries) -> Setup:
    ledgers = queries.ledgers()
    names = {ledger.name for ledger in ledgers}
    company_state = next((c.state for c in queries.companies() if c.state), None)

    party = os.getenv("TALLY_TEST_PARTY") or next(
        ledger.name
        for ledger in ledgers
        if ledger.is_under("Sundry Debtors") and (company_state is None or ledger.state == company_state)
    )
    stock_groups = queries.stock_groups()
    wanted_item = os.getenv("TALLY_TEST_ITEM")
    item, gst = next(
        (item, gst)
        for item in queries.stock_items()
        if item.base_unit
        and (wanted_item is None or item.name == wanted_item)
        and (gst := resolve_item_gst(item, stock_groups, TODAY)) is not None
        and gst.cgst > 0
    )

    def duty(head: str) -> str:
        return next(ledger.name for ledger in ledgers if ledger.gst_duty_head == head)

    return Setup(
        party=party,
        item=item,
        gst=gst,
        sales_ledger="Sales"
        if "Sales" in names
        else next(x.name for x in ledgers if x.is_under("Sales Accounts")),
        cgst=duty("CGST"),
        sgst=duty("SGST/UTGST"),
        round_off=next(name for name in names if "round" in name.lower()),
    )


def build_sales(s: Setup, number: str, *, remote_id: str | None = None, party: str | None = None) -> Voucher:
    party = party or s.party
    line = InventoryLine(
        stock_item=s.item.name,
        quantity=Decimal(1),
        unit=s.item.base_unit or "",
        rate=Decimal("1001.37"),
        discount_pct=Decimal(12),
        accounting_ledger=s.sales_ledger,
    )
    cgst = round_money(line.amount * s.gst.cgst / 100)
    sgst = round_money(line.amount * s.gst.sgst / 100)
    gross = line.amount + cgst + sgst
    total = gross.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return Voucher(
        kind=VoucherKind.SALES,
        date=TODAY,
        number=number,
        party_ledger=party,
        narration="AUTOMATED TEST (tally-ai live tests) - safe to delete",
        remote_id=remote_id,
        inventory=[line],
        ledgers=[
            LedgerLine(ledger=party, side=Side.DEBIT, amount=total, is_party=True),
            LedgerLine(ledger=s.cgst, side=Side.CREDIT, amount=cgst),
            LedgerLine(ledger=s.sgst, side=Side.CREDIT, amount=sgst),
            LedgerLine(ledger=s.round_off, side=Side.CREDIT, amount=total - gross),
        ],
    )


def stored_ledger_amounts(client: TallyClient, number: str) -> dict[str, Decimal]:
    root = client.export_collection(
        "Voucher",
        ["VoucherNumber", "LedgerEntries.LedgerName", "LedgerEntries.Amount"],
        filters=[f"$VoucherNumber = {tdl_string(number)}"],
        from_date=TODAY,
        to_date=TODAY,
    )
    return {
        name: parse_decimal(text(entry, "AMOUNT")) or Decimal(0)
        for entry in root.iter("LEDGERENTRIES.LIST")
        if (name := text(entry, "LEDGERNAME"))
    }


def cleanup(client: TallyClient, queries: TallyQueries, number: str) -> None:
    for v in queries.vouchers(voucher_type="Sales", number=number, from_date=TODAY, to_date=TODAY):
        if v.master_id:
            client.delete_voucher(v.date, "Sales", master_id=v.master_id, check=False)


# ------------------------------------------------------------------- read-only


def test_server_is_running(client: TallyClient) -> None:
    assert client.ping()


def test_masters_load(queries: TallyQueries) -> None:
    assert queries.companies()
    ledgers = queries.ledgers()
    assert any(ledger.is_under("Sundry Debtors") for ledger in ledgers)
    assert {ledger.gst_duty_head for ledger in ledgers} >= {"CGST", "SGST/UTGST", "IGST"}
    assert queries.stock_items()
    assert {t.name for t in queries.voucher_types()} >= {"Sales", "Purchase", "Receipt", "Payment"}


# ------------------------------------------------------------------- writes


def test_sales_voucher_roundtrip(client: TallyClient, queries: TallyQueries, setup: Setup) -> None:
    number = f"TEST/CLAUDE/{datetime.now():%H%M%S}"
    voucher = build_sales(setup, number, remote_id=str(uuid.uuid4()))
    try:
        created = client.import_vouchers([voucher])
        assert created.created == 1, created

        found = queries.vouchers(voucher_type="Sales", number=number, from_date=TODAY, to_date=TODAY)
        assert len(found) == 1
        assert found[0].party_ledger == setup.party

        # What Tally stored must equal what we sent (debits negative)
        party_line = voucher.ledgers[0]
        stored = stored_ledger_amounts(client, number)
        assert stored[setup.party] == -party_line.amount
        assert stored[setup.cgst] == voucher.ledgers[1].amount

        # Same REMOTEID again: Tally must alter, not duplicate
        again = client.import_vouchers([voucher])
        assert again.created == 0 and again.altered == 1, again
        assert len(queries.vouchers(voucher_type="Sales", number=number, from_date=TODAY, to_date=TODAY)) == 1

        master_id = found[0].master_id
        assert master_id
        deleted = client.delete_voucher(TODAY, VoucherKind.SALES, master_id=master_id)
        assert deleted.deleted == 1, deleted
        assert queries.vouchers(voucher_type="Sales", number=number, from_date=TODAY, to_date=TODAY) == []
    finally:
        cleanup(client, queries, number)


def test_unknown_ledger_is_rejected(client: TallyClient, queries: TallyQueries, setup: Setup) -> None:
    number = f"TEST/CLAUDE/X{datetime.now():%H%M%S}"
    voucher = build_sales(setup, number, party="ZZ No Such Ledger (tally-ai test)")
    try:
        with pytest.raises(TallyImportError, match="does not exist"):
            client.import_vouchers([voucher])
        assert queries.vouchers(voucher_type="Sales", number=number, from_date=TODAY, to_date=TODAY) == []
    finally:
        cleanup(client, queries, number)
