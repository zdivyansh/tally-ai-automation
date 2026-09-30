"""Command-line interface: `tally-ai --help`."""

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import typer

from tally_ai import __version__
from tally_ai.config import get_settings
from tally_ai.logging_setup import configure_logging

if TYPE_CHECKING:
    from tally_ai.config import Settings
    from tally_ai.tally import TallyQueries

Report = Callable[[str, bool, str], None]

app = typer.Typer(help="Natural-language voucher entry for Tally.", no_args_is_help=True)


@app.callback()
def main() -> None:
    configure_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def chat(
    today: str | None = typer.Option(None, help="Pretend today is this date (YYYY-MM-DD); for testing"),
) -> None:
    """Enter sales in plain language, e.g. 'sold 3 ctn KK 300 5/- to Sharma ji'."""
    import getpass
    import sys
    import uuid
    from datetime import date

    from tally_ai.agents.sales import SalesAgent, SalesContext
    from tally_ai.agents.sales.extraction import LLMExtractor
    from tally_ai.agents.sales.store import TallyVoucherStore
    from tally_ai.audit import AuditLog
    from tally_ai.llm import create_chat_model
    from tally_ai.masters.cache import MasterData
    from tally_ai.tally import TallyClient, TallyQueries

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    fixed_today = date.fromisoformat(today) if today else None

    def current_day() -> date:
        return fixed_today or date.today()

    with TallyClient.from_settings(settings) as client:
        typer.echo("Loading masters from Tally...")
        masters = MasterData.load(TallyQueries(client), settings, current_day())
        agent = SalesAgent(
            SalesContext(
                masters=masters,
                extractor=LLMExtractor(create_chat_model(settings)),
                store=TallyVoucherStore(client),
                voucher_type=settings.sales_voucher_type,
                number_prefix=settings.sales_number_prefix,
                today=current_day,
                audit=AuditLog(settings.audit_db_path),
                channel="cli",
            )
        )
        typer.echo(
            f"Ready ({len(masters.items)} items, LLM {settings.llm_provider}). "
            "Type a sale, or 'exit' to quit.\n"
        )
        while True:
            try:
                message = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message.lower() in {"exit", "quit"}:
                break
            if not message:
                continue
            thread = str(uuid.uuid4())
            turn = agent.start(message, thread, user=getpass.getuser())
            while not turn.done:
                typer.echo(f"\n{turn.text}\n")
                try:
                    answer = input("you> ")
                except (EOFError, KeyboardInterrupt):
                    answer = "cancel"
                turn = agent.reply(answer, thread)
            typer.echo(f"\n{turn.text}\n")


