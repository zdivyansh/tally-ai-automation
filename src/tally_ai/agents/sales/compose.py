"""Turn a complete draft into a computed invoice with explanations and warnings."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tally_ai.accounting.dates import date_warnings
from tally_ai.accounting.sales_invoice import LineInput, SalesInvoice, build_sales_invoice
from tally_ai.agents.sales.draft import RateSource, SalesDraft
from tally_ai.masters.cache import MasterData
from tally_ai.tally.masters import GstRate


@dataclass(frozen=True)
class LineNote:
    rate_source: str
    discount_source: str
    gst_source: str


@dataclass(frozen=True)
class ComposedInvoice:
    invoice: SalesInvoice
    notes: list[LineNote]
    warnings: list[str] = field(default_factory=list)


class DraftIncomplete(ValueError):
    pass


def compose_invoice(
    draft: SalesDraft,
    masters: MasterData,
    *,
    number: str,
    today: date,
    voucher_type: str,
) -> ComposedInvoice:
    if draft.party is None or draft.date is None:
        raise DraftIncomplete("draft has no party or date")
    party = masters.ledgers[draft.party]
    lines: list[LineInput] = []
    notes: list[LineNote] = []
    warnings = list(date_warnings(draft.date, today))

    for line in draft.lines:
        if line.item is None or line.quantity is None or line.rate is None or line.rate_source is None:
            raise DraftIncomplete(f"line {line.text!r} is incomplete")
        item = masters.items[line.item]

        if line.discount_pct is not None:
            discount, discount_source = line.discount_pct, "from message"
        else:
            last = masters.history.last_for_customer(party.name, item.name)
            discount = last.discount_pct if last else Decimal(0)
            discount_source = (
                f"customer's last ({last.date:%d-%b-%Y})" if last else "none (first purchase of this item)"
            )

        gst = masters.gst_rate(item, draft.date)
        if gst is None:
            if line.gst_pct is None:
                raise DraftIncomplete(f"no GST rate for {item.name!r}")
            half = line.gst_pct / 2
            gst = GstRate(applicable_from=draft.date, igst=line.gst_pct, cgst=half, sgst=half)
            gst_source = "entered"
        else:
            gst_source = f"Tally, from {gst.applicable_from:%d-%b-%Y}"

        if item.closing_quantity is not None and line.quantity > item.closing_quantity:
            stock = f"{item.closing_quantity.normalize():f} {item.base_unit or ''}".rstrip()
            warnings.append(f"Stock of {item.name} is {stock}; this sale is {line.quantity.normalize():f}.")

        rate_source = line.rate_source.value
        if line.rate_source in (RateSource.ITEM_LAST, RateSource.CUSTOMER_LAST):
            rate_source = f"{rate_source} (you chose)"
        lines.append(
            LineInput(item=item, quantity=line.quantity, rate=line.rate, discount_pct=discount, gst=gst)
        )
        notes.append(
            LineNote(rate_source=rate_source, discount_source=discount_source, gst_source=gst_source)
        )

    invoice = build_sales_invoice(
        party=party,
        company_state=masters.company_state,
        lines=lines,
        ledgers=masters.sales_ledgers,
        voucher_date=draft.date,
        number=number,
        voucher_type=voucher_type,
        narration=draft.narration,
        remote_id=draft.remote_id,
    )
    return ComposedInvoice(invoice=invoice, notes=notes, warnings=warnings)
