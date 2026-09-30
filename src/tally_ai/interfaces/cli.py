"""Command-line interface: `tally-ai --help`."""

import time

import typer

from tally_ai import __version__
from tally_ai.config import get_settings
from tally_ai.logging_setup import configure_logging

app = typer.Typer(help="Natural-language voucher entry for Tally.", no_args_is_help=True)


@app.callback()
def main() -> None:
    configure_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


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
