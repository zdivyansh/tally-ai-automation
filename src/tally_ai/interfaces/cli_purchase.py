"""`tally-ai purchase ...` commands."""

import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import typer

from tally_ai.config import Settings, get_settings

purchase_app = typer.Typer(
    help="Purchase entry from supplier invoice PDFs (docs/purchase-invoices.md).", no_args_is_help=True
)


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def invoice_dir(settings: Settings) -> Path:
    if not settings.purchase_invoice_dir:
        typer.echo(
            "Set PURCHASE_INVOICE_DIR in .env to the full path of the folder where invoice PDFs are saved."
        )
        raise typer.Exit(code=2)
    folder = Path(settings.purchase_invoice_dir).expanduser()
    if not folder.is_dir():
        typer.echo(f"PURCHASE_INVOICE_DIR does not exist: {folder}")
        raise typer.Exit(code=2)
    return folder


@purchase_app.command("learn")
def learn() -> None:
    """Pre-fill the mapping from invoice PDFs whose bills are already entered in Tally."""
    from tally_ai.purchase.learn import Evidence, align, find_bill, rules_from_evidence
    from tally_ai.purchase.mapping import FILE_NAME, MappingTable
    from tally_ai.purchase.pepsico import InvoiceParseError, parse_pdf
    from tally_ai.purchase.watch import pdfs_in
    from tally_ai.tally import TallyClient

    _utf8_stdout()
    settings = get_settings()
    folder = invoice_dir(settings)
    mapping = MappingTable(folder / FILE_NAME)
    mapping.reload_if_changed()
    evidence: list[Evidence] = []
    with TallyClient.from_settings(settings) as client:
        for path in pdfs_in(folder):
            try:
                invoice = parse_pdf(path)
            except InvoiceParseError as e:
                typer.echo(f"  {path.name}: skipped ({e})")
                continue
            if invoice.problems():
                typer.echo(f"  {path.name}: skipped (does not add up)")
                continue
            bill = find_bill(client, settings.purchase_voucher_type, invoice.invoice_number)
            if bill is None:
                continue
            result = align(invoice, bill)
            evidence += result.evidence
            typer.echo(
                f"  {path.name}: in Tally; {len(result.evidence)} of {len(invoice.lines)} lines explained"
            )

    report = rules_from_evidence(evidence)
    existing = {(tuple(r.keyword_list), r.pack, r.mrp) for r in mapping.rules}
    added = [r for r in report.rules if (tuple(r.keyword_list), r.pack, r.mrp) not in existing]
    for rule in added:
        mapping.add(rule)
    if added:
        mapping.save()
    typer.echo(
        f"\nAdded {len(added)} rules to {mapping.path} ({len(report.rules) - len(added)} were already there):"
    )
    for r in added:
        factor = f"  (1 case = {r.ctn_per_case} Ctn)" if r.ctn_per_case != 1 else ""
        typer.echo(f"  {r.keywords} ({r.pack}) Rs{r.mrp} -> {r.tally_item}{factor}   [{r.source}]")
    if report.conflicts:
        typer.echo("\nNot added, because different bills entered them differently (you will be asked):")
        for c in report.conflicts:
            typer.echo(f"  {c}")


def _fmt(value: list[Decimal] | None) -> str:
    return f"{value[0].normalize():f} Ctn {value[1]:>11}" if value else "-".rjust(20)


@purchase_app.command("check")
def check(file: str = typer.Argument(..., help="Invoice PDF")) -> None:
    """Dry run: read one invoice, show the entry it would make, compare with Tally. Posts nothing."""
    from tally_ai.purchase.build import PurchaseMasters, map_lines
    from tally_ai.purchase.learn import find_bill
    from tally_ai.purchase.mapping import FILE_NAME, MappingTable
    from tally_ai.purchase.pepsico import parse_pdf
    from tally_ai.purchase.review import product_label, render_entry
    from tally_ai.tally import TallyClient, TallyQueries

    _utf8_stdout()
    settings = get_settings()
    invoice = parse_pdf(Path(file))
    problems = invoice.problems()
    status = "adds up" if not problems else "DOES NOT ADD UP: " + "; ".join(problems)
    typer.echo(
        f"{invoice.invoice_number} {invoice.invoice_date:%d-%b-%Y}: {len(invoice.lines)} lines, "
        f"total {invoice.total} - {status}"
    )
    mapping = MappingTable(invoice_dir(settings) / FILE_NAME)
    mapping.reload_if_changed()
    with TallyClient.from_settings(settings) as client:
        masters = PurchaseMasters.load(TallyQueries(client), settings)
        mapped = map_lines(invoice, mapping, masters)
        bill = find_bill(client, settings.purchase_voucher_type, invoice.invoice_number)

    unmapped = [m for m in mapped if m.rule is None]
    if unmapped:
        typer.echo("\nNot mapped yet (the watcher would ask):")
        for m in unmapped:
            typer.echo(f"  {m.line.number}. {product_label(m.line)} - {m.line.description}")
    else:
        typer.echo("\n" + render_entry(invoice, mapped, masters.supplier_ledger(invoice.supplier_gstin).name))
    if bill is None:
        typer.echo("\nNot in Tally yet.")
        return

    typer.echo(f"\nAlready in Tally as voucher {bill.number} dated {bill.date:%d-%b-%Y}. Per Tally item:")
    ours: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for m in mapped:
        if m.rule and m.quantity is not None:
            ours[m.rule.tally_item][0] += m.quantity
            ours[m.rule.tally_item][1] += m.line.taxable
    theirs: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for t in bill.lines:
        theirs[t.item][0] += t.quantity
        theirs[t.item][1] += t.amount
    for name in sorted(set(ours) | set(theirs)):
        a, b = ours.get(name), theirs.get(name)
        same = a is not None and b is not None and a[0] == b[0] and abs(a[1] - b[1]) <= 1
        typer.echo(f"  {'same' if same else 'DIFF'}  {name:<26} agent {_fmt(a)} | Tally {_fmt(b)}")


@purchase_app.command("watch")
def watch() -> None:
    """Watch the invoice folder; review each new PepsiCo invoice and post it after your 'yes'."""
    from tally_ai.audit import AuditLog
    from tally_ai.purchase.build import PurchaseMasters
    from tally_ai.purchase.mapping import FILE_NAME, MappingTable
    from tally_ai.purchase.review import ReviewContext
    from tally_ai.purchase.state import PurchaseState
    from tally_ai.purchase.watch import FolderWatcher
    from tally_ai.tally import TallyClient, TallyQueries

    _utf8_stdout()
    settings = get_settings()
    folder = invoice_dir(settings)
    mapping = MappingTable(folder / FILE_NAME)
    audit = AuditLog(settings.audit_db_path)
    state = PurchaseState(settings.audit_db_path)
    with TallyClient.from_settings(settings) as client:
        queries = TallyQueries(client)

        def make_context() -> ReviewContext:
            # Reloaded every poll so new Tally items or ledgers are seen without restarting
            return ReviewContext(
                masters=PurchaseMasters.load(queries, settings),
                mapping=mapping,
                client=client,
                state=state,
                voucher_type=settings.purchase_voucher_type,
                audit=audit,
            )

        def ask(prompt: str) -> str:
            typer.echo(f"\n{prompt}\n")
            try:
                return input("you> ")
            except EOFError:
                raise KeyboardInterrupt from None  # input closed: stop watching

        def say(text: str) -> None:
            typer.echo(f"\n{text}")

        FolderWatcher(folder, make_context, ask, say, interval=settings.purchase_watch_seconds).run()
