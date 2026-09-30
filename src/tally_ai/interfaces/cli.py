"""Command-line interface: `tally-ai --help`."""

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

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
    import sys
    import uuid
    from datetime import date

    from tally_ai.agents.sales import SalesAgent, SalesContext
    from tally_ai.agents.sales.extraction import LLMExtractor
    from tally_ai.agents.sales.store import TallyVoucherStore
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
            turn = agent.start(message, thread)
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
