"""Synthetic PepsiCo-style invoice text and masters (all names and numbers are made up)."""

from decimal import Decimal

from tally_ai.accounting.sales_invoice import SalesLedgers
from tally_ai.purchase.build import PurchaseMasters
from tally_ai.tally.masters import Ledger, StockItem

SUPPLIER_GSTIN = "20AAACP1272G1Z1"
WB_GSTIN = "19AAACP1272G1ZK"

HEADER = """PepsiCo India Holdings Pvt Ltd
Ranchi WH - JH (Frito - Lay Division) Example Warehouse
GSTIN:{gstin} PAN No.:AAACP1272G
Memo Information
Parent Id : 0000100001 Ship to party Code . 100001 Tax Invoice No. : {number}
Dealer's Code No. 100001 EXAMPLE TRADERS Tax Invoice Date : 01.10.2026
EXAMPLE MARKET JHARKHAND GSTIN No : 20AAAAA0000A1Z5 Shipment No. : 5000000001
State Code/ POS: 20/ JHARKHAND
____________________________________________________________________________________________________
ID Item Description Item HSN Packs/Batch Prod. MRP/ Qty Unit Base Pr/ Total Amount Disc. Addl. Taxable {taxhead}
No Code Unit No Date Unit(Rs.) Unit (Base Price) (Rs.) Disc.(Rs.) Amount Rate Amount (incl. tax)
____________________________________________________________________________________________________
"""

# Two lines at 5% GST (2.5 + 2.5). Numbers are consistent with each other.
LOCAL_ROWS = """01 Crunchy MM 18.7G RS 5 55419 210690 300 AB14092614.09.2026 1,500.00 10.000 CAS 1,235.16 12,351.60 584.40 217.21 11,550.00 2.50 288.75 2.50 288.75 12,127.50
(300)50 Ext Cn RE 2 0.00 0.00
0.00
02 Chips ASCO 26.5g Rs 10(180) SE55403 210690 180 N311092611.09.2026 1,800.00 2.000 CAS 1,482.21 2,964.42 140.24 58.64 2,765.54 2.50 69.14 2.50 69.14 2,903.82
Gold E NH 5 0.00 0.00
____________________________________________________________________________________________________
Grand Total 12.000 15,316.02 724.64 275.85 14,315.54 357.89 357.89 15,031.32
0.00 0.00
Printed By: 00000000 Print Date: 01.10.2026 Print Time: 10:00:00 Page: 1 of 1
"""

IGST_ROWS = """01 Crunchy MM 18.7G RS 5 55419 210690 300 AB14092614.09.2026 1,500.00 10.000 CAS 1,235.16 12,351.60 584.40 217.21 11,550.00 5.00 577.50 12,127.50
02 Chips ASCO 26.5g Rs 10(180) 55403 210690 180 N311092611.09.2026 1,800.00 2.000 CAS 1,482.21 2,964.42 140.24 58.64 2,765.54 5.00 138.28 2,903.82
____________________________________________________________________________________________________
Grand Total 12.000 15,316.02 724.64 275.85 14,315.54 715.78 15,031.32
"""


def invoice_text(*, igst: bool = False, number: str = "5460000001", rows: str | None = None) -> list[str]:
    header = HEADER.format(
        gstin=WB_GSTIN if igst else SUPPLIER_GSTIN,
        number=number,
        taxhead="Integrated Tax/ IGST Total Amount"
        if igst
        else "Central Tax/ CGST State Tax/SGST Total Amount",
    )
    return [header + (rows if rows is not None else IGST_ROWS if igst else LOCAL_ROWS)]


def item(name: str) -> StockItem:
    return StockItem(name=name, base_unit="Ctn")


def make_masters() -> PurchaseMasters:
    creditors = ("Sundry Creditors", "Current Liabilities")
    ledgers = [
        Ledger(
            name="Snacks Supplier (JH)",
            group_path=creditors,
            gstin=SUPPLIER_GSTIN,
            state="Jharkhand",
            gst_registration_type="Regular",
        ),
        Ledger(
            name="Snacks Supplier (WB)",
            group_path=creditors,
            gstin=WB_GSTIN,
            state="West Bengal",
            gst_registration_type="Regular",
        ),
        Ledger(name="Purchase", group_path=("Purchase Accounts",)),
    ]
    items = [
        item(n)
        for n in ["Crunchy (300) 5/-", "Chips (180) 10/-", "Chips Mini (216) 10/-", "Crunchy (180) 10/-"]
    ]
    return PurchaseMasters(
        company_state="Jharkhand",
        ledgers={x.name: x for x in ledgers},
        items={x.name: x for x in items},
        tax=SalesLedgers(
            sales="Sales",
            interstate_sales="Sales",
            cgst="CGST",
            sgst="SGST",
            igst="IGST",
            round_off="Round Off (+/-)",
            godown="Main Location",
            batch="Primary Batch",
        ),
        purchase_ledger="Purchase",
    )


D = Decimal
