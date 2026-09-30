"""Review one invoice with the user: map new products, show the entry, post on 'yes'."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from tally_ai.agents.sales.render import inr
from tally_ai.audit import AuditLog
from tally_ai.masters.matcher import Matcher
from tally_ai.purchase.build import (
    MappedLine,
    PurchaseError,
    PurchaseMasters,
    build_purchase_voucher,
    map_lines,
)
from tally_ai.purchase.invoice import InvoiceLine, SupplierInvoice
from tally_ai.purchase.learn import TallyBill, find_bill
from tally_ai.purchase.mapping import MappingFileError, MappingRule, MappingTable, leading_words, name_numbers
from tally_ai.purchase.pepsico import InvoiceParseError, parse_pdf
from tally_ai.purchase.state import PurchaseState
from tally_ai.tally.client import TallyClient
from tally_ai.tally.errors import TallyError
from tally_ai.tally.xml_builder import build_import_request

Outcome = Literal["posted", "duplicate", "ignored", "unsupported", "failed", "skipped"]
YES = {"yes", "y", "ok", "haan", "han", "ha", "post"}
NO = {"no", "n", "nahi", "skip", "later"}
IGNORE = {"ignore", "never"}
MAX_SUGGESTIONS = 6


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    message: str


@dataclass
class ReviewContext:
    masters: PurchaseMasters
    mapping: MappingTable
    client: TallyClient
    state: PurchaseState
    voucher_type: str = "Purchase"
    audit: AuditLog | None = None


Ask = Callable[[str], str]
Say = Callable[[str], None]


def _num(value: Decimal) -> str:
    return f"{value.normalize():f}"


def product_label(line: InvoiceLine) -> str:
    return f"{' '.join(leading_words(line.description))} Rs{_num(line.mrp_per_piece)} ({line.pack})"


def suggestions(line: InvoiceLine, masters: PurchaseMasters, conflicts: list[MappingRule]) -> list[str]:
    """Candidate Tally items: conflicting rules first, then items named with the same pack and MRP."""
    out = [r.tally_item for r in conflicts if r.tally_item in masters.items]
    same_numbers = [
        name for name in masters.items if {Decimal(line.pack), line.mrp_per_piece} <= name_numbers(name)
    ]
    query = f"{' '.join(leading_words(line.description))} {line.pack} {_num(line.mrp_per_piece)}"
    ranked = Matcher((n, []) for n in same_numbers).match(query, limit=MAX_SUGGESTIONS).candidates
    out += [c.name for c in ranked]
    out += [n for n in sorted(same_numbers) if n not in out]
    if len(out) < MAX_SUGGESTIONS:
        fuzzy = Matcher((n, []) for n in masters.items).match(query, limit=MAX_SUGGESTIONS).candidates
        out += [c.name for c in fuzzy]
    return list(dict.fromkeys(out))[:MAX_SUGGESTIONS]


def render_entry(invoice: SupplierInvoice, mapped: list[MappedLine], supplier: str) -> str:
    tax = "IGST" if invoice.interstate else "CGST + SGST"
    out = [
        f"PepsiCo invoice {invoice.invoice_number} dated {invoice.invoice_date:%d-%b-%Y}"
        f"  ({Path(invoice.source_file).name})",
        f"Supplier: {supplier}  |  GSTIN {invoice.supplier_gstin}  |  {tax}",
        "",
    ]
    for m in mapped:
        assert m.rule is not None and m.quantity is not None
        line = m.line
        factor = f"  (1 {line.unit} = {_num(m.rule.ctn_per_case)} Ctn)" if m.rule.ctn_per_case != 1 else ""
        rate = (line.taxable / m.quantity).quantize(Decimal("0.01"))
        out.append(f"{line.number:>2}. {line.description[:44]}")
        out.append(
            f"    -> {m.rule.tally_item}: {_num(m.quantity)} Ctn x {inr(rate)} = {inr(line.taxable)}{factor}"
        )
    out.append("")
    out.append(f"   Taxable   {inr(invoice.taxable)}")
    if invoice.interstate:
        out.append(f"   IGST      {inr(invoice.igst)}")
    else:
        out.append(f"   CGST      {inr(invoice.cgst)}")
        out.append(f"   SGST      {inr(invoice.sgst)}")
    out.append(f"   Total     {inr(invoice.total)}   (matches the invoice)")
    out.append("")
    out.append("Reply 'yes' to post, 'no' to skip for now, or 'ignore' to never show this invoice again.")
    return "\n".join(out)


def _ask_mapping(
    line: InvoiceLine, conflicts: list[MappingRule], ctx: ReviewContext, ask: Ask, say: Say
) -> str | None:
    """Ask which Tally item a new product is; returns the item, or None to skip the invoice."""
    options = suggestions(line, ctx.masters, conflicts)
    note = ""
    if conflicts:
        note = (
            "The mapping file has conflicting rules for this product: "
            + ", ".join(f"{r.tally_item} ({r.source or 'no source'})" for r in conflicts)
            + "\n"
        )
    while True:
        listing = "\n".join(f"  {i}. {name}" for i, name in enumerate(options, 1)) or "  (no suggestions)"
        answer = ask(
            f"{note}New product on the invoice: {line.description}\n"
            f"  code {line.item_code}, {line.pack} pieces per {line.unit}, MRP Rs{_num(line.mrp_per_piece)}\n"
            f"Which Tally item is it?\n{listing}\n"
            "Reply with a number, type part of the Tally name to search, "
            "or 'skip' to leave this invoice for now."
        ).strip()
        if answer.lower() in NO | {"cancel"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        exact = next((n for n in ctx.masters.items if n.lower() == answer.lower()), None)
        if exact:
            return exact
        found = Matcher((n, []) for n in ctx.masters.items).match(answer, limit=MAX_SUGGESTIONS).candidates
        if found:
            options, note = [c.name for c in found], ""
        else:
            say(f"No Tally item matches '{answer}'.")


def _ask_scope(line: InvoiceLine, item: str, ask: Ask) -> str:
    flavour = leading_words(line.description)
    family = flavour[0]
    if len(flavour) == 1:
        return family
    size = f"Rs{_num(line.mrp_per_piece)} ({line.pack})"
    while True:
        answer = ask(
            f"Remember '{item}' for:\n"
            f"  1. all '{family}' {size} products\n"
            f"  2. only '{' '.join(flavour)}' {size}\n"
            "Reply 1 or 2."
        ).strip()
        if answer == "1":
            return family
        if answer == "2":
            return " ".join(flavour)


def _save_rule(ctx: ReviewContext, rule: MappingRule, ask: Ask, say: Say) -> None:
    ctx.mapping.add(rule)
    while True:
        try:
            ctx.mapping.save()
            return
        except MappingFileError as e:
            answer = ask(
                f"{e}. Press Enter to retry, or type 'skip' to keep the rule only until the watcher stops."
            )
            if answer.strip().lower() == "skip":
                say("The rule is used now but not saved to the file.")
                return


def review_file(path: Path, ctx: ReviewContext, ask: Ask, say: Say) -> Result:
    try:
        invoice = parse_pdf(path)
    except InvoiceParseError as e:
        unsupported = "not a PepsiCo" in str(e)
        ctx.state.record(path, "unsupported" if unsupported else "failed", message=str(e))
        return Result("unsupported" if unsupported else "failed", f"{path.name}: {e}")

    def record(outcome: Outcome, message: str, voucher_number: str | None = None) -> Result:
        if outcome != "skipped":
            ctx.state.record(
                path,
                outcome,
                invoice_number=invoice.invoice_number,
                supplier_gstin=invoice.supplier_gstin,
                voucher_number=voucher_number,
                message=message,
            )
        return Result(outcome, message)

    problems = invoice.problems()
    if problems:
        return record("failed", f"{path.name}: invoice does not add up, not posted: " + "; ".join(problems))
    try:
        supplier = ctx.masters.supplier_ledger(invoice.supplier_gstin)
    except PurchaseError as e:
        return record("failed", f"{path.name}: {e}")

    try:
        bill = find_bill(ctx.client, ctx.voucher_type, invoice.invoice_number)
    except TallyError as e:
        return Result("failed", f"{path.name}: cannot check Tally for duplicates: {e}")
    if bill is not None:
        return record(
            "duplicate",
            f"{path.name}: invoice {invoice.invoice_number} is already in Tally "
            f"(voucher {bill.number or '?'} dated {bill.date:%d-%b-%Y}, {bill.party}); not posted again.",
        )

    ctx.mapping.reload_if_changed()
    mapped = map_lines(invoice, ctx.mapping, ctx.masters)
    asked: set[tuple[str, int, Decimal]] = set()
    for m in mapped:
        if m.rule is not None:
            continue
        key = (" ".join(leading_words(m.line.description)).lower(), m.line.pack, m.line.mrp_per_piece)
        if key in asked:
            continue
        asked.add(key)
        item = _ask_mapping(m.line, m.conflict, ctx, ask, say)
        if item is None:
            return Result("skipped", f"{path.name}: skipped for now (products not mapped).")
        keywords = _ask_scope(m.line, item, ask)
        _save_rule(
            ctx, MappingRule(keywords, m.line.pack, m.line.mrp_per_piece, item, Decimal(1), "you"), ask, say
        )
        mapped = map_lines(invoice, ctx.mapping, ctx.masters)

    try:
        voucher = build_purchase_voucher(invoice, mapped, ctx.masters, voucher_type=ctx.voucher_type)
    except PurchaseError as e:
        return record("failed", f"{path.name}: {e}")

    while True:
        answer = ask(render_entry(invoice, mapped, supplier.name)).strip().lower()
        if answer in YES | NO | IGNORE:
            break
    if answer in IGNORE:
        return record("ignored", f"{path.name}: ignored; it will not be shown again.")
    if answer in NO:
        return Result("skipped", f"{path.name}: skipped for now; it will be shown again next time.")

    conversation = f"purchase-{invoice.supplier_gstin}-{invoice.invoice_number}-{path.stat().st_mtime:.0f}"
    if ctx.audit:
        ctx.audit.start(conversation, channel="purchase", user=None, message=str(path))
    try:
        result = ctx.client.import_vouchers([voucher])
    except TallyError as e:
        if ctx.audit:
            import_result = getattr(e, "result", None)
            ctx.audit.posting(
                conversation,
                request_xml=build_import_request([voucher]),
                ok=False,
                voucher_number=None,
                voucher_date=voucher.date.isoformat(),
                party=voucher.party_ledger,
                total=str(invoice.total),
                remote_id=voucher.remote_id,
                result=import_result.model_dump() if import_result else None,
                error=str(e),
            )
            ctx.audit.finish(conversation, status="failed", result=str(e))
        return record("failed", f"{path.name}: Tally did not accept the purchase: {e}")

    posted: TallyBill | None = find_bill(ctx.client, ctx.voucher_type, invoice.invoice_number)
    number = posted.number if posted else None
    message = (
        f"Posted purchase voucher {number or '?'} for invoice {invoice.invoice_number} "
        f"({supplier.name}), total {inr(invoice.total)}."
    )
    if ctx.audit:
        ctx.audit.posting(
            conversation,
            request_xml=build_import_request([voucher]),
            ok=True,
            voucher_number=number,
            voucher_date=voucher.date.isoformat(),
            party=voucher.party_ledger,
            total=str(invoice.total),
            remote_id=voucher.remote_id,
            result=result.model_dump(),
        )
        ctx.audit.finish(
            conversation,
            status="posted",
            result=message,
            party=voucher.party_ledger,
            voucher_date=voucher.date.isoformat(),
            voucher_number=number,
            total=str(invoice.total),
            remote_id=voucher.remote_id,
        )
    return record("posted", message, voucher_number=number)
