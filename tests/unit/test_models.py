from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tally_ai.tally import InventoryLine, LedgerLine, Side, Voucher, VoucherKind
from tally_ai.tally.models import ImportResult

from .sample_vouchers import bank_receipt, purchase_invoice, sales_invoice

D = Decimal


def test_inventory_amount_applies_discount_and_rounds_half_up() -> None:
    line = sales_invoice().inventory[0]
    # 15 * 1310.63 = 19659.45; less 12% = 17300.316 -> 17300.32
    assert line.amount == D("17300.32")


def test_valid_vouchers_balance() -> None:
    for voucher in (sales_invoice(), purchase_invoice(), bank_receipt()):
        assert voucher.side_total(Side.DEBIT) == voucher.side_total(Side.CREDIT)


def test_voucher_kind_rules() -> None:
    assert VoucherKind.SALES.party_side is Side.DEBIT
    assert VoucherKind.SALES.inventory_side is Side.CREDIT
    assert VoucherKind.PURCHASE.party_side is Side.CREDIT
    assert VoucherKind.PURCHASE.inventory_side is Side.DEBIT
    assert VoucherKind.RECEIPT.party_side is Side.CREDIT
    assert VoucherKind.PAYMENT.party_side is Side.DEBIT
    assert VoucherKind.RECEIPT.view == "Accounting Voucher View"


def test_unbalanced_voucher_is_rejected() -> None:
    ledgers = list(sales_invoice().ledgers)
    ledgers[-1] = LedgerLine(ledger="Round Off (+/-)", side=Side.CREDIT, amount=D("0.34"))
    with pytest.raises(ValidationError, match="does not balance"):
        sales_invoice(ledgers=ledgers)


def test_party_line_must_match_party_ledger() -> None:
    with pytest.raises(ValidationError, match="party_ledger"):
        sales_invoice(party_ledger="Someone Else")


def test_party_must_be_on_correct_side() -> None:
    with pytest.raises(ValidationError, match="Dr side"):
        Voucher(
            kind=VoucherKind.PAYMENT,
            date=date(2026, 10, 1),
            party_ledger="Vendor",
            ledgers=[
                LedgerLine(ledger="Vendor", side=Side.CREDIT, amount=D(10), is_party=True),
                LedgerLine(ledger="Cash", side=Side.DEBIT, amount=D(10)),
            ],
        )


def test_exactly_one_party_line() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        sales_invoice(
            ledgers=[line.model_copy(update={"is_party": False}) for line in sales_invoice().ledgers]
        )


def test_receipt_cannot_have_inventory() -> None:
    item = InventoryLine(stock_item="X", quantity=D(1), unit="Ctn", rate=D(1), accounting_ledger="Sales")
    with pytest.raises(ValidationError, match="cannot have inventory"):
        Voucher(
            kind=VoucherKind.RECEIPT,
            date=date(2026, 10, 1),
            party_ledger="P",
            inventory=[item],
            ledgers=[LedgerLine(ledger="P", side=Side.CREDIT, amount=D(1), is_party=True)],
        )


@pytest.mark.parametrize("field", [{"quantity": D(0)}, {"rate": D(-1)}, {"discount_pct": D(101)}])
def test_inventory_line_bounds(field: dict[str, Decimal]) -> None:
    base = {"stock_item": "X", "quantity": D(1), "unit": "Ctn", "rate": D(1), "accounting_ledger": "Sales"}
    with pytest.raises(ValidationError):
        InventoryLine.model_validate(base | field)


def test_import_result_ok() -> None:
    assert ImportResult(created=1).ok
    assert not ImportResult(exceptions=1).ok
    assert not ImportResult(created=1, line_errors=["oops"]).ok