@app.command()
def doctor(skip_llm: bool = typer.Option(False, "--skip-llm", help="Do not call the LLM")) -> None:
    """Check the Tally connection, master data and LLM configuration."""
    from tally_ai.llm import LLMConfigError, create_chat_model
    from tally_ai.tally import TallyClient, TallyError, TallyQueries

    settings = get_settings()
    ok = True

    def report(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok &= passed
        mark = typer.style("OK  ", fg="green") if passed else typer.style("FAIL", fg="red")
        typer.echo(f"[{mark}] {label}: {detail}")

    with TallyClient.from_settings(settings) as client:
        report("Tally server", client.ping(), settings.tally_url)
        try:
            queries = TallyQueries(client)
            companies = queries.companies()
            report("Company", bool(companies), ", ".join(c.name for c in companies) or "none open")
            ledgers = queries.ledgers()
            debtors = sum(ledger.is_under("Sundry Debtors") for ledger in ledgers)
            creditors = sum(ledger.is_under("Sundry Creditors") for ledger in ledgers)
            report("Ledgers", bool(ledgers), f"{len(ledgers)} ({debtors} debtors, {creditors} creditors)")
            items = queries.stock_items()
            report("Stock items", bool(items), str(len(items)))
            types = {t.name: t for t in queries.voucher_types()}
            for kind in ("Sales", "Purchase", "Receipt", "Payment"):
                vt = types.get(kind)
                report(
                    f"Voucher type {kind}", vt is not None, (vt.numbering_method or "?") if vt else "missing"
                )
        except TallyError as e:
            report("Tally data", False, str(e))
        else:
            _check_sales_setup(queries, settings, report)

    if skip_llm:
        typer.echo(f"[SKIP] LLM: {settings.llm_provider}")
    else:
        try:
            model = create_chat_model(settings)
            start = time.perf_counter()
            reply = model.invoke("Reply with exactly: OK")
            elapsed = time.perf_counter() - start
            content = str(reply.content).strip()
            report(
                f"LLM {settings.llm_provider}", bool(content), f"replied {content[:20]!r} in {elapsed:.1f}s"
            )
        except LLMConfigError as e:
            report(f"LLM {settings.llm_provider}", False, str(e))
        except Exception as e:  # provider SDKs raise many different types
            report(f"LLM {settings.llm_provider}", False, f"{type(e).__name__}: {e}")

    raise typer.Exit(code=0 if ok else 1)


def _check_sales_setup(queries: "TallyQueries", settings: "Settings", report: "Report") -> None:
    """Masters load for the sales agent, ledgers resolve, and the next number can be computed."""
    from datetime import date

    from tally_ai.accounting.numbering import NumberingError, derive_prefix, next_number
    from tally_ai.agents.sales.store import TallyVoucherStore
    from tally_ai.masters.cache import MasterData, MasterDataError

    today = date.today()
    try:
        masters = MasterData.load(queries, settings, today)
    except MasterDataError as e:
        report("Sales setup", False, str(e))
        return
    ledgers = masters.sales_ledgers
    report(
        "Sales ledgers",
        True,
        f"sales={ledgers.sales}, IGST sales={ledgers.interstate_sales}, {ledgers.cgst}/{ledgers.sgst}/"
        f"{ledgers.igst}, round off={ledgers.round_off}",
    )
    try:
        existing = TallyVoucherStore(queries.client).numbers_around(settings.sales_voucher_type, today)
        prefix = settings.sales_number_prefix or derive_prefix(existing)
        report("Next sales number", True, next_number(prefix, today, existing))
    except NumberingError as e:
        report("Next sales number", False, str(e))


from tally_ai.interfaces.cli_purchase import purchase_app  # noqa: E402

app.add_typer(purchase_app, name="purchase")

eval_app = typer.Typer(
    help="Measure extraction and matching accuracy (docs/evaluation.md).", no_args_is_help=True
)
app.add_typer(eval_app, name="eval")

DEFAULT_GENERATED = "tests/eval/generated.local.json"


@eval_app.command("generate")
def eval_generate(
    count: int = typer.Option(200, help="Number of cases"),
    seed: int = typer.Option(1, help="Random seed (same seed = same cases)"),
    out: str = typer.Option(DEFAULT_GENERATED, help="Output file (contains real names: keep local)"),
) -> None:
    """Write cases built from past sales invoices in Tally."""
    from datetime import date
    from pathlib import Path

    from tally_ai.accounting.dates import fy_end, fy_start
    from tally_ai.evaluation.cases import save_cases
    from tally_ai.evaluation.generate import generate_cases
    from tally_ai.masters.cache import MasterData
    from tally_ai.tally import TallyClient, TallyQueries

    settings = get_settings()
    today = date.today()
    with TallyClient.from_settings(settings) as client:
        queries = TallyQueries(client)
        masters = MasterData.load(queries, settings, today)
        lines = queries.sales_lines(
            voucher_type=settings.sales_voucher_type,
            from_date=fy_start(date(fy_start(today).year - 1, 4, 1)),
            to_date=fy_end(today),
        )
    cases = generate_cases(masters, lines, count=count, seed=seed)
    save_cases(Path(out), cases)
    typer.echo(f"Wrote {len(cases)} cases to {out}")


@eval_app.command("run")
def eval_run(
    files: Annotated[
        list[str] | None, typer.Argument(help="Case files (default: local hand-written and generated)")
    ] = None,
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM; match the known texts only (fast)"),
    no_tally: bool = typer.Option(False, "--no-tally", help="Skip matching; check extraction only"),
    model: str | None = typer.Option(None, help="Ollama model to use instead of OLLAMA_MODEL"),
    limit: int | None = typer.Option(None, help="Only the first N cases of each file"),
    failures: int = typer.Option(20, help="How many failures to print"),
    fail_on_wrong: bool = typer.Option(False, help="Exit with code 1 if anything was picked wrong"),
) -> None:
    """Run evaluation cases and print accuracy."""
    import sys
    from datetime import date
    from pathlib import Path

    from tally_ai.agents.sales.extraction import LLMExtractor
    from tally_ai.evaluation.cases import load_cases
    from tally_ai.evaluation.runner import run_cases
    from tally_ai.llm import create_chat_model
    from tally_ai.masters.cache import MasterData
    from tally_ai.tally import TallyClient, TallyQueries

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    if model:
        settings = settings.model_copy(update={"ollama_model": model})
    paths = (
        [Path(f) for f in files]
        if files
        else [p for p in (Path("tests/eval/cases.local.json"), Path(DEFAULT_GENERATED)) if p.exists()]
    )
    if not paths:
        typer.echo("No case files. Run 'tally-ai eval generate' or pass a file.")
        raise typer.Exit(code=2)

    masters = None
    if not no_tally:
        with TallyClient.from_settings(settings) as client:
            masters = MasterData.load(TallyQueries(client), settings, date.today())
    extractor = None if no_llm else LLMExtractor(create_chat_model(settings))

    wrong = 0
    for path in paths:
        cases = load_cases(path)[:limit]
        if no_llm:
            cases = [c for c in cases if c.texts is not None]
        label = f"{settings.llm_provider}:{settings.ollama_model}" if extractor else "no LLM"
        typer.echo(f"\n=== {path} ({len(cases)} cases, {label}) ===")
        report = run_cases(cases, extractor=extractor, masters=masters, today=date.today())
        typer.echo(report.render(show_failures=failures))
        wrong += report.wrong_auto
    if fail_on_wrong and wrong:
        raise typer.Exit(code=1)
