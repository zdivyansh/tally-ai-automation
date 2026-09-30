"""Synthetic vouchers shared by unit tests (names are made up)."""

from datetime import date
from decimal import Decimal

from tally_ai.tally import BankAllocation, InventoryLine, LedgerLine, Side, Voucher, VoucherKind

D = Decimal


def sales_invoice(**overrides: object) -> Voucher:
    """15 Ctn @ 1310.63 less 12% = 17300.32; CGST/SGST 2.5% = 432.51 each; round-off -0.34."""
    fields: dict[str, object] = {
        "kind": VoucherKind.SALES,
        "date": date(2026, 10, 1),
        "number": "TEST/0001",
        "party_ledger": "Example Traders",
        "narration": "unit test",
        "inventory": [
            InventoryLine(
                stock_item="Crunchy Chips (300) 5/-",
                quantity=D(15),
                unit="Ctn",
                rate=D("1310.63"),
                discount_pct=D(12),
                accounting_ledger="Sales",
            )
        ],
        "ledgers": [
            LedgerLine(ledger="Example Traders", side=Side.DEBIT, amount=D("18165.00"), is_party=True),
            LedgerLine(ledger="CGST", side=Side.CREDIT, amount=D("432.51")),
            LedgerLine(ledger="SGST", side=Side.CREDIT, amount=D("432.51")),
            LedgerLine(ledger="Round Off (+/-)", side=Side.CREDIT, amount=D("-0.34")),
        ],
    }
    fields.update(overrides)
    return Voucher.model_validate(fields)


def purchase_invoice() -> Voucher:
    return Voucher(
        kind=VoucherKind.PURCHASE,
        date=date(2026, 10, 1),
        number="50",
        reference="SUP/123",
        reference_date=date(2026, 9, 30),
        party_ledger="Snacks Co Distributors",
        inventory=[
            InventoryLine(
                stock_item="Crunchy Chips (300) 5/-",
                quantity=D(10),
                unit="Ctn",
                rate=D(1000),
                accounting_ledger="Purchase",
            )
        ],
        ledgers=[
            LedgerLine(ledger="Snacks Co Distributors", side=Side.CREDIT, amount=D(10500), is_party=True),
            LedgerLine(ledger="CGST", side=Side.DEBIT, amount=D(250)),
            LedgerLine(ledger="SGST", side=Side.DEBIT, amount=D(250)),
        ],
    )


def bank_receipt() -> Voucher:
    return Voucher(
        kind=VoucherKind.RECEIPT,
        date=date(2026, 10, 1),
        party_ledger="Example Traders",
        ledgers=[
            LedgerLine(ledger="Example Traders", side=Side.CREDIT, amount=D(5000), is_party=True),
            LedgerLine(
                ledger="Example Bank",
                side=Side.DEBIT,
                amount=D(5000),
                bank=BankAllocation(instrument_number="000123", favouring="Example Traders"),
            ),
        ],
    )
