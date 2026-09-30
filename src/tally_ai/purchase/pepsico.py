"""Parser for PepsiCo India tax invoices (SAP form YSALEINV..._GST, digital PDFs).

Each item row's first text line holds every number we need. Wrapped lines
below it carry the rest of the description plus the credit-note / route
discount columns, whose digits PepsiCo sometimes wraps onto the next line
("15,006.8" / "3"), so those numbers are never used: the taxable amount on
the first line is authoritative and everything is cross-checked against the
printed grand totals.
"""

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber

from tally_ai.purchase.invoice import InvoiceLine, SupplierInvoice

NUM = r"-?[\d,]+\.\d+"

# 01 <description> <code> <hsn> <pack> <batch><prod date> <mrp/case> <qty> <unit> <base price> <gross>
#    <disc> <addl disc> <taxable> <tax rate/amount pairs...> <line total>
# The description can run straight into the code ("...(300) SE55399").
ROW = re.compile(
    rf"^(?P<no>\d{{2}}) (?P<desc>.+?) ?(?P<code>\d{{5}}) (?P<hsn>\d{{6,8}}) (?P<pack>\d+) "
    rf"(?P<batch>\S*?)(?P<prod>\d{{2}}\.\d{{2}}\.\d{{4}}) "
    rf"(?P<mrp>{NUM}) (?P<qty>{NUM}) (?P<unit>[A-Z]{{2,4}}) "
    rf"(?P<base>{NUM}) (?P<gross>{NUM}) (?P<disc>{NUM}) (?P<addl>{NUM}) (?P<taxable>{NUM}) "
    rf"(?P<tax>(?:{NUM} )+)(?P<total>{NUM})$"
)
GRAND = re.compile(rf"^Grand Total ({NUM}) ((?:{NUM} ?)+)$", re.M)
NOT_DESCRIPTION = re.compile(r"^(?:_{5,}|-{5,}|Printed By|PepsiCo|Grand Total|Total |HSN Code|Page )")


class InvoiceParseError(ValueError):
    pass


def _dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _plain(value: Decimal) -> Decimal:
    """10.00 -> 10, 2.50 -> 2.5 (no exponent notation)."""
    return Decimal(format(value.normalize(), "f"))


def is_pepsico(text: str) -> bool:
    return "PepsiCo India" in text and "Tax Invoice No." in text


def _field(pattern: str, text: str, what: str) -> str:
    m = re.search(pattern, text, re.M)
    if not m:
        raise InvoiceParseError(f"could not find the {what}")
    return m.group(1)


def parse_text(pages: list[str], source_file: str) -> SupplierInvoice:
    text = "\n".join(pages)
    if not is_pepsico(text):
        raise InvoiceParseError("not a PepsiCo tax invoice")
    interstate = "Integrated Tax" in text

    lines: list[InvoiceLine] = []
    rows = text.splitlines()
    i = 0
    while i < len(rows):
        m = ROW.match(rows[i])
        i += 1
        if not m:
            continue
        extra: list[str] = []
        while (
            i < len(rows) and not ROW.match(rows[i]) and not NOT_DESCRIPTION.match(rows[i]) and len(extra) < 3
        ):
            # drop standalone amounts (wrapped discount columns) but keep "31.2g"
            words = re.sub(rf"(?<!\S){NUM}(?!\S)", " ", rows[i]).split()
            extra += [w for w in words if not w.isdigit()]
            i += 1
        g = m.groupdict()
        tax = [_dec(x) for x in g["tax"].split()]
        pack = int(g["pack"])
        if interstate:
            if len(tax) != 2:
                raise InvoiceParseError(f"line {g['no']}: expected IGST rate and amount")
            cgst = sgst = Decimal(0)
            rate, igst = tax
        else:
            if len(tax) != 4:
                raise InvoiceParseError(f"line {g['no']}: expected CGST and SGST rates and amounts")
            cgst_rate, cgst, sgst_rate, sgst = tax
            rate, igst = cgst_rate + sgst_rate, Decimal(0)
        lines.append(
            InvoiceLine(
                number=int(g["no"]),
                description=" ".join([g["desc"].strip(), *extra]),
                item_code=g["code"],
                hsn=g["hsn"],
                pack=pack,
                mrp_per_piece=_plain(_dec(g["mrp"]) / pack),
                quantity=_dec(g["qty"]),
                unit=g["unit"],
                gross=_dec(g["gross"]),
                taxable=_dec(g["taxable"]),
                cgst=cgst,
                sgst=sgst,
                igst=igst,
                tax_rate=rate,
                total=_dec(g["total"]),
            )
        )

    grand = GRAND.search(text)
    if not grand:
        raise InvoiceParseError("could not find the Grand Total row")
    totals = [_dec(x) for x in grand.group(2).split()]
    # gross, discount, additional discount, taxable, <tax...>, total
    if interstate:
        if len(totals) != 6:
            raise InvoiceParseError("unexpected Grand Total layout")
        taxable, igst_total, total = totals[3], totals[4], totals[5]
        cgst_total = sgst_total = Decimal(0)
    else:
        if len(totals) != 7:
            raise InvoiceParseError("unexpected Grand Total layout")
        taxable, cgst_total, sgst_total, total = totals[3], totals[4], totals[5], totals[6]
        igst_total = Decimal(0)

    tcs = re.search(rf"\bTCS\b[^\d\n]*({NUM})", text, re.I)
    return SupplierInvoice(
        source_file=source_file,
        supplier=text.splitlines()[0].strip(),
        supplier_gstin=_field(r"^GSTIN:(\w{15})", text, "supplier GSTIN"),
        buyer_gstin=_field(r"GSTIN No : (\w{15})", text, "buyer GSTIN"),
        invoice_number=_field(r"Tax Invoice No\. : (\d+)", text, "invoice number"),
        invoice_date=datetime.strptime(
            _field(r"Tax Invoice Date : (\d{2}\.\d{2}\.\d{4})", text, "date"), "%d.%m.%Y"
        ).date(),
        interstate=interstate,
        lines=lines,
        taxable=taxable,
        cgst=cgst_total,
        sgst=sgst_total,
        igst=igst_total,
        total=total,
        tcs=_dec(tcs.group(1)) if tcs and _dec(tcs.group(1)) else Decimal(0),
    )


def read_pages(path: Path) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def parse_pdf(path: Path) -> SupplierInvoice:
    try:
        pages = read_pages(path)
    except Exception as e:  # pdfplumber raises many types for damaged / partial files
        raise InvoiceParseError(f"cannot read PDF: {type(e).__name__}: {e}") from e
    if not any(p.strip() for p in pages):
        raise InvoiceParseError("the PDF has no text (scanned image?)")
    return parse_text(pages, str(path))
