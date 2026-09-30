"""Learn mapping rules from invoices that were already entered in Tally.

For each invoice PDF whose number is found in Tally (as a purchase voucher's
reference), every Tally line is matched to the invoice lines it came from:
same pack + MRP, amounts equal within Rs 1 (accountants type rate x qty), and
Tally qty = invoice qty x 1/2/... (the CAS -> Ctn factor). Accountants often
merge several invoice lines into one Tally line; that is handled.

A rule covers a whole product family ("Lays") only when at least two
different flavours of that family, pack and MRP went to the same Tally item;
otherwise it is limited to the flavour words actually seen ("Lays ASCO").
"""

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tally_ai.purchase.invoice import InvoiceLine, SupplierInvoice
from tally_ai.purchase.mapping import MappingRule, leading_words, name_numbers
from tally_ai.tally.client import TallyClient
from tally_ai.tally.parsing import parse_date, parse_decimal, parse_quantity, text
from tally_ai.tally.xml_builder import tdl_string

FACTORS = (Decimal(1), Decimal(2), Decimal("0.5"), Decimal(3), Decimal(4))
MAX_MERGED_LINES = 6


@dataclass(frozen=True)
class TallyLine:
    item: str
    quantity: Decimal
    amount: Decimal


@dataclass(frozen=True)
class TallyBill:
    reference: str
    date: date
    party: str
    number: str | None
    lines: list[TallyLine]


def find_bill(client: TallyClient, voucher_type: str, reference: str) -> TallyBill | None:
    """The purchase voucher whose reference (supplier bill no.) is `reference`, if any."""
    root = client.export_collection(
        "Voucher",
        [
            "Date",
            "VoucherNumber",
            "Reference",
            "PartyLedgerName",
            "AllInventoryEntries.StockItemName",
            "AllInventoryEntries.ActualQty",
            "AllInventoryEntries.Amount",
        ],
        filters=[f"$VoucherTypeName = {tdl_string(voucher_type)}", f"$Reference = {tdl_string(reference)}"],
        from_date=date(2000, 1, 1),
        to_date=date(2099, 12, 31),
    )
    for v in root.iter("VOUCHER"):
        vdate = parse_date(text(v, "DATE"))
        if vdate is None:
            continue
        lines = []
        for entry in v.findall("ALLINVENTORYENTRIES.LIST"):
            qty = parse_quantity(text(entry, "ACTUALQTY"))
            amount = parse_decimal(text(entry, "AMOUNT"))
            name = text(entry, "STOCKITEMNAME")
            if name and qty and amount is not None:
                lines.append(TallyLine(item=name, quantity=qty[0], amount=abs(amount)))
        return TallyBill(
            reference=reference,
            date=vdate,
            party=text(v, "PARTYLEDGERNAME") or "",
            number=text(v, "VOUCHERNUMBER"),
            lines=lines,
        )
    return None


@dataclass(frozen=True)
class Evidence:
    line: InvoiceLine
    tally_item: str
    factor: Decimal
    bill: str


@dataclass
class Alignment:
    evidence: list[Evidence] = field(default_factory=list)
    unmatched_tally: list[TallyLine] = field(default_factory=list)
    unmatched_invoice: list[InvoiceLine] = field(default_factory=list)


def align(invoice: SupplierInvoice, bill: TallyBill) -> Alignment:
    result = Alignment()
    remaining = list(invoice.lines)
    for tline in sorted(bill.lines, key=lambda t: -t.amount):
        tolerance = max(Decimal(1), tline.amount * Decimal("0.0001"))
        hints = name_numbers(tline.item)
        groups: dict[tuple[int, Decimal], list[InvoiceLine]] = defaultdict(list)
        for line in remaining:
            groups[(line.pack, line.mrp_per_piece)].append(line)
        candidates = []
        for (pack, mrp), lines in groups.items():
            for size in range(1, min(len(lines), MAX_MERGED_LINES) + 1):
                for combo in itertools.combinations(lines, size):
                    diff = abs(sum((c.taxable for c in combo), Decimal(0)) - tline.amount)
                    if diff > tolerance:
                        continue
                    qty = sum((c.quantity for c in combo), Decimal(0))
                    factor = next((f for f in FACTORS if qty * f == tline.quantity), None)
                    if factor is None:
                        continue
                    hint_score = (Decimal(pack) in hints) + (mrp in hints)
                    candidates.append((-hint_score, diff, size, combo, factor))
        if not candidates:
            result.unmatched_tally.append(tline)
            continue
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))
        best = candidates[0]
        if len(candidates) > 1 and candidates[1][:3] == best[:3] and set(candidates[1][3]) != set(best[3]):
            result.unmatched_tally.append(tline)  # two equally good explanations: don't guess
            continue
        for line in best[3]:
            result.evidence.append(
                Evidence(line=line, tally_item=tline.item, factor=best[4], bill=bill.reference)
            )
            remaining.remove(line)
    result.unmatched_invoice = remaining
    return result


@dataclass
class LearnReport:
    rules: list[MappingRule] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def rules_from_evidence(evidence: list[Evidence]) -> LearnReport:
    report = LearnReport()
    by_family: dict[tuple[str, int, Decimal], list[Evidence]] = defaultdict(list)
    for e in evidence:
        family = leading_words(e.line.description)[0]
        by_family[(family.lower(), e.line.pack, e.line.mrp_per_piece)].append(e)

    today = date.today().isoformat()
    for (_family, pack, mrp), group in sorted(by_family.items()):
        targets = {(e.tally_item, e.factor) for e in group}
        flavours = {" ".join(leading_words(e.line.description)).lower() for e in group}
        bills = ", ".join(sorted({e.bill for e in group}))
        if len(targets) == 1 and len(flavours) >= 2:
            item, factor = targets.pop()
            keywords = leading_words(group[0].line.description)[0]
            report.rules.append(
                MappingRule(keywords, pack, mrp, item, factor, f"learned from bill {bills}", today)
            )
            continue
        # Flavour-level rules, only where each flavour is consistent
        by_flavour: dict[str, list[Evidence]] = defaultdict(list)
        for e in group:
            by_flavour[" ".join(leading_words(e.line.description))].append(e)
        for flavour, items in sorted(by_flavour.items()):
            flavour_targets = {(e.tally_item, e.factor) for e in items}
            flavour_bills = ", ".join(sorted({e.bill for e in items}))
            if len(flavour_targets) == 1:
                item, factor = flavour_targets.pop()
                report.rules.append(
                    MappingRule(flavour, pack, mrp, item, factor, f"learned from bill {flavour_bills}", today)
                )
            else:
                seen = ", ".join(sorted(f"{i} (x{f})" for i, f in flavour_targets))
                report.conflicts.append(
                    f"{flavour} ({pack}) Rs{mrp}: entered as {seen} in bills {flavour_bills}"
                )
    return report
