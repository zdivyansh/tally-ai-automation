"""Snapshot of the master data the sales agent needs, loaded once per session."""

import logging
from dataclasses import dataclass
from datetime import date

from tally_ai.accounting.dates import fy_end, fy_start
from tally_ai.accounting.history import SalesHistory
from tally_ai.accounting.sales_invoice import SalesLedgers
from tally_ai.config import Settings
from tally_ai.masters.matcher import Matcher
from tally_ai.tally.masters import GstRate, Ledger, StockGroup, StockItem, resolve_item_gst
from tally_ai.tally.queries import TallyQueries

logger = logging.getLogger(__name__)

DEBTORS = "Sundry Debtors"


class MasterDataError(RuntimeError):
    """Tally masters are missing something the agent needs (e.g. a ledger)."""


@dataclass
class MasterData:
    company_state: str
    ledgers: dict[str, Ledger]
    items: dict[str, StockItem]
    stock_groups: dict[str, StockGroup]
    history: SalesHistory
    sales_ledgers: SalesLedgers
    party_matcher: Matcher
    item_matcher: Matcher

    @classmethod
    def load(cls, queries: TallyQueries, settings: Settings, today: date) -> "MasterData":
        companies = queries.companies()
        company_states = {c.state for c in companies if c.state}
        if len(company_states) != 1:
            raise MasterDataError(
                f"expected one open company with a state, found {[(c.name, c.state) for c in companies]}; "
                "set TALLY_COMPANY"
            )
        ledgers = {ledger.name: ledger for ledger in queries.ledgers()}
        items = {item.name: item for item in queries.stock_items()}
        stock_groups = queries.stock_groups()
        # Current and previous financial year are enough for rates and discounts
        history_from = fy_start(date(fy_start(today).year - 1, 4, 1))
        lines = queries.sales_lines(
            voucher_type=settings.sales_voucher_type, from_date=history_from, to_date=fy_end(today)
        )
        logger.info("loaded %d ledgers, %d items, %d sales lines", len(ledgers), len(items), len(lines))

        debtors = [ledger for ledger in ledgers.values() if ledger.is_under(DEBTORS)]
        return cls(
            company_state=company_states.pop(),
            ledgers=ledgers,
            items=items,
            stock_groups=stock_groups,
            history=SalesHistory(lines),
            sales_ledgers=resolve_sales_ledgers(ledgers, settings),
            party_matcher=Matcher((ledger.name, ledger.aliases) for ledger in debtors),
            item_matcher=Matcher((item.name, item.aliases) for item in items.values()),
        )

    def gst_rate(self, item: StockItem, on: date) -> GstRate | None:
        return resolve_item_gst(item, self.stock_groups, on)


def resolve_sales_ledgers(ledgers: dict[str, Ledger], settings: Settings) -> SalesLedgers:
    def require(name: str, setting: str) -> str:
        if name not in ledgers:
            raise MasterDataError(f"ledger {name!r} ({setting}) does not exist in Tally")
        return name

    def duty_ledger(head: str, configured: str | None, setting: str) -> str:
        if configured:
            return require(configured, setting)
        found = [ledger.name for ledger in ledgers.values() if ledger.gst_duty_head == head]
        if len(found) != 1:
            raise MasterDataError(f"cannot pick the {head} ledger from {found}; set {setting}")
        return found[0]

    if settings.round_off_ledger:
        round_off = require(settings.round_off_ledger, "ROUND_OFF_LEDGER")
    else:
        found = [name for name in ledgers if "round" in name.lower()]
        if len(found) != 1:
            raise MasterDataError(f"cannot pick the round-off ledger from {found}; set ROUND_OFF_LEDGER")
        round_off = found[0]

    sales = require(settings.sales_ledger, "SALES_LEDGER")
    return SalesLedgers(
        sales=sales,
        interstate_sales=require(settings.sales_interstate_ledger, "SALES_INTERSTATE_LEDGER")
        if settings.sales_interstate_ledger
        else sales,
        cgst=duty_ledger("CGST", settings.cgst_ledger, "CGST_LEDGER"),
        sgst=duty_ledger("SGST/UTGST", settings.sgst_ledger, "SGST_LEDGER"),
        igst=duty_ledger("IGST", settings.igst_ledger, "IGST_LEDGER"),
        round_off=round_off,
        godown=settings.godown,
        batch=settings.batch,
    )
