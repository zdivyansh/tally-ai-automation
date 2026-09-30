"""Synthetic master data for agent tests (all names are made up)."""

from datetime import date
from decimal import Decimal

from tally_ai.accounting.history import SalesHistory
from tally_ai.accounting.sales_invoice import SalesLedgers
from tally_ai.agents.sales.extraction import ExtractedItem, Extraction
from tally_ai.masters.cache import MasterData
from tally_ai.masters.matcher import Matcher
from tally_ai.tally.masters import GstDetails, GstRate, Ledger, SalesLine, StockGroup, StockItem

D = Decimal
TODAY = date(2026, 9, 30)
DEBTORS = ("Sundry Debtors", "Current Assets")


def rates(*pairs: tuple[date, int]) -> GstDetails:
    return GstDetails(
        source="Specify Details Here",
        rates=tuple(GstRate(applicable_from=d, igst=D(p), cgst=D(p) / 2, sgst=D(p) / 2) for d, p in pairs),
    )


INHERIT = GstDetails(source="As per Company/Stock Group")


def debtor(name: str, state: str = "Jharkhand") -> Ledger:
    return Ledger(name=name, parent="Sundry Debtors", group_path=DEBTORS, state=state)


LEDGERS = [
    debtor("Sharma Traders"),
    debtor("Gupta Store"),
    debtor("Gupta Store (Main Road)"),
    debtor("GUPTA STORE (STATION)"),
    debtor("Verma General Store"),
    debtor("Patna Wholesale", state="Bihar"),
    Ledger(name="Snacks Co Distributors", parent="Sundry Creditors", group_path=("Sundry Creditors",)),
    Ledger(name="Sales", group_path=("Sales Accounts",)),
    Ledger(name="Inter-State Sale", group_path=("Sales Accounts",)),
    Ledger(name="CGST", group_path=("Duties & Taxes",), gst_duty_head="CGST"),
    Ledger(name="SGST", group_path=("Duties & Taxes",), gst_duty_head="SGST/UTGST"),
    Ledger(name="IGST", group_path=("Duties & Taxes",), gst_duty_head="IGST"),
    Ledger(name="Round Off (+/-)", group_path=("Direct Expenses",)),
]

STOCK_GROUPS = {
    "Snacks Co": StockGroup(name="Snacks Co", gst=rates((date(2017, 7, 1), 12), (date(2025, 9, 22), 5))),
    "Dairy Co": StockGroup(name="Dairy Co", gst=INHERIT),
}


def item(name: str, group: str, closing: int | None = 100, gst: GstDetails = INHERIT) -> StockItem:
    return StockItem(
        name=name,
        parent=group,
        base_unit="Ctn",
        gst=gst,
        closing_quantity=D(closing) if closing is not None else None,
    )


ITEMS = [
    item("Crunchy (300) 5/-", "Snacks Co", closing=-40),
    item("Crunchy (270) 5/-", "Snacks Co"),
    item("Crunchy Puff (270) 5/-", "Snacks Co"),
    item("Masala Puff (240) 5/-", "Snacks Co"),
    item("Aloo Bhujia 200g", "Snacks Co", gst=rates((date(2017, 7, 1), 18))),
    item("Pure Ghee 1 Ltr", "Dairy Co"),
]

HISTORY = [
    SalesLine(
        date=date(2026, 9, 10),
        master_id=1,
        number="TST/26-27/0003",
        party="Sharma Traders",
        stock_item="Crunchy (300) 5/-",
        rate=D(1300),
        unit="Ctn",
        discount_pct=D(12),
    ),
    SalesLine(
        date=date(2026, 9, 28),
        master_id=2,
        number="TST/26-27/0007",
        party="GUPTA STORE (STATION)",
        stock_item="Crunchy (300) 5/-",
        rate=D("1310.63"),
        unit="Ctn",
        discount_pct=D(11),
    ),
]

SALES_LEDGERS = SalesLedgers(
    sales="Sales",
    interstate_sales="Inter-State Sale",
    cgst="CGST",
    sgst="SGST",
    igst="IGST",
    round_off="Round Off (+/-)",
    godown="Main Location",
    batch="Primary Batch",
)


def make_masters() -> MasterData:
    ledgers = {ledger.name: ledger for ledger in LEDGERS}
    items = {i.name: i for i in ITEMS}
    return MasterData(
        company_state="Jharkhand",
        ledgers=ledgers,
        items=items,
        stock_groups=dict(STOCK_GROUPS),
        history=SalesHistory(HISTORY),
        sales_ledgers=SALES_LEDGERS,
        party_matcher=Matcher((x.name, x.aliases) for x in LEDGERS if x.is_under("Sundry Debtors")),
        item_matcher=Matcher((i.name, i.aliases) for i in ITEMS),
    )


def sale(party: str | None, *items: tuple[str, float | None], **fields: object) -> Extraction:
    return Extraction(
        intent="sales",
        party=party,
        items=[ExtractedItem(name=name, quantity=qty, unit="ctn") for name, qty in items],
        **fields,  # type: ignore[arg-type]
    )
