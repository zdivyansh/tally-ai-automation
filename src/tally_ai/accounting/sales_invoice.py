"""Compute a sales invoice (tax, round-off, total) and build the Tally voucher."""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from tally_ai.tally.masters import GstRate, Ledger, StockItem
from tally_ai.tally.models import InventoryLine, LedgerLine, Side, Voucher, VoucherKind, round_money


class InvoiceError(ValueError):
    """The invoice cannot be built from the given data."""


@dataclass(frozen=True)
class SalesLedgers:
    """Ledger names used on sales invoices."""

    sales: str
    interstate_sales: str
    cgst: str
    sgst: str
    igst: str
    round_off: str
    godown: str | None = None
    batch: str | None = None


@dataclass(frozen=True)
class LineInput:
    item: StockItem
    quantity: Decimal
    rate: Decimal
    discount_pct: Decimal
    gst: GstRate


@dataclass(frozen=True)
class TaxLine:
    ledger: str
    label: str  # e.g. "CGST 2.5%"
    amount: Decimal


@dataclass(frozen=True)
class SalesInvoice:
    voucher: Voucher
    interstate: bool
    taxable: Decimal
    taxes: list[TaxLine]
    round_off: Decimal
    total: Decimal
    line_amounts: list[Decimal] = field(default_factory=list)


def _pct(value: Decimal) -> str:
    return f"{value.normalize():f}%"


def is_interstate(party: Ledger, company_state: str) -> bool:
    if not party.state:
        raise InvoiceError(f"ledger {party.name!r} has no state in Tally; set it before billing")
    return party.state.strip().lower() != company_state.strip().lower()


def build_sales_invoice(
    *,
    party: Ledger,
    company_state: str,
    lines: list[LineInput],
    ledgers: SalesLedgers,
    voucher_date: date,
    number: str | None,
    voucher_type: str = "Sales",
    narration: str | None = None,
    remote_id: str | None = None,
) -> SalesInvoice:
    if not lines:
        raise InvoiceError("an invoice needs at least one item")
    interstate = is_interstate(party, company_state)
    sales_ledger = ledgers.interstate_sales if interstate else ledgers.sales

    inventory: list[InventoryLine] = []
    tax_by_ledger: dict[str, tuple[str, Decimal]] = {}

    def add_tax(ledger: str, label: str, amount: Decimal) -> None:
        if amount == 0:
            return
        previous_label, previous = tax_by_ledger.get(ledger, (label, Decimal(0)))
        # Mixed rates on one ledger: drop the rate from the label
        tax_by_ledger[ledger] = (label if previous_label == label else label.split()[0], previous + amount)

    for line in lines:
        if not line.item.base_unit:
            raise InvoiceError(f"stock item {line.item.name!r} has no unit in Tally")
        if line.gst.cess:
            raise InvoiceError(f"{line.item.name!r} has GST cess, which is not supported yet")
        inv = InventoryLine(
            stock_item=line.item.name,
            quantity=line.quantity,
            unit=line.item.base_unit,
            rate=line.rate,
            discount_pct=line.discount_pct,
            accounting_ledger=sales_ledger,
            godown=ledgers.godown,
            batch=ledgers.batch,
        )
        inventory.append(inv)
        # GST is computed per line and rounded to paise, as Tally does
        if interstate:
            add_tax(
                ledgers.igst, f"IGST {_pct(line.gst.igst)}", round_money(inv.amount * line.gst.igst / 100)
            )
        else:
            add_tax(
                ledgers.cgst, f"CGST {_pct(line.gst.cgst)}", round_money(inv.amount * line.gst.cgst / 100)
            )
            add_tax(
                ledgers.sgst, f"SGST {_pct(line.gst.sgst)}", round_money(inv.amount * line.gst.sgst / 100)
            )

    taxable = sum((inv.amount for inv in inventory), Decimal(0))
    taxes = [
        TaxLine(ledger=name, label=label, amount=amount) for name, (label, amount) in tax_by_ledger.items()
    ]
    gross = taxable + sum((t.amount for t in taxes), Decimal(0))
    total = gross.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    round_off = total - gross

    ledger_lines = [LedgerLine(ledger=party.name, side=Side.DEBIT, amount=total, is_party=True)]
    ledger_lines += [LedgerLine(ledger=t.ledger, side=Side.CREDIT, amount=t.amount) for t in taxes]
    if round_off:
        ledger_lines.append(LedgerLine(ledger=ledgers.round_off, side=Side.CREDIT, amount=round_off))

    voucher = Voucher(
        kind=VoucherKind.SALES,
        voucher_type=None if voucher_type == VoucherKind.SALES.value else voucher_type,
        date=voucher_date,
        number=number,
        party_ledger=party.name,
        narration=narration,
        remote_id=remote_id,
        state_name=party.state,
        place_of_supply=party.state,
        party_gstin=party.gstin,
        gst_registration_type=party.gst_registration_type,
        inventory=inventory,
        ledgers=ledger_lines,
    )
    return SalesInvoice(
        voucher=voucher,
        interstate=interstate,
        taxable=taxable,
        taxes=taxes,
        round_off=round_off,
        total=total,
        line_amounts=[inv.amount for inv in inventory],
    )
