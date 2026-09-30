"""Plain-text messages for the user. Channel-agnostic (CLI now, chat apps later)."""

from decimal import Decimal

from tally_ai.agents.sales.compose import ComposedInvoice
from tally_ai.agents.sales.draft import Question, QuestionKind, SalesDraft
from tally_ai.masters.cache import MasterData

CONFIRM_HINT = "Reply 'yes' to post, 'no' to cancel, or type a correction (e.g. 'rate 1300', 'qty 5')."


def inr(value: Decimal) -> str:
    """Indian digit grouping: 123456.5 -> '1,23,456.50'."""
    sign = "-" if value < 0 else ""
    whole, _, paise = f"{abs(value):.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join([*groups, tail])}.{paise}"


def _num(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _choices(choices: tuple[str, ...]) -> str:
    return "\n".join(f"  {i}. {name}" for i, name in enumerate(choices, 1))


def render_question(question: Question, draft: SalesDraft, masters: MasterData) -> str:
    line = draft.lines[question.line] if question.line is not None else None

    match question.kind:
        case QuestionKind.DATE:
            return (
                f"I could not read the date '{draft.date_text}'. Which date? (e.g. 25/09/2026, 'aaj', 'kal')"
            )
        case QuestionKind.PARTY_NAME:
            return "Which customer is this sale for?"
        case QuestionKind.PARTY_CHOICE:
            if not question.choices:
                return f"No customer matches '{draft.party_text}'. Please type the customer name again."
            return (
                f"Which customer is '{draft.party_text}'?\n{_choices(question.choices)}\n"
                "Reply with a number, or type the name again."
            )
        case QuestionKind.ITEM_CHOICE:
            assert line is not None
            if not question.choices:
                return f"No item matches '{line.text}'. Please type the item name again."
            return (
                f"Which item is '{line.text}'?\n{_choices(question.choices)}\n"
                "Reply with a number, or type the name again."
            )
        case QuestionKind.QUANTITY:
            assert line is not None and line.item is not None
            unit = masters.items[line.item].base_unit or ""
            return f"How many {unit} of {line.item}?".replace("  ", " ")
        case QuestionKind.UNIT:
            assert line is not None and line.item is not None
            base_unit = masters.items[line.item].base_unit
            return (
                f"{line.item} is billed in {base_unit}, but you wrote "
                f"{_num(line.quantity or Decimal(0))} {line.unit_text}. How many {base_unit}?"
            )
        case QuestionKind.RATE:
            assert line is not None and line.item is not None and draft.party is not None
            unit = masters.items[line.item].base_unit or "unit"
            header = f"Rate for {line.item} ({_num(line.quantity or Decimal(0))} {unit})?"
            customer = masters.history.last_for_customer(draft.party, line.item)
            anyone = masters.history.last_for_item(line.item)
            if anyone is None:
                return f"{header}\nThis item has no past sales. Please type the rate per {unit}."
            hints = []
            if customer:
                hints.append(
                    f"  - last to {draft.party}: {inr(customer.rate)}/{unit} on {customer.date:%d-%b-%Y}"
                )
            else:
                hints.append(f"  - {draft.party} has not bought this item before")
            hints.append(
                f"  - last to anyone: {inr(anyone.rate)}/{unit} on {anyone.date:%d-%b-%Y} ({anyone.party})"
            )
            options = "'same' (last to anyone)" + (", 'customer' (last to this customer)" if customer else "")
            return f"{header}\n" + "\n".join(hints) + f"\nReply with a rate, {options}."
        case QuestionKind.GST:
            assert line is not None
            return f"Tally has no GST rate for {line.item}. What GST % applies? (e.g. 5, 12, 18, 0)"
    raise AssertionError(question.kind)


def render_invoice(composed: ComposedInvoice, draft: SalesDraft, masters: MasterData) -> str:
    inv = composed.invoice
    v = inv.voucher
    party = masters.ledgers[v.party_ledger]
    out = [
        f"Sales invoice {v.number}  |  {v.date:%d-%b-%Y}",
        f"Customer: {party.name} ({party.state}{', IGST' if inv.interstate else ''})",
        "",
    ]
    for i, (line, note) in enumerate(zip(v.inventory, composed.notes, strict=True), 1):
        discount = f" less {_num(line.discount_pct)}%" if line.discount_pct else ""
        out.append(f"{i}. {line.stock_item}")
        out.append(f"   {_num(line.quantity)} {line.unit} x {inr(line.rate)}{discount} = {inr(line.amount)}")
        out.append(f"   rate: {note.rate_source}; discount: {note.discount_source}")
    out.append("")
    out.append(f"   Taxable     {inr(inv.taxable)}")
    for tax in inv.taxes:
        out.append(f"   {tax.label:<11} {inr(tax.amount)}")
    if inv.round_off:
        out.append(f"   Round off   {inr(inv.round_off)}")
    out.append(f"   Total       {inr(inv.total)}")
    if composed.warnings:
        out.append("")
        out.extend(f"Warning: {w}" for w in composed.warnings)
    out.append("")
    out.append(CONFIRM_HINT)
    return "\n".join(out)
