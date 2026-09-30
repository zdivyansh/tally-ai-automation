"""Typed models for writing vouchers to Tally and reading import results.

Sign convention
---------------
Every line sits on a side of the voucher (Dr or Cr) and carries an amount
relative to that side. A negative amount reduces its side, e.g. a round-off
of -0.38 on the Cr side of a sales invoice. The XML builder converts this to
Tally's convention (debit = negative AMOUNT, ISDEEMEDPOSITIVE = Yes).
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

PAISA = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


class Side(StrEnum):
    DEBIT = "Dr"
    CREDIT = "Cr"


class VoucherKind(StrEnum):
    """Base voucher types supported. Values are Tally's voucher type names."""

    SALES = "Sales"
    PURCHASE = "Purchase"
    RECEIPT = "Receipt"
    PAYMENT = "Payment"

    @property
    def is_invoice(self) -> bool:
        return self in (VoucherKind.SALES, VoucherKind.PURCHASE)

    @property
    def party_side(self) -> Side:
        return Side.DEBIT if self in (VoucherKind.SALES, VoucherKind.PAYMENT) else Side.CREDIT

    @property
    def inventory_side(self) -> Side:
        return Side.CREDIT if self is VoucherKind.SALES else Side.DEBIT

    @property
    def view(self) -> str:
        return "Invoice Voucher View" if self.is_invoice else "Accounting Voucher View"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BankAllocation(_Model):
    transaction_type: str = "Cheque/DD"
    instrument_number: str | None = None
    instrument_date: dt.date | None = None
    favouring: str | None = None


class LedgerLine(_Model):
    ledger: str = Field(min_length=1)
    side: Side
    amount: Decimal
    is_party: bool = False
    bank: BankAllocation | None = None


class InventoryLine(_Model):
    stock_item: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1)
    rate: Decimal = Field(ge=0)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    accounting_ledger: str = Field(min_length=1, description="e.g. 'Sales' or 'Purchase'")
    godown: str | None = "Main Location"
    batch: str | None = "Primary Batch"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount(self) -> Decimal:
        """Taxable value after discount, rounded to paise."""
        gross = self.quantity * self.rate
        return round_money(gross * (100 - self.discount_pct) / 100)


class Voucher(_Model):
    kind: VoucherKind
    date: dt.date
    party_ledger: str = Field(min_length=1)
    ledgers: list[LedgerLine]
    inventory: list[InventoryLine] = Field(default_factory=list)

    voucher_type: str | None = Field(default=None, description="Custom voucher type name; default = kind")
    number: str | None = Field(default=None, description="None lets Tally number the voucher")
    reference: str | None = Field(default=None, description="Supplier bill number (purchase)")
    reference_date: dt.date | None = None
    narration: str | None = None
    remote_id: str | None = Field(
        default=None, description="Stable id; re-importing the same id alters instead of duplicating"
    )

    # GST context (Tally fills these from the party ledger when omitted)
    state_name: str | None = None
    place_of_supply: str | None = None
    party_gstin: str | None = None
    gst_registration_type: str | None = None

    @property
    def type_name(self) -> str:
        return self.voucher_type or self.kind.value

    def side_total(self, side: Side) -> Decimal:
        total = sum((line.amount for line in self.ledgers if line.side is side), Decimal(0))
        if self.kind.inventory_side is side:
            total += sum((line.amount for line in self.inventory), Decimal(0))
        return round_money(total)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.inventory and not self.kind.is_invoice:
            raise ValueError(f"{self.kind} vouchers cannot have inventory lines")

        party_lines = [line for line in self.ledgers if line.is_party]
        if len(party_lines) != 1:
            raise ValueError("exactly one ledger line must be marked is_party")
        party = party_lines[0]
        if party.ledger != self.party_ledger:
            raise ValueError(f"party line ledger {party.ledger!r} != party_ledger {self.party_ledger!r}")
        if party.side is not self.kind.party_side:
            raise ValueError(f"party must be on the {self.kind.party_side} side for {self.kind}")
        if party.amount <= 0:
            raise ValueError("party amount must be positive")

        debit, credit = self.side_total(Side.DEBIT), self.side_total(Side.CREDIT)
        if debit != credit:
            raise ValueError(f"voucher does not balance: Dr {debit} != Cr {credit}")
        return self


class ImportResult(_Model):
    """Counters Tally returns after an import request."""

    created: int = 0
    altered: int = 0
    deleted: int = 0
    combined: int = 0
    ignored: int = 0
    cancelled: int = 0
    errors: int = 0
    exceptions: int = 0
    last_voucher_id: int | None = None
    line_errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0 and self.exceptions == 0 and not self.line_errors
