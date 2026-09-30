"""Supplier invoice + mapping -> Tally purchase voucher (rules: docs/purchase-invoices.md)."""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from tally_ai.accounting.sales_invoice import SalesLedgers
from tally_ai.config import Settings
from tally_ai.masters.cache import MasterDataError, resolve_sales_ledgers
from tally_ai.purchase.invoice import InvoiceLine, SupplierInvoice
from tally_ai.purchase.mapping import MappingRule, MappingTable
from tally_ai.tally.masters import Ledger, StockItem
from tally_ai.tally.models import InventoryLine, LedgerLine, Side, Voucher, VoucherKind
from tally_ai.tally.queries import TallyQueries

CREDITORS = "Sundry Creditors"


class PurchaseError(ValueError):
    """The invoice cannot be turned into a purchase voucher."""


@dataclass
class PurchaseMasters:
    company_state: str
    ledgers: dict[str, Ledger]
    items: dict[str, StockItem]
    tax: SalesLedgers
    purchase_ledger: str

    @classmethod
    def load(cls, queries: TallyQueries, settings: Settings) -> "PurchaseMasters":
        states = {c.state for c in queries.companies() if c.state}
        if len(states) != 1:
            raise MasterDataError("expected one open company with a state; set TALLY_COMPANY")
        ledgers = {ledger.name: ledger for ledger in queries.ledgers()}
        if settings.purchase_ledger not in ledgers:
            raise MasterDataError(
                f"ledger {settings.purchase_ledger!r} (PURCHASE_LEDGER) does not exist in Tally"
            )
        return cls(
            company_state=states.pop(),
            ledgers=ledgers,
            items={item.name: item for item in queries.stock_items()},
            tax=resolve_sales_ledgers(ledgers, settings),
            purchase_ledger=settings.purchase_ledger,
        )

    def supplier_ledger(self, gstin: str) -> Ledger:
        found = [
            ledger
            for ledger in self.ledgers.values()
            if ledger.gstin and ledger.gstin.upper() == gstin.upper() and ledger.is_under(CREDITORS)
        ]
        if len(found) != 1:
            names = [ledger.name for ledger in found]
            raise PurchaseError(
                f"expected one supplier ledger with GSTIN {gstin} under Sundry Creditors, "
                f"found {names or 'none'}"
            )
        return found[0]


@dataclass(frozen=True)
class MappedLine:
    line: InvoiceLine
    rule: MappingRule | None
    conflict: list[MappingRule] = field(default_factory=list)

    @property
    def quantity(self) -> Decimal | None:
        return None if self.rule is None else self.line.quantity * self.rule.ctn_per_case


def map_lines(invoice: SupplierInvoice, mapping: MappingTable, masters: PurchaseMasters) -> list[MappedLine]:
    out = []
    for line in invoice.lines:
        outcome = mapping.match(line)
        rule = outcome.rule
        if rule is not None and rule.tally_item not in masters.items:
            rule = None  # item renamed or deleted in Tally: ask again
        out.append(MappedLine(line=line, rule=rule, conflict=outcome.conflict))
    return out


def remote_id(invoice: SupplierInvoice) -> str:
    """Stable id: re-posting the same invoice alters the voucher instead of duplicating it."""
    return f"tally-ai-purchase-{invoice.supplier_gstin}-{invoice.invoice_number}"


def build_purchase_voucher(
    invoice: SupplierInvoice,
    mapped: list[MappedLine],
    masters: PurchaseMasters,
    *,
    voucher_type: str = "Purchase",
    reference: str | None = None,
) -> Voucher:
    problems = invoice.problems()
    if problems:
        raise PurchaseError("invoice does not add up: " + "; ".join(problems))
    if any(m.rule is None for m in mapped):
        raise PurchaseError("some lines are not mapped to Tally items")
    supplier = masters.supplier_ledger(invoice.supplier_gstin)
    tax = masters.tax

    inventory = []
    for m in mapped:
        assert m.rule is not None and m.quantity is not None
        item = masters.items[m.rule.tally_item]
        if not item.base_unit:
            raise PurchaseError(f"Tally item {item.name!r} has no unit")
        if m.quantity <= 0:
            raise PurchaseError(f"line {m.line.number}: quantity must be positive")
        inventory.append(
            InventoryLine(
                stock_item=item.name,
                quantity=m.quantity,
                unit=item.base_unit,
                rate=(m.line.taxable / m.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                exact_amount=m.line.taxable,
                accounting_ledger=masters.purchase_ledger,
                godown=tax.godown,
                batch=tax.batch,
            )
        )

    ledgers = [LedgerLine(ledger=supplier.name, side=Side.CREDIT, amount=invoice.total, is_party=True)]
    for ledger, amount in ((tax.cgst, invoice.cgst), (tax.sgst, invoice.sgst), (tax.igst, invoice.igst)):
        if amount:
            ledgers.append(LedgerLine(ledger=ledger, side=Side.DEBIT, amount=amount))
    difference = invoice.total - invoice.taxable - invoice.cgst - invoice.sgst - invoice.igst
    if difference:
        raise PurchaseError(f"invoice total differs from taxable + tax by {difference}")

    return Voucher(
        kind=VoucherKind.PURCHASE,
        voucher_type=None if voucher_type == VoucherKind.PURCHASE.value else voucher_type,
        date=invoice.invoice_date,
        number=None,  # Tally numbers purchase vouchers automatically
        reference=reference or invoice.invoice_number,
        reference_date=invoice.invoice_date,
        party_ledger=supplier.name,
        remote_id=remote_id(invoice) if reference is None else f"{remote_id(invoice)}-{reference}",
        state_name=supplier.state,
        place_of_supply=masters.company_state,
        party_gstin=supplier.gstin,
        gst_registration_type=supplier.gst_registration_type,
        inventory=inventory,
        ledgers=ledgers,
    )
