"""Supplier invoice as read from a PDF, independent of the supplier's layout."""

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

PAISA = Decimal("0.01")


class InvoiceLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    description: str
    item_code: str
    hsn: str
    pack: int = Field(description="Pieces per case, e.g. 180")
    mrp_per_piece: Decimal
    quantity: Decimal = Field(description="In the supplier's unit, e.g. CAS")
    unit: str
    gross: Decimal
    taxable: Decimal
    cgst: Decimal = Decimal(0)
    sgst: Decimal = Decimal(0)
    igst: Decimal = Decimal(0)
    tax_rate: Decimal = Field(description="Total GST percent, e.g. 5")
    total: Decimal

    @property
    def tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


class SupplierInvoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_file: str
    supplier: str
    supplier_gstin: str
    buyer_gstin: str
    invoice_number: str
    invoice_date: dt.date
    interstate: bool
    lines: list[InvoiceLine]
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal
    tcs: Decimal = Decimal(0)

    def problems(self) -> list[str]:
        """Every way the invoice fails to add up. Empty = safe to use."""
        out = []
        if not self.lines:
            out.append("no item lines found")
        for line in self.lines:
            label = f"line {line.number} ({line.description[:30]})"
            if line.taxable + line.tax != line.total:
                out.append(f"{label}: taxable + tax != line total")
            expected_tax = (line.taxable * line.tax_rate / 100).quantize(PAISA)
            if abs(expected_tax - line.tax) > Decimal("0.02"):
                out.append(f"{label}: tax {line.tax} is not {line.tax_rate}% of {line.taxable}")
            if line.taxable > line.gross or line.quantity <= 0:
                out.append(f"{label}: taxable/quantity out of range")
            if (self.interstate and (line.cgst or line.sgst)) or (not self.interstate and line.igst):
                out.append(f"{label}: tax type does not match the invoice (IGST vs CGST/SGST)")
        sums = {
            "taxable": sum((line.taxable for line in self.lines), Decimal(0)),
            "CGST": sum((line.cgst for line in self.lines), Decimal(0)),
            "SGST": sum((line.sgst for line in self.lines), Decimal(0)),
            "IGST": sum((line.igst for line in self.lines), Decimal(0)),
            "total": sum((line.total for line in self.lines), Decimal(0)),
        }
        printed = {
            "taxable": self.taxable,
            "CGST": self.cgst,
            "SGST": self.sgst,
            "IGST": self.igst,
            "total": self.total,
        }
        for key, value in sums.items():
            if value != printed[key]:
                out.append(f"lines add up to {key} {value}, invoice prints {printed[key]}")
        if self.tcs:
            out.append(f"invoice has TCS {self.tcs}, which is not handled yet")
        return out
